"""Language registry: extension -> grammar, plus the tree-sitter queries that
extract definitions, calls and imports for each language.

Queries are verified against the installed grammar wheels. Anything that fails to
compile (grammar version drift) is skipped with a warning rather than killing the
whole index - see parser._compile.
"""

from __future__ import annotations

import tree_sitter as ts

# Grammar loaders are imported lazily so a missing wheel degrades one language,
# not the whole server.
_GRAMMARS = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "php": ("tree_sitter_php", "language_php"),
    "ruby": ("tree_sitter_ruby", "language"),
    "go": ("tree_sitter_go", "language"),
}

EXT_LANG = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
    ".rb": "ruby", ".rake": "ruby",
    ".go": "go",
}

# Node types that introduce a containing scope (class/module). Used to attribute a
# definition to its parent and to scope method-call resolution.
CONTAINER_TYPES = {
    "python": {"class_definition"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration"},
    "tsx": {"class_declaration"},
    "php": {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"},
    "ruby": {"class", "module"},
    "go": set(),
}

# Definition patterns: (kind, query). Each query captures @def (whole node) and @name.
DEFS = {
    "python": [
        ("function", "(function_definition name: (identifier) @name) @def"),
        ("class", "(class_definition name: (identifier) @name) @def"),
    ],
    "javascript": [
        ("function", "(function_declaration name: (identifier) @name) @def"),
        ("function", "(generator_function_declaration name: (identifier) @name) @def"),
        ("method", "(method_definition name: (property_identifier) @name) @def"),
        ("class", "(class_declaration name: (identifier) @name) @def"),
        ("function", "(variable_declarator name: (identifier) @name value: (arrow_function)) @def"),
        ("function", "(variable_declarator name: (identifier) @name value: (function_expression)) @def"),
        ("function", "(variable_declarator name: (identifier) @name value: (generator_function)) @def"),
    ],
    "typescript": [
        ("function", "(function_declaration name: (identifier) @name) @def"),
        ("function", "(generator_function_declaration name: (identifier) @name) @def"),
        ("method", "(method_definition name: (property_identifier) @name) @def"),
        ("class", "(class_declaration name: (type_identifier) @name) @def"),
        ("interface", "(interface_declaration name: (type_identifier) @name) @def"),
        ("function", "(variable_declarator name: (identifier) @name value: (arrow_function)) @def"),
        ("function", "(variable_declarator name: (identifier) @name value: (function_expression)) @def"),
        ("function", "(variable_declarator name: (identifier) @name value: (generator_function)) @def"),
    ],
    "php": [
        ("function", "(function_definition name: (name) @name) @def"),
        ("method", "(method_declaration name: (name) @name) @def"),
        ("class", "(class_declaration name: (name) @name) @def"),
        ("interface", "(interface_declaration name: (name) @name) @def"),
        ("trait", "(trait_declaration name: (name) @name) @def"),
    ],
    "ruby": [
        ("method", "(method name: (identifier) @name) @def"),
        ("method", "(singleton_method name: (identifier) @name) @def"),
        ("class", "(class name: (constant) @name) @def"),
        ("module", "(module name: (constant) @name) @def"),
    ],
    "go": [
        ("function", "(function_declaration name: (identifier) @name) @def"),
        ("method", "(method_declaration name: (field_identifier) @name) @def"),
        ("class", "(type_spec name: (type_identifier) @name) @def"),
    ],
}
DEFS["tsx"] = DEFS["typescript"]

# Call patterns: each captures @callee (the called name) and optionally @recv (the
# receiver/scope, used to detect this/self/static for method-call scoping).
CALLS = {
    "python": [
        "(call function: (identifier) @callee)",
        "(call function: (attribute object: (_) @recv attribute: (identifier) @callee))",
    ],
    "javascript": [
        "(call_expression function: (identifier) @callee)",
        "(call_expression function: (member_expression object: (_) @recv property: (property_identifier) @callee))",
    ],
    "typescript": [
        "(call_expression function: (identifier) @callee)",
        "(call_expression function: (member_expression object: (_) @recv property: (property_identifier) @callee))",
    ],
    "php": [
        "(function_call_expression function: (name) @callee)",
        "(member_call_expression object: (_) @recv name: (name) @callee)",
        "(scoped_call_expression scope: (_) @recv name: (name) @callee)",
    ],
    "ruby": [
        "(call method: (identifier) @callee)",
        # Ruby allows a bare method invocation with no receiver and no parens,
        # which the grammar parses as a lone identifier in statement position.
        "(body_statement (identifier) @callee)",
    ],
    "go": [
        "(call_expression function: (identifier) @callee)",
        "(call_expression function: (selector_expression field: (field_identifier) @callee))",
    ],
}
CALLS["tsx"] = CALLS["typescript"]

# Import patterns: capture @module (a string or dotted path). Stored per-file for
# the file-dependency view; call edges remain the resolved symbol-level graph.
IMPORTS = {
    "python": [
        "(import_statement name: (dotted_name) @module)",
        "(import_from_statement module_name: (dotted_name) @module)",
        "(import_statement name: (aliased_import name: (dotted_name) @module))",
    ],
    "javascript": ["(import_statement source: (string) @module)"],
    "typescript": ["(import_statement source: (string) @module)"],
    "tsx": ["(import_statement source: (string) @module)"],
    "php": [],
    "ruby": [],
    "go": ["(import_spec path: (interpreted_string_literal) @module)"],
}

# --- call-shape classification ---
# A call's receiver text decides what it CAN target. Resolution then filters
# candidates by this shape instead of matching on name alone.
#   SELF_RECEIVERS  -> a method of the SAME class as the caller.
#   SUPER_RECEIVERS -> a method of a DIFFERENT (parent) class.
# Anything else is either a known class name (static call), a typed local
# (instance call resolved via local type inference), or an untyped instance.
SELF_RECEIVERS = {"self", "this", "$this", "static"}
SUPER_RECEIVERS = {"super", "parent"}

# Languages where a bare call f() inside a class implicitly targets a same-class
# method (method lookup on the implicit self). In Python/JS/PHP/Go a bare call
# can only reach a free function - a method requires an explicit receiver.
BARE_SELF_METHOD_LANGS = {"ruby"}

# Languages where a bare `Name()` receiver is CONSTRUCTION (so `Name().m()` calls a
# method of class Name). Only Python. Elsewhere `Name()` is a function call with an
# unverified return type (PHP/JS/TS/Go construct via `new Name()`/composite literals;
# Ruby via `Name.new`), so it must NOT resolve as a static reference to a same-named class.
CONSTRUCT_BY_CALL_LANGS = {"python"}

# Resolution-family langs (post-_fam, so the js family is "js") where a bare `Name()`
# CALL can legitimately hit a same-named class: construction (python), whichever
# binding is in scope (js - legacy `function Modal` vs `class Modal`), type conversion
# (go). In PHP functions and classes occupy separate namespaces and Ruby constructs
# only via `.new`, so there a bare call provably targets the free function even when
# a same-named class exists.
BARE_CALL_CLASS_LANGS = {"python", "js", "go"}

# Resolution-family langs where a LOCAL binding (param, local var) shadows a
# same-named free function for a bare call. PHP is exempt: variables are $-sigiled
# and live in a separate namespace, so `handler()` always means the function.
LOCALS_SHADOW_FUNCS = {"python", "js", "go", "ruby"}

# Local instantiation patterns: capture @var (the assigned name) and @cls (the
# class being constructed) so `x = Foo(); x.m()` resolves x.m to Foo.m precisely.
ASSIGNS = {
    "python": [
        "(assignment left: (identifier) @var right: (call function: (identifier) @cls))",
    ],
    "javascript": [
        "(variable_declarator name: (identifier) @var value: (new_expression constructor: (identifier) @cls))",
        "(assignment_expression left: (identifier) @var right: (new_expression constructor: (identifier) @cls))",
    ],
    "php": [
        "(assignment_expression left: (variable_name (name) @var) right: (object_creation_expression (name) @cls))",
    ],
    "ruby": [
        "(assignment left: (identifier) @var right: (call receiver: (constant) @cls))",
    ],
    "go": [
        "(short_var_declaration right: (expression_list (composite_literal type: (type_identifier) @cls)))",
        "(short_var_declaration right: (expression_list (unary_expression operand: (composite_literal type: (type_identifier) @cls))))",
        "(var_spec name: (identifier) type: (type_identifier) @cls)",
        "(var_spec value: (expression_list (composite_literal type: (type_identifier) @cls)))",
    ],
}
ASSIGNS["typescript"] = ASSIGNS["javascript"]
ASSIGNS["tsx"] = ASSIGNS["javascript"]

# Declared types - typed parameters and annotated local variables. These let an
# INJECTED receiver resolve (`def handler(req: Request): req.json()`), the dominant
# shape in modern DI/framework code that local-construction capture misses entirely.
# Each `@hint` node is mined structurally for (var, type) in the parser. Resolved as
# `inferred`, NOT `exact`: a hint is a declared contract the runtime can break (the
# annotated type may be an interface/base whose real subclass differs).
TYPE_HINTS = {
    "python": [
        "(typed_parameter) @hint",
        "(typed_default_parameter) @hint",
        "(assignment type: (type)) @hint",
    ],
    "javascript": [],  # no type annotations in plain JS
    "typescript": [
        "(required_parameter) @hint",
        "(optional_parameter) @hint",
        "(variable_declarator type: (type_annotation)) @hint",
    ],
    "php": [
        "(simple_parameter) @hint",
        "(property_promotion_parameter) @hint",  # PHP 8 constructor promotion
    ],
    "ruby": [],  # dynamically typed
    "go": [],    # Go types already captured exactly via ASSIGNS/var_spec
}
TYPE_HINTS["tsx"] = TYPE_HINTS["typescript"]

# Local binding sites - names bound in a function scope (params, loop/with targets,
# assignment LHS) EVEN WHEN UNTYPED. A name bound locally masks a same-named global
# class or module-level construct, so the resolver must not fall back to those (which
# would fabricate a wrong `exact` and drop the real caller). Recorded as kind='shadow'.
SHADOWS = {
    "python": [
        "(parameters (identifier) @shadow)",
        "(default_parameter name: (identifier) @shadow)",
        "(lambda_parameters (identifier) @shadow)",
        "(for_statement left: (identifier) @shadow)",
        "(as_pattern alias: (as_pattern_target (identifier) @shadow))",
        "(assignment left: (identifier) @shadow)",
    ],
    "javascript": [
        "(formal_parameters (identifier) @shadow)",
        "(for_in_statement left: (identifier) @shadow)",
        "(variable_declarator name: (identifier) @shadow)",
    ],
    "typescript": [
        "(required_parameter pattern: (identifier) @shadow)",
        "(optional_parameter pattern: (identifier) @shadow)",
        "(for_in_statement left: (identifier) @shadow)",
        "(variable_declarator name: (identifier) @shadow)",
    ],
    "php": [
        "(simple_parameter name: (variable_name (name) @shadow))",
        "(property_promotion_parameter name: (variable_name (name) @shadow))",
        "(foreach_statement (variable_name (name) @shadow))",
        "(assignment_expression left: (variable_name (name) @shadow))",
    ],
    # Ruby: a local assignment/param shadows a same-named method for BARE references
    # (`report = 5; report` reads the local, never calls). Feeds the callee-side
    # masking so the statement-position identifier capture can't fabricate a call
    # edge out of a variable read.
    "ruby": [
        "(assignment left: (identifier) @shadow)",
        "(method_parameters (identifier) @shadow)",
        "(block_parameters (identifier) @shadow)",
        "(optional_parameter name: (identifier) @shadow)",
        "(keyword_parameter name: (identifier) @shadow)",
    ],
}
SHADOWS["tsx"] = SHADOWS["typescript"]

# Grammars that actually have JSX element nodes. The plain `typescript` dialect does NOT
# (only `tsx` does); `javascript` includes JSX. Running the JSX queries elsewhere errors
# "Invalid node type" once per file. HOC-component detection runs for all JS-family langs.
JSX_LANGS = {"tsx", "javascript"}

# WordPress / PHP hook dispatch - string-named action/filter coupling that a call
# graph cannot see (the target is a string, not a syntactic call). Captured as a
# separate 'hook' edge tier. PHP first (where WP lives); extensible to JS wp.hooks.
# Python framework string-dispatch the call graph can't see (Phase 1: Django signals).
# A signal is identified by its bare name (post_save); `@receiver(sig)` / `sig.connect(cb)`
# register a listener, `sig.send()` fires it - a fire and a listener on the same signal
# name link into a hook-tier edge, the spooky-action coupling the AST misses.
PY_SIGNAL_CONNECT = {"connect"}
PY_SIGNAL_FIRE = {"send", "send_robust"}
PY_LISTENER_DECORATORS = {"receiver"}
# Celery: @task/@shared_task/@app.task defines a task; task.delay()/apply_async() fires it
# -> caller -> task-body edge (async dispatch the call graph can't see). Evidence-gated like
# signals: a `.delay` only fires a name backed by an in-repo @task decorator.
PY_TASK_DECORATORS = {"task", "shared_task"}
PY_TASK_FIRE = {"delay", "apply_async"}
# Web routes -> ENTRY POINTS (attack surface, like WP REST). Flask/FastAPI route decorators
# and Django URLconf path()/re_path()/url(route, view).
PY_ROUTE_DECORATOR_METHODS = {"route", "get", "post", "put", "delete", "patch", "websocket"}
PY_ROUTE_CALLS = {"path", "re_path", "url"}
PY_SIGNAL_CLASSES = {"Signal"}  # `x = Signal()` evidences x as a real signal

# Ruby mixin sites: `include/extend/prepend Util` inside a class wires the module's
# methods into the class's bare-call method lookup (Ruby MRO: class -> mixins ->
# Object). Captured as kind='mixin' assigns (cls=module name; the enclosing class is
# attached via byte containment). The parser walks the call's argument nodes itself -
# never paired independent captures (the zip(@name,@def) mispairing lesson).
RUBY_MIXIN_METHODS = {"include", "extend", "prepend"}
# Django built-in signals - evidence that a bare `.connect`/`.send` name is a signal, so a
# non-signal `sock.connect(fn)`+`sock.send()` can't fabricate a hook edge to a real function.
DJANGO_BUILTIN_SIGNALS = {
    "pre_init", "post_init", "pre_save", "post_save", "pre_delete", "post_delete",
    "m2m_changed", "pre_migrate", "post_migrate", "class_prepared",
    "request_started", "request_finished", "got_request_exception", "setting_changed",
    "connection_created", "user_logged_in", "user_logged_out", "user_login_failed",
}

HOOK_REGISTER = {"php": {"add_action", "add_filter"}}
HOOK_FIRE = {"php": {"do_action", "do_action_ref_array", "apply_filters", "apply_filters_ref_array"}}
HOOK_REST = {"php": {"register_rest_route"}}
# Query surfaces every plain function call; the parser derives function/arguments from
# each call node by field (no parallel-capture-list ordering hazard) and filters by name.
HOOK_CALL_QUERY = {
    "php": "(function_call_expression) @call",
}

_lang_cache: dict[str, ts.Language] = {}


def supported_langs() -> list[str]:
    return list(_GRAMMARS.keys())


def lang_for_path(path: str) -> str | None:
    import os
    return EXT_LANG.get(os.path.splitext(path)[1].lower())


def get_language(lang: str) -> ts.Language | None:
    """Load a grammar; return None (and stay quiet) if its wheel is absent."""
    if lang in _lang_cache:
        return _lang_cache[lang]
    spec = _GRAMMARS.get(lang)
    if not spec:
        return None
    module_name, fn_name = spec
    try:
        mod = __import__(module_name)
        language = ts.Language(getattr(mod, fn_name)())
    except Exception:
        return None
    _lang_cache[lang] = language
    return language
