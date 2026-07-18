"""codegraph - AST-extracted symbol/call graph for any repo, queryable over MCP.

Not a hand-maintained doc: the graph is regenerated from source via tree-sitter,
incrementally by file hash, so it cannot lie about the code - it *is* the code,
re-parsed. Every edge is tagged exact/ambiguous; unresolved calls are recorded,
never hidden.
"""

__version__ = "1.0.0"
