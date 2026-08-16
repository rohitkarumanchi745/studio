"""GitHub repository registry + prompt-based selection.

Scripts often live in GitHub repos. An admin registers the repos (name, url,
description); given a prompt, `pick` scores them against the request — the same
deterministic term-overlap match used for tables — and returns the best repo,
so a pipeline built from a prompt pulls scripts from the right place. When a
GITHUB_TOKEN is present, the chosen repo's file tree can be fetched as context.
"""
import json
import os
import re
import time
import urllib.request
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/repos", tags=["repos"])
_settings = APIRouter(prefix="/settings/repos", tags=["repos"])


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS github_repos (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            description TEXT,
            default_branch TEXT DEFAULT 'main',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_repo_name ON github_repos(name);
        """
    )
    c.commit()
    c.close()


def _all():
    c = db._conn()
    rows = c.execute("SELECT * FROM github_repos WHERE enabled=1 ORDER BY created_at").fetchall()
    c.close()
    return [dict(r) for r in rows]


_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "by", "with",
         "from", "our", "data", "pipeline", "build", "run", "script", "scripts"}


def _tokens(text):
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t and t not in _STOP}


def pick(prompt, skill_terms=None):
    """Best-matching registered repo for a prompt, plus the ranked list. Scores
    term overlap of the prompt (and optional skill-file terms) against each
    repo's name + description + url."""
    want = _tokens(prompt) | set(skill_terms or [])
    ranked = []
    for r in _all():
        hay = _tokens(f"{r['name']} {r.get('description', '')} {r['url']}")
        score = len(want & hay)
        ranked.append({**r, "score": score})
    ranked.sort(key=lambda x: -x["score"])
    best = ranked[0] if ranked and ranked[0]["score"] > 0 else (ranked[0] if ranked else None)
    return best, ranked


def file_tree(repo, limit=40):
    """List script files in the repo (best-effort; needs GITHUB_TOKEN for
    private repos). Returns [] on any failure — selection still works."""
    token = os.getenv("GITHUB_TOKEN", "")
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", repo["url"])
    if not m:
        return []
    owner, name = m.group(1), m.group(2).replace(".git", "")
    branch = repo.get("default_branch") or "main"
    url = f"https://api.github.com/repos/{owner}/{name}/git/trees/{branch}?recursive=1"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tree = json.loads(r.read().decode()).get("tree", [])
    except Exception:
        return []
    scripts = [t["path"] for t in tree
               if t.get("type") == "blob" and t["path"].endswith((".py", ".sql", ".sh", ".scala"))]
    return scripts[:limit]


# ── Admin registry ──────────────────────────────────────────────────────

class RepoIn(BaseModel):
    name: str
    url: str
    description: str | None = None
    default_branch: str = "main"


def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Repositories are admin-only")


@_settings.get("")
def list_repos(user=Depends(current_user)):
    _admin(user)
    return {"repos": _all()}


@_settings.post("", status_code=201)
def add_repo(body: RepoIn, user=Depends(current_user)):
    _admin(user)
    name = body.name.strip()
    if not name or "github.com" not in body.url:
        raise HTTPException(400, "name and a github.com url are required")
    c = db._conn()
    c.execute("DELETE FROM github_repos WHERE name=?", (name,))
    c.execute("INSERT INTO github_repos (id, name, url, description, default_branch, enabled, created_at) "
              "VALUES (?,?,?,?,?,?,?)",
              (str(uuid.uuid4()), name, body.url.strip(), body.description,
               body.default_branch or "main", 1, time.time()))
    c.commit()
    c.close()
    db.log_activity(user, "repo_register", prompt=name)
    return {"repos": _all()}


@_settings.delete("/{name}")
def remove_repo(name: str, user=Depends(current_user)):
    _admin(user)
    c = db._conn()
    c.execute("DELETE FROM github_repos WHERE name=?", (name,))
    c.commit()
    c.close()
    return {"repos": _all()}


class PickIn(BaseModel):
    prompt: str


@router.post("/pick")
def pick_repo(body: PickIn, user=Depends(current_user)):
    """Choose the repo whose scripts best fit the prompt."""
    best, ranked = pick(body.prompt)
    if not best:
        return {"repo": None, "ranked": [], "files": []}
    return {"repo": {k: best[k] for k in ("name", "url", "description", "default_branch")},
            "score": best["score"],
            "ranked": [{"name": r["name"], "score": r["score"]} for r in ranked],
            "files": file_tree(best)}
