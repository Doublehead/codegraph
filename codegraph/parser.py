"""Parse one source file into definitions, call-references and imports.

Resolution of a call to its target symbol happens later, globally, in store.py -
here we only extract what the AST literally says, plus the cheap structural facts
(which definition encloses a call; which class encloses a method).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter as ts

from . import languages as L


@dataclass
class Symbol:
    name: str
    kind: str
    container: str | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str


@dataclass
class Ref:
    callee: str
    receiver: str | None
    line: int
    byte: int           # start byte of the callee, for containment lookup
    src_byte: int | None = None  # filled when attributed to an enclosing symbol


@dataclass
class Assign:
    var: str            # local/instance variable name
    cls: str            # class it is constructed from / declared as
    byte: int           # position, for containment lookup
    src_byte: int | None = None
    kind: str = "construct"  # construct = proven (x=Foo()); hint = declared (x: Foo)


@dataclass
class HookSite:
    """A WordPress hook registration or fire - string dispatch the call graph can't see.
    'listen' = add_action/add_filter/register_rest_route; 'fire' = do_action/apply_filters."""
    kind: str                     # 'listen' | 'fire'
    hook: str | None              # hook name / REST route; None if dynamic (non-literal)
    hook_class: str               # action|filter|ajax|ajax_nopriv|rest|fire
    cb_kind: str | None = None    # free|static|method_self|method|closure|dynamic
    cb_name: str | None = None
    cb_container: str | None = None
    cb_recv: str | None = None    # receiver var for [$obj,'m'] -> local type inference
    cb_raw: str = ""
    entry_point: bool = False     # public attack surface (ajax/rest)
    unauth: bool = False          # reachable without auth (nopriv / __return_true perm)
    note: str = ""
    line: int = 0
    byte: int = 0
    src_byte: int | None = None


@dataclass
class ParsedFile:
    lang: str
    symbols: list[Symbol] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    assigns: list[Assign] = field(default_factory=list)
    hooks: list[HookSite] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_query_cache: dict[str, ts.Query] = {}


def _compile(lang: ts.Language, src: str, warnings: list[str]) -> ts.Query | None:
    key = f"{id(lang)}:{src}"
    if key in _query_cache:
        return _query_cache[key]
    try:
        q = ts.Query(lang, src)
    except Exception as e:  # grammar drift - degrade this pattern only
        warnings.append(f"query failed: {src[:50]}... ({e})")
        return None
    _query_cache[key] = q
    return q


def _captures(query: ts.Query, node: ts.Node) -> dict[str, list[ts.Node]]:
    return ts.QueryCursor(query).captures(node)


def _container_of(node: ts.Node, container_types: set[str], source: bytes) -> str | None:
    """Walk up to the nearest enclosing class/module and return its name."""
    p = node.parent
    while p is not None:
        if p.type in container_types:
            name_node = p.child_by_field_name("name")
            if name_node is not None:
                return source[name_node.start_byte:name_node.end_byte].decode("utf8", "replace")
        p = p.parent
    return None


def _receiver_of(callee_node: ts.Node, source: bytes) -> str | None:
    """The object/scope a call is made on (for this/self/static scoping). Read from
    the callee's parent fields rather than a parallel capture list, which can be
    mis-ordered relative to the callee list."""
    parent = callee_node.parent
    if parent is None:
        return None
    for field in ("object", "scope", "receiver", "operand"):
        recv = parent.child_by_field_name(field)
        if recv is not None:
            return source[recv.start_byte:recv.end_byte].decode("utf8", "replace").strip()
    return None


_CLASS_AT = re.compile(rb"(?:class|trait|interface|enum)\s+([A-Za-z_]\w*)")


def _isword(b: int) -> bool:
    return b == 0x5F or 0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A


def _class_decl_sites(source: bytes):
    """(start_byte, name) for each class/trait/interface/enum declaration at a CODE
    position. Strings and comments are skipped so a 'class' word inside a comment or
    string never manufactures a phantom container during error recovery."""
    n, i, sites = len(source), 0, []
    while i < n:
        c = source[i]
        if c == 0x2F and i + 1 < n:                       # '/'
            nxt = source[i + 1]
            if nxt == 0x2F:
                j = source.find(b"\n", i + 2); i = n if j < 0 else j + 1; continue
            if nxt == 0x2A:
                j = source.find(b"*/", i + 2); i = n if j < 0 else j + 2; continue
        if c == 0x23 and source[i + 1:i + 2] != b"[":     # '#' line comment (not #[ attr)
            j = source.find(b"\n", i + 1); i = n if j < 0 else j + 1; continue
        if c == 0x27 or c == 0x22:                         # ' or "  string literal
            i += 1
            while i < n:
                d = source[i]
                if d == 0x5C: i += 2; continue
                if d == c: i += 1; break
                i += 1
            continue
        if c in (0x63, 0x74, 0x69, 0x65) and (i == 0 or not _isword(source[i - 1])):  # c/t/i/e
            m = _CLASS_AT.match(source, i)
            if m:
                sites.append((m.start(), m.group(1).decode("utf8", "replace")))
                i = m.end(); continue
        i += 1
    return sites


def _class_body_end(source: bytes, decl_start: int) -> int | None:
    """Byte index just past a class body's matching '}', found by brace-matching the
    raw bytes from a class declaration while skipping braces inside strings and
    comments. Returns None when no balanced body brace is found (bounds uncertain,
    or a non-brace language) so the caller leaves such symbols containerless."""
    n, i, depth, started = len(source), decl_start, 0, False
    while i < n:
        c = source[i]
        if c == 0x2F and i + 1 < n:                      # '/'
            nxt = source[i + 1]
            if nxt == 0x2F:                              # //  line comment
                j = source.find(b"\n", i + 2); i = n if j < 0 else j + 1; continue
            if nxt == 0x2A:                              # /*  block comment
                j = source.find(b"*/", i + 2); i = n if j < 0 else j + 2; continue
        elif c == 0x23 and source[i + 1:i + 2] != b"[":  # '#' line comment (not #[ attribute)
            j = source.find(b"\n", i + 1); i = n if j < 0 else j + 1; continue
        elif c == 0x27 or c == 0x22:                      # ' or "  string literal
            i += 1
            while i < n:
                d = source[i]
                if d == 0x5C: i += 2; continue            # backslash escape
                if d == c: i += 1; break
                i += 1
            continue
        elif c == 0x7B:                                   # {
            depth += 1; started = True
        elif c == 0x7D:                                   # }
            depth -= 1
            if started and depth == 0:
                return i + 1
        i += 1
    return None


def _recover_containers(pf: ParsedFile, source: bytes) -> None:
    """Error-recovery for container detection. When a parse error collapses a class
    into ERROR nodes, its methods reparse as containerless top-level functions and
    every `$this->m()` / `self::class` resolution breaks. Brace-match each class
    declaration's body from the raw bytes and re-attribute only the container-less
    symbols that fall INSIDE a class body. Genuine top-level functions after a broken
    class fall outside every body and stay containerless rather than being swept into
    the previous class. Only runs on files that failed to parse, so cleanly-parsed
    files keep their precise (AST parent-walk) containers."""
    ranges = []
    for start, name in _class_decl_sites(source):
        end = _class_body_end(source, start)
        if end is not None:
            ranges.append((start, end, name))
    if not ranges:
        return
    for s in pf.symbols:
        if s.container is not None or s.kind == "class":
            continue
        cls, cls_start = None, -1
        for start, end, name in ranges:  # tightest enclosing body wins (handles nesting)
            if start <= s.start_byte < end and name != s.name and start > cls_start:
                cls, cls_start = name, start
        if cls:
            s.container = cls
            s.kind = "method"


def _signature(node: ts.Node, source: bytes) -> str:
    text = source[node.start_byte:node.end_byte].decode("utf8", "replace")
    first = text.splitlines()[0] if text else ""
    return first.strip()[:200]


def parse(source: bytes, lang: str) -> ParsedFile:
    out = ParsedFile(lang=lang)
    language = L.get_language(lang)
    if language is None:
        out.warnings.append(f"no grammar for {lang}")
        return out

    parser = ts.Parser(language)
    root = parser.parse(source).root_node
    container_types = L.CONTAINER_TYPES.get(lang, set())

    # --- definitions ---
    for kind, qsrc in L.DEFS.get(lang, []):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        # Capture only @def; resolve each def's name via its own `name` field.
        # tree-sitter returns separate capture lists in independent orders, so
        # zipping @name to @def mispairs them - the name MUST come from the node.
        for def_node in _captures(q, root).get("def", []):
            name_node = def_node.child_by_field_name("name")
            if name_node is None:
                continue
            name = source[name_node.start_byte:name_node.end_byte].decode("utf8", "replace")
            container = _container_of(def_node, container_types, source)
            # Go methods aren't nested in a type node; the receiver type is the
            # container. Also record receiver as a typed local so r.M() resolves.
            if lang == "go" and def_node.type == "method_declaration":
                rv_name, rv_type = _go_receiver(def_node, source)
                if rv_type:
                    container = rv_type
                    if rv_name:
                        out.assigns.append(Assign(var=rv_name, cls=rv_type, byte=def_node.start_byte))
            real_kind = "method" if (kind == "function" and container) else kind
            out.symbols.append(Symbol(
                name=name,
                kind=real_kind,
                container=container,
                start_line=def_node.start_point[0] + 1,
                end_line=def_node.end_point[0] + 1,
                start_byte=def_node.start_byte,
                end_byte=def_node.end_byte,
                signature=_signature(def_node, source),
            ))

    # Recover class membership when a parse error collapsed the class node (e.g.
    # tree-sitter-php choking on `const NAMESPACE`), orphaning its methods. Brace
    # languages only: in Python/Ruby a class body has no braces, so the byte matcher
    # would lock onto a later dict/hash literal and sweep genuine top-level functions
    # into the broken class as phantom methods.
    if root.has_error and lang in ("php", "javascript", "typescript", "tsx"):
        _recover_containers(out, source)

    # --- calls ---
    for qsrc in L.CALLS.get(lang, []):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        for callee_node in _captures(q, root).get("callee", []):
            name = source[callee_node.start_byte:callee_node.end_byte].decode("utf8", "replace")
            out.refs.append(Ref(
                callee=name,
                receiver=_receiver_of(callee_node, source),
                line=callee_node.start_point[0] + 1,
                byte=callee_node.start_byte,
            ))

    # --- imports ---
    for qsrc in L.IMPORTS.get(lang, []):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        for mod_node in _captures(q, root).get("module", []):
            mod = source[mod_node.start_byte:mod_node.end_byte].decode("utf8", "replace").strip("'\"`")
            out.imports.append(mod)

    # --- instantiations (local type inference) ---
    for qsrc in L.ASSIGNS.get(lang, []):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        for cls_node in _captures(q, root).get("cls", []):
            var = _assign_var(cls_node, source, lang)
            if not var:
                continue
            # Ruby: `x = Const.m` is construction ONLY for `.new`; `User.where(...)` returns
            # a relation, not a User, so don't record it as a proven type (-> ambiguous).
            if lang == "ruby":
                call = cls_node.parent
                m = call.child_by_field_name("method") if call is not None else None
                if m is None or source[m.start_byte:m.end_byte].decode("utf8", "replace") != "new":
                    continue
            cls = source[cls_node.start_byte:cls_node.end_byte].decode("utf8", "replace")
            out.assigns.append(Assign(var=var, cls=cls, byte=cls_node.start_byte))

    # --- declared types (typed params / annotated vars) -> 'hint' assigns ---
    _extract_types(out, root, source, language, lang)

    # --- local binding names (params/loops/assignments) -> 'shadow' assigns ---
    _extract_shadows(out, root, source, language, lang)

    # --- WordPress hook dispatch (string-named action/filter coupling) ---
    _extract_hooks(out, root, source, language, lang)

    # --- Python framework dispatch (Django signals: @receiver/connect <-> send) ---
    if lang == "python":
        _extract_py_coupling(out, root, source, language)

    # --- Ruby mixins (include/extend/prepend -> class bare-call method lookup) ---
    if lang == "ruby":
        _extract_rb_mixins(out, root, source, language)

    # --- PHP string constants (const NS / define) -> resolve constant-namespaced routes ---
    if lang == "php":
        _extract_php_consts(out, root, source, language)

    # --- React component graph (HOC-wrapped defs + JSX usage as edges) ---
    if lang in ("javascript", "typescript", "tsx"):
        _extract_tsx_components(out, root, source, language, lang)

    _attribute(out)
    return out


def _hook_expr(node: ts.Node | None, source: bytes) -> str:
    """A route-part's literal value, else its token / raw expression text so a dynamic
    route shows itself honestly instead of a bare '?'. A class-constant access
    (Foo::NS / self::NS) returns its 'Class::NAME' token for later const resolution."""
    if node is None:
        return "?"
    lit = _str_lit(node, source)
    if lit is not None:
        return lit
    return source[node.start_byte:node.end_byte].decode("utf8", "replace").strip()


def _extract_php_consts(out: ParsedFile, root: ts.Node, source: bytes, language) -> None:
    """String-valued PHP class constants (const NS = 'x';) and define()s, recorded as
    kind='const' assigns keyed by their TOKEN: class consts as 'Class::NAME', globals as
    'NAME'. resolve() swaps these into hook names that reference them (Foo::NS/scene ->
    zrougable/v1/scene), cross-file. Only literal values are stored; a computed const
    (X . '/y') is skipped so nothing is faked. Kept out of type inference (resolve()
    skips kind='const')."""
    ctypes = L.CONTAINER_TYPES.get("php", set())
    q = _compile(language, "(const_element) @el", out.warnings)
    if q is not None:
        for el in _captures(q, root).get("el", []):
            kids = el.named_children
            if len(kids) < 2 or kids[0].type != "name":
                continue
            name = _text(kids[0], source)
            val = _str_lit(kids[1], source)
            if not name or val is None:
                continue  # computed / non-literal const -> not faked
            cls = _container_of(el, ctypes, source)
            token = f"{cls}::{name}" if cls else name
            out.assigns.append(Assign(var=token, cls=val, byte=el.start_byte, kind="const"))
    qd = _compile(language, "(function_call_expression function: (name) @fn) @call", out.warnings)
    if qd is not None:
        for call in _captures(qd, root).get("call", []):
            fnn = call.child_by_field_name("function")
            if fnn_txt(fnn, source) != "define":
                continue
            args = call.child_by_field_name("arguments")
            vals = _arg_values(args) if args is not None else []
            if len(vals) >= 2:
                name = _str_lit(vals[0], source)
                val = _str_lit(vals[1], source)
                if name and val is not None:
                    out.assigns.append(Assign(var=name, cls=val, byte=call.start_byte, kind="const"))


def fnn_txt(node: ts.Node | None, source: bytes) -> str | None:
    return source[node.start_byte:node.end_byte].decode("utf8", "replace") if node is not None else None


def _str_lit(node: ts.Node | None, source: bytes) -> str | None:
    """Literal value of a PHP string. Handles both single-quoted `string` and
    double-quoted `encapsed_string` (tree-sitter-php uses the latter for ALL double
    quotes, interpolation or not). Concatenates every content/escape segment so a
    namespaced FQN ('App\\Mailer') isn't truncated at the first backslash. Returns
    None only when the string contains a real interpolation (a variable / {expr})."""
    if node is None or node.type not in ("string", "encapsed_string"):
        return None
    parts = []
    for c in node.children:
        if not c.is_named:
            continue
        if c.type == "string_content":
            parts.append(source[c.start_byte:c.end_byte].decode("utf8", "replace"))
        elif c.type == "escape_sequence":
            raw = source[c.start_byte:c.end_byte].decode("utf8", "replace")
            parts.append(raw[1:] if len(raw) == 2 and raw[0] == "\\" else raw)
        else:
            return None  # variable_name / interpolation -> genuinely dynamic
    return "".join(parts)


def _arg_values(args_node: ts.Node) -> list[ts.Node]:
    """Positional argument value nodes of a call's `arguments` node."""
    out = []
    for arg in args_node.children:
        if arg.type != "argument":
            continue
        vals = [c for c in arg.children if c.is_named]
        if vals:
            out.append(vals[-1])  # last named child = the value (skips a PHP8 name: label)
    return out


def _array_pairs(node: ts.Node):
    """(key_literal_or_None, value_node) for each element of an array_creation_expression."""
    pairs = []
    for el in node.children:
        if el.type != "array_element_initializer":
            continue
        named = [c for c in el.children if c.is_named]
        if len(named) >= 2:  # key => value
            pairs.append((named[0], named[1]))
        elif named:         # positional
            pairs.append((None, named[0]))
    return pairs


def _parse_callback(node: ts.Node, source: bytes):
    """Normalize a WP callback -> (cb_kind, cb_name, cb_container, cb_recv, raw)."""
    raw = source[node.start_byte:node.end_byte].decode("utf8", "replace")
    if node.type in ("string", "encapsed_string"):
        s = _str_lit(node, source)
        if s is None:
            return "dynamic", None, None, None, raw
        if "::" in s:
            cont, _, m = s.partition("::")
            return "static", m, cont, None, raw
        return "free", s, None, None, raw
    if node.type in ("anonymous_function", "arrow_function"):
        return "closure", None, None, None, raw
    if node.type == "array_creation_expression":
        pairs = _array_pairs(node)
        if len(pairs) >= 2:
            first, second = pairs[0][1], pairs[1][1]
            method = _str_lit(second, source)
            if method is None:
                return "dynamic", None, None, None, raw
            if first.type == "variable_name":
                v = source[first.start_byte:first.end_byte].decode("utf8", "replace").lstrip("$")
                if v == "this":
                    return "method_self", method, None, None, raw
                return "method", method, None, v, raw  # recv var -> local type inference
            if first.type in ("string", "encapsed_string"):
                return "static", method, _str_lit(first, source), None, raw
            if first.type == "class_constant_access_expression":  # Class::class / self::class
                scope = first.children[0] if first.children else None
                if scope is not None and scope.type == "relative_scope":
                    t = source[scope.start_byte:scope.end_byte].decode("utf8", "replace")
                    if t in ("self", "static"):
                        return "method_self", method, None, None, raw
                    return "method", method, None, None, raw  # parent:: -> generic
                if scope is not None and scope.type in ("name", "qualified_name"):
                    cont = source[scope.start_byte:scope.end_byte].decode("utf8", "replace")
                    return "static", method, cont, None, raw
        return "dynamic", None, None, None, raw
    return "dynamic", None, None, None, raw


def _call_fn_name(call: ts.Node, source: bytes) -> str | None:
    """Function name of a call, normalized - handles `\\add_action` (qualified_name,
    a global-namespace qualifier) by taking the last name segment."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "name":
        return source[fn.start_byte:fn.end_byte].decode("utf8", "replace")
    if fn.type == "qualified_name":
        names = [c for c in fn.children if c.type == "name"]
        if names:
            return source[names[-1].start_byte:names[-1].end_byte].decode("utf8", "replace")
    return None


def _classify_entry(hook: str | None):
    """(hook_class, entry, unauth) for WP entry-point hooks (ajax / admin_post),
    nopriv variants first; None if not an entry-point hook."""
    if not hook:
        return None
    if hook.startswith("wp_ajax_nopriv_") or hook == "wp_ajax_nopriv":
        return ("ajax_nopriv", True, True)
    if hook.startswith("admin_post_nopriv_") or hook == "admin_post_nopriv":
        return ("admin_post_nopriv", True, True)
    if hook.startswith("wp_ajax_") or hook == "wp_ajax":
        return ("ajax", True, False)
    if hook.startswith("admin_post_") or hook == "admin_post":
        return ("admin_post", True, False)
    return None


def _extract_hooks(out: ParsedFile, root: ts.Node, source: bytes, language, lang: str) -> None:
    hq = L.HOOK_CALL_QUERY.get(lang)
    if not hq:
        return
    reg = L.HOOK_REGISTER.get(lang, set())
    fire = L.HOOK_FIRE.get(lang, set())
    rest = L.HOOK_REST.get(lang, set())
    q = _compile(language, hq, out.warnings)
    if q is None:
        return
    for call in _captures(q, root).get("call", []):
        fn = _call_fn_name(call, source)
        args_node = call.child_by_field_name("arguments")
        if fn is None or args_node is None or (fn not in reg and fn not in fire and fn not in rest):
            continue
        vals = _arg_values(args_node)
        line = call.start_point[0] + 1
        byte = call.start_byte
        if fn in fire:
            hook = _str_lit(vals[0], source) if vals else None
            out.hooks.append(HookSite(kind="fire", hook=hook, hook_class="fire", line=line, byte=byte))
        elif fn in reg and len(vals) >= 2:
            hook = _str_lit(vals[0], source)
            ck, cn, cc, recv, raw = _parse_callback(vals[1], source)
            entry_info = _classify_entry(hook)
            if entry_info:
                hc, entry, unauth = entry_info
            else:
                hc, entry, unauth = ("filter" if fn == "add_filter" else "action"), False, False
            out.hooks.append(HookSite(kind="listen", hook=hook, hook_class=hc, cb_kind=ck,
                                      cb_name=cn, cb_container=cc, cb_recv=recv, cb_raw=raw,
                                      entry_point=entry, unauth=unauth, line=line, byte=byte))
        elif fn in rest and len(vals) >= 3:
            ns = _hook_expr(vals[0], source)      # literal, else 'Class::NS' token / raw expr
            route = _hook_expr(vals[1], source)
            hook = f"{ns}{route}"
            cb = perm = None
            for key, val in _array_pairs(vals[2]):
                k = _str_lit(key, source) if key is not None else None
                if k == "callback":
                    cb = val
                elif k == "permission_callback":
                    perm = val
            perm_lit = _str_lit(perm, source) if perm is not None else None
            unauth = perm is None or (perm_lit is not None and perm_lit.lstrip("\\") == "__return_true")
            note = "no permission_callback" if perm is None else (
                f"permission={source[perm.start_byte:perm.end_byte].decode('utf8', 'replace')[:40]}")
            if cb is not None:
                ck, cn, cc, recv, raw = _parse_callback(cb, source)
                out.hooks.append(HookSite(kind="listen", hook=hook, hook_class="rest", cb_kind=ck,
                                          cb_name=cn, cb_container=cc, cb_recv=recv, cb_raw=raw,
                                          entry_point=True, unauth=unauth, note=note, line=line, byte=byte))


def _attr_tail(node: ts.Node | None, source: bytes) -> str | None:
    """Rightmost name of an identifier/attribute chain - `signals.post_save` -> post_save,
    `post_save` -> post_save. Signals are matched by this bare name across sites."""
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node, source)
    if node.type == "attribute":
        return _text(node.child_by_field_name("attribute"), source)
    return None


def _first_pos_arg(args_node: ts.Node | None) -> ts.Node | None:
    """First positional (non-keyword) argument value, or None."""
    if args_node is None:
        return None
    for c in args_node.children:
        if c.is_named and c.type != "keyword_argument":
            return c
    return None


def _kw_arg(args_node: ts.Node | None, name: str, source: bytes) -> ts.Node | None:
    """Value node of a `name=...` keyword argument, or None."""
    if args_node is None:
        return None
    for c in args_node.children:
        if c.type == "keyword_argument" and _text(c.child_by_field_name("name"), source) == name:
            return c.child_by_field_name("value")
    return None


def _signal_names(node: ts.Node | None, source: bytes) -> list[str]:
    """Signal name(s) from a @receiver argument: a single signal, or a [list]/(tuple) of
    them (`@receiver([post_save, post_delete])` is idiomatic Django)."""
    if node is None:
        return []
    if node.type in ("list", "tuple"):
        return [n for c in node.children if c.is_named for n in (_attr_tail(c, source),) if n]
    n = _attr_tail(node, source)
    return [n] if n else []


def _py_callback(node: ts.Node | None, source: bytes):
    """(cb_kind, cb_name, cb_container) for a Python signal callback reference."""
    if node is None:
        return None, None, None
    if node.type == "identifier":
        return "free", _text(node, source), None
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        name = _text(node.child_by_field_name("attribute"), source)
        if obj is not None and obj.type == "identifier" and _text(obj, source) == "self":
            return "method_self", name, None  # self.handler -> the enclosing class
        return "method", name, None  # obj.handler -> resolve only if the method name is unique
    return None, None, None


def _py_str(node: ts.Node | None, source: bytes) -> str | None:
    """Literal value of a Python string node, or None if it isn't a plain string."""
    if node is None or node.type != "string":
        return None
    return "".join(_text(c, source) for c in node.children if c.type == "string_content")


def _pos_args(args_node: ts.Node | None) -> list[ts.Node]:
    """Positional (non-keyword) argument value nodes, in order."""
    if args_node is None:
        return []
    return [c for c in args_node.children if c.is_named and c.type != "keyword_argument"]


def _extract_py_coupling(out: ParsedFile, root: ts.Node, source: bytes, language) -> None:
    """Python framework string-dispatch the call graph can't see, captured as hook-tier
    coupling: Django SIGNALS (@receiver/connect <-> send), Celery TASKS (@task <-> .delay/
    .apply_async, a caller->task edge), and web ROUTES (Flask/FastAPI decorators + Django
    path()) as ENTRY POINTS / attack surface. Signal & task fires are evidence-gated in
    the store so a non-signal/non-task `.connect`/`.send`/`.delay` can't fabricate an edge."""
    q = _compile(language, "(call) @call", out.warnings)
    if q is not None:
        for call in _captures(q, root).get("call", []):
            fn = call.child_by_field_name("function")
            if fn is None:
                continue
            line, byte = call.start_point[0] + 1, call.start_byte
            args = call.child_by_field_name("arguments")
            if fn.type == "attribute":
                method = _text(fn.child_by_field_name("attribute"), source)
                obj = _attr_tail(fn.child_by_field_name("object"), source)
                if not obj:
                    continue
                if method in L.PY_SIGNAL_FIRE:
                    out.hooks.append(HookSite(kind="fire", hook=obj, hook_class="fire",
                                              note="signal", line=line, byte=byte))
                elif method in L.PY_TASK_FIRE:  # task.delay()/apply_async() -> fire the task
                    out.hooks.append(HookSite(kind="fire", hook=obj, hook_class="fire",
                                              note="task", line=line, byte=byte))
                elif method in L.PY_SIGNAL_CONNECT:
                    ck, cn, cc = _py_callback(_first_pos_arg(args) or _kw_arg(args, "receiver", source), source)
                    if cn:
                        out.hooks.append(HookSite(kind="listen", hook=obj, hook_class="signal", cb_kind=ck,
                                                  cb_name=cn, cb_container=cc, note="connect", line=line, byte=byte))
            elif fn.type == "identifier" and _text(fn, source) in L.PY_ROUTE_CALLS:
                pos = _pos_args(args)  # Django path(route, view) -> entry-point route
                if len(pos) >= 2:
                    route = _py_str(pos[0], source)
                    ck, cn, cc = _py_callback(pos[1], source)
                    if cn:
                        out.hooks.append(HookSite(kind="listen", hook=route or cn, hook_class="route",
                                                  cb_kind=ck, cb_name=cn, cb_container=cc, entry_point=True,
                                                  note="route", line=line, byte=byte))

    q2 = _compile(language, "(decorated_definition) @dd", out.warnings)
    if q2 is not None:
        for dd in _captures(q2, root).get("dd", []):
            defn = dd.child_by_field_name("definition")
            if defn is None or defn.type != "function_definition":
                continue
            fname = _text(defn.child_by_field_name("name"), source)
            if not fname:
                continue
            for dec in dd.children:
                if dec.type != "decorator":
                    continue
                inner = next((c for c in dec.children if c.is_named), None)  # @name or @name(...)
                if inner is None:
                    continue
                if inner.type == "call":
                    decname = _attr_tail(inner.child_by_field_name("function"), source)
                    dargs = inner.child_by_field_name("arguments")
                else:
                    decname, dargs = _attr_tail(inner, source), None
                line = dec.start_point[0] + 1
                if decname in L.PY_LISTENER_DECORATORS:  # @receiver(sig) / @receiver([sigA, sigB])
                    for sig in _signal_names(_first_pos_arg(dargs), source):
                        out.hooks.append(HookSite(kind="listen", hook=sig, hook_class="signal", cb_kind="free",
                                                  cb_name=fname, note="@receiver", line=line, byte=defn.start_byte))
                elif decname in L.PY_TASK_DECORATORS:  # @task/@shared_task/@app.task -> task on its own name
                    out.hooks.append(HookSite(kind="listen", hook=fname, hook_class="task", cb_kind="free",
                                              cb_name=fname, note="@task", line=line, byte=defn.start_byte))
                elif decname in L.PY_ROUTE_DECORATOR_METHODS:  # @app.route/@app.get(...) -> entry point
                    route = _py_str(_first_pos_arg(dargs), source) or fname
                    out.hooks.append(HookSite(kind="listen", hook=route, hook_class="route", cb_kind="free",
                                              cb_name=fname, entry_point=True, note="route",
                                              line=line, byte=defn.start_byte))


def _has_func_arg(args_node: ts.Node | None) -> bool:
    """True if a call's arguments include an inline function (arrow/function expression) -
    the signature of a component/HOC binding (`memo(()=>...)`), not a data call (`load()`)."""
    if args_node is None:
        return False
    return any(c.is_named and c.type in ("arrow_function", "function_expression")
               for c in args_node.children)


def _extract_rb_mixins(out: ParsedFile, root: ts.Node, source: bytes, language) -> None:
    """Ruby `include/extend/prepend Util` in a class body, recorded as kind='mixin'
    assigns (cls=the module) so a bare call in that class resolves through the
    module's methods (Ruby MRO) instead of falling to a same-named top-level def.
    Walks each call's own argument nodes - handles `include A, B` and `Foo::Bar`."""
    q = _compile(language, "(call method: (identifier) @mix) @call", out.warnings)
    if q is None:
        return
    for call in _captures(q, root).get("call", []):
        m = call.child_by_field_name("method")
        if m is None or _text(m, source) not in L.RUBY_MIXIN_METHODS:
            continue
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        for a in args.named_children:
            if a.type == "constant":
                mod = _text(a, source)
            elif a.type == "scope_resolution":
                mod = _text(a.child_by_field_name("name"), source)
            else:
                continue
            if mod:
                out.assigns.append(Assign(var="<mixin>", cls=mod,
                                          byte=call.start_byte, kind="mixin"))


def _extract_tsx_components(out: ParsedFile, root: ts.Node, source: bytes, language, lang: str) -> None:
    """React component graph (.tsx/.jsx/.ts).
    Gap 1: register HOC-wrapped components - `const X = React.memo((p)=>{...})` binds the
    name to a CALL RESULT, so the generic arrow-const DEF query misses it. Register the
    const when the wrapping call takes an inline function arg (memo/forwardRef/observer/…).
    Gap 2: treat a JSX element `<Component/>` as a reference (a caller edge), so the
    component tree - who renders whom - is visible. Only Capitalized names are captured
    (React components, never host elements like <div/>)."""
    container_types = L.CONTAINER_TYPES.get(lang, set())
    q = _compile(language, "(variable_declarator value: (call_expression)) @def", out.warnings)
    if q is not None:
        for d in _captures(q, root).get("def", []):
            name_node = d.child_by_field_name("name")
            val = d.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier" or val is None:
                continue
            name = _text(name_node, source)
            # Components (Capitalized - JSX requires it) and custom hooks (useXxx)
            # always register. A lowercase binding registers only for a BARE-identifier
            # wrapper (`const g = memo(fn)`): a MEMBER call like `const rows =
            # items.map(fn)` is a data pipeline, and registering those plants a phantom
            # symbol per pipeline that demotes real same-named exact edges to ambiguous.
            is_component = bool(name) and (
                name[0].isupper()
                or (name.startswith("use") and len(name) > 3 and name[3].isupper()))
            fn_node = val.child_by_field_name("function")
            if not is_component and (fn_node is None or fn_node.type != "identifier"):
                continue
            if not _has_func_arg(val.child_by_field_name("arguments")):
                continue  # plain data call (`const x = load()`) -> not a component/fn binding
            out.symbols.append(Symbol(
                name=name, kind="function",
                container=_container_of(d, container_types, source),
                start_line=d.start_point[0] + 1, end_line=d.end_point[0] + 1,
                start_byte=d.start_byte, end_byte=d.end_byte, signature=_signature(d, source)))
    if lang not in L.JSX_LANGS:  # the plain `typescript` grammar has no JSX nodes; the query
        return                   # would fail per-.ts-file ("Invalid node type"). JS/TSX only.
    for qsrc in ("(jsx_self_closing_element name: (identifier) @c)",
                 "(jsx_opening_element name: (identifier) @c)"):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        for node in _captures(q, root).get("c", []):
            name = _text(node, source)
            if name and name[0].isupper():  # Capitalized -> a component, not a host element
                out.refs.append(Ref(callee=name, receiver=None,
                                    line=node.start_point[0] + 1, byte=node.start_byte))


def _assign_var(cls_node: ts.Node, source: bytes, lang: str) -> str | None:
    """Climb from the constructor node to the nearest assignment target and return
    its variable name - pairs var<->class structurally, never by capture order. Only
    PHP's `$` sigil is stripped; in JS/TS `$el` and `el` are DISTINCT identifiers, so
    stripping there would collide same-scope locals onto one key and fabricate a wrong
    edge. _classify mirrors this normalization on the receiver side, per language."""
    p = cls_node.parent
    while p is not None:
        for fld in ("left", "name"):
            v = p.child_by_field_name(fld)
            if v is not None:
                if v.type == "variable_name":  # PHP wraps the name
                    inner = v.child_by_field_name("name")
                    if inner is not None:
                        v = inner
                name = source[v.start_byte:v.end_byte].decode("utf8", "replace")
                return name.lstrip("$") if lang == "php" else name
        p = p.parent
    return None


def _go_receiver(def_node: ts.Node, source: bytes) -> tuple[str | None, str | None]:
    recv = def_node.child_by_field_name("receiver")
    if recv is None:
        return None, None
    for ch in recv.children:
        if ch.type == "parameter_declaration":
            nm = ch.child_by_field_name("name")
            ty = ch.child_by_field_name("type")
            ty_text = source[ty.start_byte:ty.end_byte].decode("utf8", "replace").lstrip("*") if ty else None
            nm_text = source[nm.start_byte:nm.end_byte].decode("utf8", "replace") if nm else None
            return nm_text, ty_text
    return None, None


def _text(node: ts.Node | None, source: bytes) -> str | None:
    return source[node.start_byte:node.end_byte].decode("utf8", "replace") if node is not None else None


def _hint_type_name(node: ts.Node | None, source: bytes) -> str | None:
    """Bare class name from a type-annotation node, or None for cases we can't pin to
    one in-repo class: builtins/primitives, generics/subscripts, unions, array types,
    and forward-reference strings. Namespaced PHP names collapse to the bare last
    segment (symbols are stored bare). Conservative on purpose - a wrong type would
    manufacture a wrong edge."""
    if node is None:
        return None
    t = node.type
    if t in ("type", "type_annotation", "optional_type"):  # wrappers: py/ts/php(?T)
        inner = [c for c in node.children if c.is_named]
        return _hint_type_name(inner[0], source) if len(inner) == 1 else None
    if t == "named_type":                                  # php: Foo / \Ns\Foo
        return _hint_type_name(node.children[0], source) if node.children else None
    if t in ("identifier", "type_identifier", "name"):
        return _text(node, source)
    if t == "qualified_name":                              # php \Ns\Foo -> bare Foo
        names = [c for c in node.children if c.type == "name"]
        return _text(names[-1], source) if names else None
    return None  # subscript/generic_type/union_type/array_type/string/primitive -> skip


def _hint_var(node: ts.Node, source: bytes) -> str | None:
    """The variable/parameter name a type hint binds to, or None for splat/destructured
    targets (which aren't a single typed receiver)."""
    t = node.type
    if t == "typed_parameter":  # python: name is the positional first child
        first = node.children[0] if node.children else None
        return _text(first, source) if first is not None and first.type == "identifier" else None
    if t in ("typed_default_parameter", "variable_declarator"):  # python default / ts var: name field
        nm = node.child_by_field_name("name")
        return _text(nm, source) if nm is not None and nm.type == "identifier" else None
    if t == "assignment":  # python annotated local: left field
        lf = node.child_by_field_name("left")
        return _text(lf, source) if lf is not None and lf.type == "identifier" else None
    if t in ("required_parameter", "optional_parameter"):  # ts: pattern field
        pat = node.child_by_field_name("pattern")
        return _text(pat, source) if pat is not None and pat.type == "identifier" else None
    if t in ("simple_parameter", "property_promotion_parameter"):  # php: name is a variable_name ($m)
        txt = _text(node.child_by_field_name("name"), source)
        return txt.lstrip("$") if txt else None  # stored '$'-stripped, like _assign_var
    return None


def _extract_types(out: ParsedFile, root: ts.Node, source: bytes, language, lang: str) -> None:
    """Capture declared types (typed params, annotated locals) as `hint` assigns so an
    injected receiver resolves. The byte position lands inside the enclosing function/
    method, so _attribute scopes the hint to exactly where that variable is in play.
    Tier is decided in the resolver (hint -> inferred), never exact."""
    for qsrc in L.TYPE_HINTS.get(lang, []):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        for node in _captures(q, root).get("hint", []):
            var = _hint_var(node, source)
            if not var:
                continue
            cls = _hint_type_name(node.child_by_field_name("type"), source)
            # A resolvable type -> 'hint'. An UNRESOLVABLE annotation (generic List[X],
            # union X|Y, forward-ref 'X') still binds the name locally -> 'shadow', so it
            # masks a same-named module construct / global class instead of leaking it as
            # a wrong exact. (Python typed params aren't otherwise in SHADOWS.)
            out.assigns.append(Assign(var=var, cls=cls or "", byte=node.start_byte,
                                      kind="hint" if cls else "shadow"))


def _extract_shadows(out: ParsedFile, root: ts.Node, source: bytes, language, lang: str) -> None:
    """Record every locally-bound name (params, loop/with targets, assignment LHS) as a
    typeless `shadow` assign. A name bound in a scope masks any same-named global class
    or module-level construct; the resolver uses this to avoid a wrong exact + dropped
    caller when, e.g., a `def handler(db)` param shadows a module `db = Database()`."""
    for qsrc in L.SHADOWS.get(lang, []):
        q = _compile(language, qsrc, out.warnings)
        if q is None:
            continue
        for node in _captures(q, root).get("shadow", []):
            name = _text(node, source)
            if not name:
                continue
            out.assigns.append(Assign(var=name.lstrip("$") if lang == "php" else name,
                                      cls="", byte=node.start_byte, kind="shadow"))


def _attribute(pf: ParsedFile) -> None:
    """Attribute each call and each instantiation to the innermost definition whose
    byte range contains it (the enclosing symbol / scope). None => module level."""
    # Sort symbols by range size ascending so the first containing match is innermost.
    ranges = sorted(
        ((s.start_byte, s.end_byte, s) for s in pf.symbols),
        key=lambda t: t[1] - t[0],
    )
    for item in (*pf.refs, *pf.assigns, *pf.hooks):
        for start, end, sym in ranges:
            if start <= item.byte < end:
                item.src_byte = sym.start_byte
                break
