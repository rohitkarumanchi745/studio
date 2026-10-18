"""Explicit planning sources are bounded, validated, and never silently ignored."""

import pytest
from fastapi import HTTPException

from app import confluence, pipeline_sources, repos


USER = {"id": "analyst", "role": "analyst"}


@pytest.mark.parametrize("page_ids", [["0"], ["1", "1"], ["1", "2", "3", "4"],
                                      ["https://example.atlassian.net/wiki/123"]])
def test_bad_page_selection_is_rejected_before_a_fetch(page_ids):
    with pytest.raises(HTTPException) as error:
        pipeline_sources.validate_selection(confluence_page_ids=page_ids)
    assert error.value.status_code == 400


def test_selected_source_fetch_failure_does_not_fall_back_to_prompt_only(monkeypatch):
    monkeypatch.setattr(repos, "get_repo", lambda repo_id: {"id": repo_id})

    def unavailable(*args):
        raise HTTPException(502, "Selected GitHub repository could not be read completely")

    monkeypatch.setattr(repos, "planning_context", unavailable)
    with pytest.raises(HTTPException) as error:
        pipeline_sources.resolve(USER, repository_id="registered")
    assert error.value.status_code == 502


def test_selected_pages_must_resolve_exact_ids(monkeypatch):
    monkeypatch.setattr(confluence, "resolve_pages", lambda ids, user: [{
        "id": "999", "text": "Wrong page", "version": 1,
    }])
    with pytest.raises(HTTPException) as error:
        pipeline_sources.resolve(USER, confluence_page_ids=["123"])
    assert error.value.status_code == 502


def test_context_cap_and_provenance_excludes_page_text(monkeypatch):
    monkeypatch.setattr(confluence, "resolve_pages", lambda ids, user: [{
        "provider": "confluence", "id": "123", "title": "Runbook", "space_key": "OPS",
        "version": 2, "sha256": "a" * 64, "url": "https://example.atlassian.net/wiki/123",
        "text": "Small reference",
    }])
    context, provenance = pipeline_sources.resolve(USER, confluence_page_ids=["123"])
    assert "Small reference" in context and "untrusted data" in context
    assert provenance["confluence_pages"][0]["version"] == 2
    assert "text" not in provenance["confluence_pages"][0]

    monkeypatch.setattr(confluence, "resolve_pages", lambda ids, user: [{
        "provider": "confluence", "id": "123", "text": "x" * 70000,
    }])
    with pytest.raises(HTTPException) as error:
        pipeline_sources.resolve(USER, confluence_page_ids=["123"])
    assert error.value.status_code == 413


def test_source_selection_routes_are_mounted_once():
    from app import main

    # FastAPI versions may keep included routers lazy rather than flattening
    # them into app.routes. The OpenAPI paths are the public contract.
    paths = main.app.openapi()["paths"]
    assert "/api/repos" in paths
    assert "/api/confluence/pages" in paths
    # Read-only pipelines and Pipeline flow were removed; their routes stay gone.
    assert not [p for p in paths if p.startswith(("/api/pipelines", "/api/flow"))]
    assert main._ROUTERS.count(repos.router) == 1
    assert main._ROUTERS.count(confluence.router) == 1
