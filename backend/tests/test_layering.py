"""Import-layering guard for backend/app.

Why this exists: an architecture review found that although the module-level
(top-of-file) import graph of ``app`` is acyclic, a number of *lazy* imports —
``from . import x`` inside a function body, usually with a comment saying it
dodges a circular import — formed cycles. Lazy cycles are legal in Python, but
they hide the real layering and let an innocent new top-level import turn into
an ImportError at boot. This test makes the layering explicit and keeps it:

* ``test_top_level_import_graph_is_acyclic`` — the module-level graph must stay
  a DAG. This is the hard invariant: a cycle here breaks ``import app``.
* ``test_leaf_layers`` — the leaf modules (pure data / helpers split out to
  break lazy cycles) may import only what LAYERS lists, so a cycle cannot
  silently return through them.
* ``test_broken_edges_stay_broken`` — the specific edges removed by the split
  (governance → rbac, pipelines → catalog) must not come back in either graph.
* ``test_report_lazy_cycles`` — prints the strongly connected components of the
  lazy graph (and of the combined graph, for information) and asserts that
  only the cycles in INTENTIONAL_LAZY_CYCLES exist. Those encode real runtime
  dependencies (see the README architecture section), not layout accidents.

Pure stdlib (``ast``) so it runs anywhere the suite runs; no third-party deps.
Run the file directly for an ad-hoc dump of both graphs.
"""
from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PKG = "app"

# Leaf layer: module → the ONLY ``app`` modules it may import (top-level or
# lazily). Pure leaves import nothing from app at all. Adding a module here is
# a promise that it stays that way; anything above may import these freely.
#
#   limits      env-driven size caps            (stdlib only)
#   policies    built-in RBAC dict              (stdlib only)
#   matching    prompt → table ranking          (stdlib only)
#   queryguard  SQL guard                       (stdlib only)
#   util        pmap and friends                (stdlib only)
#   sources     connector-or-400 helper         → connectors (the registry it wraps)
#   bootstrap   boot-time secret checks         → db, LAZILY: db imports bootstrap
#                                                 for demo_mode(), see its docstring
LAYERS: dict[str, set[str]] = {
    "limits": set(),
    "policies": set(),
    "matching": set(),
    "queryguard": set(),
    "util": set(),
    "sources": {"connectors"},
    "bootstrap": {"db"},
}

# Edges that the policies.py / matching.py split removed. If one of these
# reappears the governance ↔ rbac or pipelines → catalog → … chain is back.
BROKEN_EDGES = [
    ("governance", "rbac"),
    ("pipelines", "catalog"),
]

# Lazy (in-function) cycles that are intentional because they encode a genuine
# runtime dependency rather than an accident of file layout. Each entry is one
# strongly connected component of the lazy graph (a superset is fine: the
# assertion is that every real SCC is contained in one of these). Growing a
# component or adding one must be a deliberate decision: update this list AND
# the README architecture section.
INTENTIONAL_LAZY_CYCLES = [
    # db ⇄ bootstrap: db imports bootstrap at module level for demo_mode();
    #   bootstrap needs db only inside enforce() (runs after init_db()).
    # db → extraction: signup auto-onboards the new user's M365 documents;
    #   extraction sits above db, so db reaches it lazily and best-effort.
    # kag ⇄ extraction: kag.init_tables() registers the M365 docx/pptx parsers
    #   (plug-in registration); extraction ingests through kag.
    frozenset({"bootstrap", "db", "extraction", "kag"}),
]


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else ""


def _resolve(node: ast.AST, current: str) -> list[str]:
    """Map an Import/ImportFrom node to the top-level ``app`` module names it
    references (``connectors.demo`` → ``connectors``). Non-app imports → []."""
    names: list[str] = []
    if isinstance(node, ast.ImportFrom):
        if node.level:  # relative import: strip `level` trailing components
            parts = current.split(".")
            base = parts[: len(parts) - node.level]
            if node.module:
                names.append((base + node.module.split("."))[0])
            else:  # ``from . import a, b`` — each alias names a module
                for alias in node.names:
                    names.append((base + [alias.name])[0])
        elif node.module and node.module.startswith(PKG + "."):
            names.append(node.module.split(".")[1])
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith(PKG + "."):
                names.append(alias.name.split(".")[1])
    return [n for n in names if n]


def build_graphs() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (top_level, lazy) adjacency maps over top-level ``app`` modules.

    Top-level = imports at module scope (including inside try/if blocks at
    module scope, which still run at import time). Lazy = imports nested
    inside a function / lambda body. Sub-packages (connectors, extraction,
    viz) collapse to one node; intra-package edges are dropped.
    """
    top: dict[str, set[str]] = {}
    lazy: dict[str, set[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        name = _module_name(path)
        if not name:
            continue
        head = name.split(".")[0]
        top.setdefault(head, set())
        lazy.setdefault(head, set())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stack: list[tuple[ast.AST, bool]] = [(tree, False)]
        while stack:
            node, nested = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    for dep in _resolve(child, name):
                        if dep != head:
                            (lazy if nested else top)[head].add(dep)
                is_def = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                stack.append((child, nested or is_def))
    known = set(top)
    for g in (top, lazy):
        for k in g:
            g[k] = {d for d in g[k] if d in known}
    return top, lazy


def strongly_connected_components(graph: dict[str, set[str]]) -> list[set[str]]:
    """Tarjan's algorithm; returns only non-trivial SCCs (size > 1 or self-loop)."""
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[set[str]] = []

    def visit(v: str) -> None:
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in indices:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.add(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, ()):
                out.append(comp)

    for v in sorted(graph):
        if v not in indices:
            visit(v)
    return out


def _fmt(sccs: list[set[str]]) -> list[list[str]]:
    return [sorted(c) for c in sorted(sccs, key=lambda s: (-len(s), sorted(s)))]


def test_top_level_import_graph_is_acyclic():
    top, _ = build_graphs()
    assert not strongly_connected_components(top), (
        f"top-level import cycles in app/: {_fmt(strongly_connected_components(top))}")


def test_leaf_layers():
    top, lazy = build_graphs()
    missing = set(LAYERS) - set(top)
    assert not missing, f"LAYERS names modules missing from app/: {sorted(missing)}"
    bad = {}
    for leaf, allowed in LAYERS.items():
        extra = (top[leaf] | lazy[leaf]) - allowed
        if extra:
            bad[leaf] = sorted(extra)
    assert not bad, f"leaf modules import above their layer: {bad}"


def test_broken_edges_stay_broken():
    top, lazy = build_graphs()
    back = [(a, b) for a, b in BROKEN_EDGES if b in top[a] or b in lazy[a]]
    assert not back, f"import edge(s) removed by the leaf split have returned: {back}"


def test_report_lazy_cycles():
    top, lazy = build_graphs()
    sccs = strongly_connected_components(lazy)
    combined = {k: top[k] | lazy[k] for k in top}
    print("\nlazy-import SCCs in app/:", _fmt(sccs))
    print("combined (top-level + lazy) SCCs, for information:",
          _fmt(strongly_connected_components(combined)))
    unexpected = [sorted(c) for c in sccs
                  if not any(c <= set(i) for i in INTENTIONAL_LAZY_CYCLES)]
    assert not unexpected, (
        f"new or grown lazy-import cycle(s) in app/: {unexpected}. Either break "
        "the cycle (move the pure helper into a leaf module) or, if it encodes a "
        "real runtime dependency, add it to INTENTIONAL_LAZY_CYCLES and document "
        "it in the README architecture section."
    )


if __name__ == "__main__":  # ad-hoc inspection: python tests/test_layering.py
    top, lazy = build_graphs()
    print("top-level SCCs:", _fmt(strongly_connected_components(top)))
    print("lazy SCCs:", _fmt(strongly_connected_components(lazy)))
    print("combined SCCs:", _fmt(strongly_connected_components({k: top[k] | lazy[k] for k in top})))
    for k in sorted(lazy):
        if lazy[k]:
            print(f"  lazy {k} -> {sorted(lazy[k])}")
