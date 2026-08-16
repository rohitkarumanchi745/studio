"""Small concurrency helper — run independent operations in parallel.

Used for genuinely independent I/O: fetching table schemas, listing tables per
source, fanning a question across databases. Each connector here opens its own
connection per call (see connectors/demo.py), so concurrent read queries are
safe. Order is preserved and per-item failures yield a default, matching the
try/except the callers already had when these ran in a sequential loop.
"""
import concurrent.futures


def pmap(fn, items, workers=8, default=None):
    """Map fn over items concurrently, preserving input order. A per-item
    exception yields `default` rather than failing the batch — one slow or
    unreachable source never blocks or breaks the others."""
    items = list(items)
    if not items:
        return []
    workers = max(1, min(workers, len(items)))
    if workers == 1:
        out = []
        for it in items:
            try:
                out.append(fn(it))
            except Exception:
                out.append(default)
        return out
    out = [default] * len(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for fut in concurrent.futures.as_completed(futs):
            try:
                out[futs[fut]] = fut.result()
            except Exception:
                out[futs[fut]] = default
    return out
