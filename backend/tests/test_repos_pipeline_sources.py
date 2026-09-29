"""Selected GitHub sources stay registered, bounded and pinned to one commit."""
import base64

import pytest
from fastapi import HTTPException

from app import db, repos


ADMIN = {"id": "admin", "role": "admin", "email": "admin@example.test"}
BUILDER = {"id": "builder", "role": "viewer", "email": "builder@example.test"}
COMMIT = "a" * 40
TREE = "b" * 40


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setattr(db, "log_activity", lambda *a, **kw: None)
    repos.init_tables()


def register(url="https://github.com/example/data-pipelines", branch="main"):
    return repos.add_repo(repos.RepoIn(name="Data Pipelines", url=url,
                                       description="Sales workflow", default_branch=branch),
                          user=ADMIN)["repos"][0]["id"]


@pytest.mark.parametrize("url", [
    "http://github.com/example/repo", "https://github.com.evil.test/example/repo",
    "https://evil.test/github.com/example/repo", "https://github.com@evil.test/example/repo",
    "https://user:password@github.com/example/repo", "https://github.com:443/example/repo",
    "https://github.com/example/repo/other", "https://github.com/example/repo?ref=evil",
    "https://github.com/example/repo#fragment", "https://github.com/example/repo%2Fother",
    "https://github.com/example/repo//",
])
def test_registration_rejects_noncanonical_or_spoofed_urls(url):
    with pytest.raises(HTTPException) as exc:
        register(url)
    assert exc.value.status_code == 400
    assert repos.selectable_repos(user=BUILDER) == {"repos": []}


@pytest.mark.parametrize("branch", ["../main", "main?recursive=1", "main//other",
                                     "refs/heads/../secret", "feature/.lock", "main ", "main@{1}"])
def test_registration_rejects_unsafe_refs(branch):
    with pytest.raises(HTTPException) as exc:
        register(branch=branch)
    assert exc.value.status_code == 400


def test_builder_lists_enabled_repos_and_resolves_only_registered_id():
    repo_id = register("https://github.com/example/data-pipelines.git", "release/v1")
    listed = repos.selectable_repos(user=BUILDER)["repos"]
    assert listed == [{"id": repo_id, "name": "Data Pipelines",
                       "url": "https://github.com/example/data-pipelines",
                       "description": "Sales workflow", "default_branch": "release/v1"}]
    assert repos.selected_repo(repo_id, user=BUILDER)["repo"] == listed[0]
    with pytest.raises(HTTPException) as exc:
        repos.get_repo("https://github.com/example/data-pipelines")
    assert exc.value.status_code == 404
    with db.connect() as c:
        c.execute("UPDATE github_repos SET enabled=0 WHERE id=?", (repo_id,))
        c.commit()
    assert repos.selectable_repos(user=BUILDER) == {"repos": []}
    with pytest.raises(HTTPException) as exc:
        repos.get_repo(repo_id)
    assert exc.value.status_code == 404


def test_legacy_unsafe_registry_row_is_not_selectable():
    repo_id = register()
    with db.connect() as c:
        c.execute("UPDATE github_repos SET url=? WHERE id=?",
                  ("https://github.com.evil.test/example/repo", repo_id))
        c.commit()
    assert repos.selectable_repos(user=BUILDER) == {"repos": []}
    with pytest.raises(HTTPException) as exc:
        repos.get_repo(repo_id)
    assert exc.value.status_code == 422


def test_context_is_pinned_bounded_and_redacts_credentials(monkeypatch):
    repo_id = register(branch="release/v1")
    paths = []
    blobs = {}
    tree = []
    for index in range(8):
        path = f"dags/{index}.py"
        raw = ("API_KEY = 'should-never-appear'\nprint('safe plan')\n"
               if index == 0 else f"print('step {index}')\n").encode()
        sha = f"{index + 1:040x}"
        tree.append({"path": path, "sha": sha, "size": len(raw),
                     "type": "blob", "mode": "100644"})
        blobs[sha] = {"sha": sha, "size": len(raw), "encoding": "base64",
                      "content": base64.b64encode(raw).decode()}
    tree.extend([
        {"path": "dags/secrets.py", "sha": "c" * 40, "size": 10,
         "type": "blob", "mode": "100644"},
        {"path": "other/arbitrary.py", "sha": "d" * 40, "size": 10,
         "type": "blob", "mode": "100644"},
        {"path": "dags/link.py", "sha": "e" * 40, "size": 10,
         "type": "blob", "mode": "120000"},
        {"path": "dags/huge.sql", "sha": "f" * 40, "size": repos.MAX_FILE_BYTES + 1,
         "type": "blob", "mode": "100644"},
    ])

    def fake_api(path, max_bytes):
        paths.append(path)
        if "/commits/" in path:
            return {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
        if "/git/trees/" in path:
            return {"sha": TREE, "truncated": False, "tree": tree}
        return blobs[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(repos, "_api_json", fake_api)
    text, evidence = repos.planning_context(repo_id, BUILDER)
    assert paths[:2] == ["/repos/example/data-pipelines/commits/release%2Fv1",
                         f"/repos/example/data-pipelines/git/trees/{TREE}?recursive=1"]
    assert len(evidence["files"]) == repos.MAX_FILES
    assert all(f["path"].startswith("dags/") for f in evidence["files"])
    assert evidence["ref"] == evidence["commit_sha"] == COMMIT
    assert evidence["tree_sha"] == TREE
    assert "[REDACTED CREDENTIAL LINE]" in text
    assert "should-never-appear" not in text
    assert "step 6" not in text
    assert "secrets.py" not in text
    assert "Untrusted GitHub" in text
    assert not any("secret" in str(item).lower() for item in evidence["files"])


def test_selected_repo_without_eligible_files_fails_closed(monkeypatch):
    repo_id = register()

    def fake_api(path, max_bytes):
        if "/commits/" in path:
            return {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
        return {"sha": TREE, "truncated": False, "tree": [
            {"path": "README.md", "sha": "c" * 40, "size": 20,
             "type": "blob", "mode": "100644"}]}

    monkeypatch.setattr(repos, "_api_json", fake_api)
    with pytest.raises(HTTPException) as exc:
        repos.planning_context(repo_id)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("tree_response", [
    {"sha": TREE, "truncated": True, "tree": []},
    {"sha": "c" * 40, "truncated": False, "tree": []},
    {"sha": TREE, "tree": []},
])
def test_incomplete_or_wrong_tree_fails_closed(monkeypatch, tree_response):
    repo_id = register()

    def fake_api(path, max_bytes):
        if "/commits/" in path:
            return {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
        return tree_response

    monkeypatch.setattr(repos, "_api_json", fake_api)
    with pytest.raises(HTTPException) as exc:
        repos.planning_context(repo_id)
    assert exc.value.status_code == 502


def test_malformed_commit_fails_as_source_error_not_server_error(monkeypatch):
    repo_id = register()
    monkeypatch.setattr(repos, "_api_json", lambda path, max_bytes:
                        {"sha": COMMIT, "commit": "malformed"})
    with pytest.raises(HTTPException) as exc:
        repos.planning_context(repo_id)
    assert exc.value.status_code == 502


def test_redaction_cannot_expand_context_past_limit(monkeypatch):
    repo_id = register()
    content = ("token\n" * 2000).encode()
    sha = "c" * 40

    def fake_api(path, max_bytes):
        if "/commits/" in path:
            return {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
        if "/git/trees/" in path:
            return {"sha": TREE, "truncated": False, "tree": [
                {"path": "dags/plan.py", "sha": sha, "size": len(content),
                 "type": "blob", "mode": "100644"}]}
        return {"sha": sha, "size": len(content), "encoding": "base64",
                "content": base64.b64encode(content).decode()}

    monkeypatch.setattr(repos, "_api_json", fake_api)
    with pytest.raises(HTTPException) as exc:
        repos.planning_context(repo_id)
    assert exc.value.status_code == 502


def test_transport_errors_never_include_token_or_raw_exception(monkeypatch):
    class BrokenOpener:
        def open(self, request, timeout):
            raise RuntimeError("Bearer SUPER_SECRET should not be returned")

    handlers = []

    def opener(*args):
        handlers.extend(args)
        return BrokenOpener()

    monkeypatch.setenv("GITHUB_TOKEN", "SUPER_SECRET")
    monkeypatch.setattr(repos.urllib.request, "build_opener", opener)
    with pytest.raises(repos.RepositorySourceError) as exc:
        repos._api_json("/repos/example/repo/commits/main", 100)
    assert "SUPER_SECRET" not in str(exc.value)
    assert any(isinstance(handler, repos._NoRedirect) for handler in handlers)
    assert any(isinstance(handler, repos.urllib.request.ProxyHandler) for handler in handlers)
