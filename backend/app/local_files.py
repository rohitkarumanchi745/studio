"""Files from a folder on the machine Studio runs on, for agents to read.

An administrator names one folder (STUDIO_LOCAL_FILES_DIR). Agents working for
an admin or analyst can then list it, search it and read a file in it: text,
Markdown, CSV, JSON, code, PDF, Excel and Word. They can never write. It is off
when the folder is not set, so a cloud deployment does not have it; on your own
machine it lets "what does my notes file say about…" be answered.

Reading stays inside that folder: every path is resolved (symlinks included)
and must land under it; hidden names (.env, .git, .ssh…) are never listed or
read; files above a size cap are refused. On macOS, a file iCloud has
offloaded (only a placeholder on disk) is reported instead of read, since
reading it would wait for a download.
"""
import io
import os
import re
import stat
import time

from . import settings

_SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)    # macOS: content not on disk
# Read as plain text by default; STUDIO_LOCAL_FILES_TEXT_TYPES adds more.
_TEXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".yaml", ".yml",
         ".xml", ".html", ".htm", ".sql", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go",
         ".rs", ".rb", ".sh", ".ini", ".cfg", ".toml", ".r", ".ipynb", ".css", ".tex", ".rst"}
# Documents with a reader of their own.
_DOCS = {".pdf", ".xlsx", ".docx", ".eml"}
_ROLES = ("admin", "analyst", "viewer")


# ── Settings (see backend/.env.example) ─────────────────────────────────

def max_read_bytes():
    return settings.integer("STUDIO_LOCAL_FILES_MAX_MB", 10, 1, 200) * 1024 * 1024


def max_text_chars():
    return settings.integer("STUDIO_LOCAL_FILES_MAX_CHARS", 16000, 1000, 500_000)


def roles():
    """Who may use the folder (STUDIO_LOCAL_FILES_ROLES): admin and analyst by default."""
    return tuple(settings.names("STUDIO_LOCAL_FILES_ROLES", ("admin", "analyst"), allowed=_ROLES))


def text_types():
    extra = {("." + x.lstrip(".")) for x in settings.names("STUDIO_LOCAL_FILES_TEXT_TYPES", ())
             if re.fullmatch(r"\.?[a-z0-9]{1,10}", x)}
    return _TEXT | extra


def readable():
    """The kinds of file read: all supported, or STUDIO_LOCAL_FILES_TYPES (a
    subset of them, e.g. "md,txt,pdf")."""
    supported = text_types() | _DOCS
    picked = {("." + x.lstrip(".")) for x in settings.names("STUDIO_LOCAL_FILES_TYPES", ())}
    return (picked & supported) or supported


def root():
    """The shared folder, resolved, or None when none is set or it is missing."""
    raw = os.getenv("STUDIO_LOCAL_FILES_DIR", "").strip()
    if not raw:
        return None
    path = os.path.realpath(os.path.expanduser(raw))
    return path if os.path.isdir(path) else None


def available(user):
    return bool(root()) and (user or {}).get("role") in roles()


def _hidden(rel):
    """A dot-name anywhere in the path (.env, .git…). "." and ".." are not
    names: where they lead is checked against the folder itself."""
    return any(part.startswith(".") and part not in (".", "..")
               for part in rel.replace("\\", "/").split("/") if part)


def resolve(rel):
    """(absolute path, relative path) inside the folder; ValueError otherwise."""
    base = root()
    if not base:
        raise ValueError("No shared folder is set on this server.")
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if _hidden(rel):
        raise ValueError("Hidden files are not shared.")
    path = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([base, path]) != base:
        raise ValueError("That path is outside the shared folder.")
    return path, os.path.relpath(path, base).replace(os.sep, "/")


def _offloaded(st):
    return bool(getattr(st, "st_flags", 0) & _SF_DATALESS)


def _walk(base, start):
    """(absolute, relative, stat) of each visible regular file under `start`,
    never following a link out of the folder."""
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            real = os.path.realpath(path)
            if os.path.commonpath([base, real]) != base:
                continue
            try:
                st = os.stat(real)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield real, os.path.relpath(path, base).replace(os.sep, "/"), st


def list_files(folder=""):
    """Up to STUDIO_LOCAL_FILES_LIST_MAX files under a subfolder: path, size, modified."""
    path, rel = resolve(folder)
    if not os.path.isdir(path):
        raise ValueError(f"“{folder}” is not a folder in the shared folder.")
    out = []
    for _, rel_path, st in _walk(root(), path):
        out.append({"path": rel_path, "bytes": st.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    **({"offloaded": True} if _offloaded(st) else {})})
        if len(out) >= settings.integer("STUDIO_LOCAL_FILES_LIST_MAX", 200, 10, 10_000):
            break
    return out


def _text_of(path, name, data):
    ext = os.path.splitext(name)[1].lower()
    if ext in text_types():
        return data.decode("utf-8", "replace")
    if ext == ".docx":
        import docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    from . import kag
    _, chunks = kag.parse(name, data)
    parts = []
    for ch in chunks:
        meta = ch.get("meta") or {}
        where = (f"[page {meta['page']}] " if meta.get("page")
                 else f"[sheet {meta['sheet']}] " if meta.get("sheet") else "")
        parts.append(where + ch["text"])
    return "\n".join(parts)


def read_file(rel):
    """{path, text} of one file in the folder; ValueError with a sentence."""
    path, rel = resolve(rel)
    if not os.path.isfile(path):
        raise ValueError(f"There is no file “{rel}” in the shared folder.")
    ext = os.path.splitext(path)[1].lower()
    if ext not in readable():
        raise ValueError(f"{ext or 'That kind of'} file cannot be read as text.")
    st = os.stat(path)
    if _offloaded(st):
        raise ValueError(f"“{rel}” is in iCloud, not on this Mac — download it first.")
    limit = max_read_bytes()
    if st.st_size > limit:
        raise ValueError(f"“{rel}” is larger than {limit // (1024 * 1024)} MB.")
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)
    try:
        text = _text_of(path, os.path.basename(path), data).strip()
    except Exception:
        raise ValueError(f"“{rel}” could not be read.") from None
    cap = max_text_chars()
    if len(text) > cap:
        text = text[:cap].rstrip() + f"\n… ({len(text) - cap} more characters)"
    return {"path": rel, "text": text}


def search(words, limit=None):
    """Files whose name or text holds every word: [{path, line}] with the
    first matching line. Text files only (the documents are searched by name),
    within a file and time budget."""
    limit = limit or settings.integer("STUDIO_LOCAL_FILES_SEARCH_HITS", 20, 1, 500)
    max_files = settings.integer("STUDIO_LOCAL_FILES_SEARCH_FILES", 3000, 100, 200_000)
    max_seconds = settings.number("STUDIO_LOCAL_FILES_SEARCH_SECONDS", 6.0, 1.0, 120.0)
    file_bytes = settings.integer("STUDIO_LOCAL_FILES_SEARCH_FILE_MB", 1, 1, 100) * 1024 * 1024
    terms = [t.lower() for t in re.findall(r"\w[\w.-]*", str(words or ""))][:8]
    if not terms:
        raise ValueError("Give words to search for.")
    base = root()
    if not base:
        raise ValueError("No shared folder is set on this server.")
    out, started, seen = [], time.monotonic(), 0
    for path, rel, st in _walk(base, base):
        seen += 1
        if seen > max_files or time.monotonic() - started > max_seconds:
            break
        name = rel.lower()
        if all(t in name for t in terms):
            out.append({"path": rel, "line": ""})
        elif (os.path.splitext(rel)[1].lower() in text_types() and st.st_size <= file_bytes
              and not _offloaded(st)):
            try:
                with open(path, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            low = text.lower()
            if all(t in low for t in terms):
                line = next((ln.strip() for ln in text.splitlines() if terms[0] in ln.lower()), "")
                out.append({"path": rel, "line": line[:200]})
        if len(out) >= limit:
            break
    return out


def tools(user, record, emit=None):
    """LangChain tools over the folder for this user, or [] (not set, or a
    role that may not read it). `record` collects the files read."""
    if not available(user):
        return []
    from langchain_core.tools import tool
    say = emit or (lambda label: None)

    @tool
    def list_local_files(folder: str = "") -> str:
        """List files in the shared folder on this machine (or a subfolder of it).

        Args:
            folder: A subfolder, relative to the shared folder; empty for all.
        """
        say(f"listed the shared folder{' ' + folder if folder else ''}")
        try:
            files = list_files(folder)
        except ValueError as e:
            return f"CANNOT LIST: {e}"
        return "\n".join(f"{f['path']} ({f['bytes']} bytes, modified {f['modified']})"
                         + (" [in iCloud only]" if f.get("offloaded") else "")
                         for f in files) or "(no files)"

    @tool
    def search_local_files(words: str) -> str:
        """Find files in the shared folder whose name or text contains all of
        these words.

        Args:
            words: The words to look for.
        """
        say(f"searched the shared folder for “{str(words)[:80]}”")
        try:
            hits = search(words)
        except ValueError as e:
            return f"CANNOT SEARCH: {e}"
        return "\n".join(f"{h['path']}" + (f": {h['line']}" if h["line"] else "")
                         for h in hits) or "(no matching files)"

    @tool
    def read_local_file(path: str) -> str:
        """Read one file from the shared folder as text (text, Markdown, CSV,
        JSON, code, PDF, Excel, Word). Cite the file you used.

        Args:
            path: The file's path relative to the shared folder.
        """
        say(f"read the file {str(path)[:120]}")
        try:
            got = read_file(path)
        except ValueError as e:
            return f"CANNOT READ: {e}"
        if got["path"] not in record:
            record.append(got["path"])
        return got["text"] or "(the file is empty)"

    return [list_local_files, search_local_files, read_local_file]


FILES_RULE = ("Files on this machine: list_local_files, search_local_files and read_local_file "
              "read the shared folder. Use them when the question is about the user's files "
              "or notes, and say which file you used.")
