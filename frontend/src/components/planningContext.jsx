// Planning context for an Airflow DAG drafted in chat: the registered
// repositories and Confluence pages an analyst may attach, the picker that
// selects them, and the provenance card shown on the draft.
import { useEffect, useState } from "react";
import { api, getUser } from "../api";

export function planningContextFields(repositoryId, confluencePageIds) {
  const pages = Array.isArray(confluencePageIds)
    ? [...new Set(confluencePageIds
        .filter((id) => typeof id === "string")
        .map((id) => id.trim())
        .filter(Boolean))].slice(0, 3)
    : [];
  return {
    repository_id: typeof repositoryId === "string" && repositoryId.trim()
      ? repositoryId.trim()
      : null,
    confluence_page_ids: pages,
  };
}

export function chatPipelineFields(buildAirflow, repositoryId, confluencePageIds) {
  if (!buildAirflow) return {};
  return {
    pipeline_action: "build",
    pipeline_mode: "airflow_dag",
    ...planningContextFields(repositoryId, confluencePageIds),
  };
}

export function usePlanningCatalog() {
  const [planningAllowed] = useState(() => ["admin", "analyst"].includes(getUser()?.role));
  const [repositories, setRepositories] = useState([]);
  const [repositoryStatus, setRepositoryStatus] = useState(
    () => planningAllowed ? "loading" : "forbidden");
  const [confluencePages, setConfluencePages] = useState([]);
  const [confluenceStatus, setConfluenceStatus] = useState(
    () => planningAllowed ? "loading" : "forbidden");
  const [confluenceStart, setConfluenceStart] = useState(0);
  const [confluenceHasMore, setConfluenceHasMore] = useState(false);
  const [confluenceLoadingMore, setConfluenceLoadingMore] = useState(false);

  useEffect(() => {
    if (!planningAllowed) return undefined;
    let active = true;
    api("/repos")
      .then((data) => {
        if (!active) return;
        setRepositories(Array.isArray(data?.repos) ? data.repos : []);
        setRepositoryStatus("ready");
      })
      .catch(() => {
        if (!active) return;
        setRepositories([]);
        setRepositoryStatus("unavailable");
      });
    // Confluence is optional. An unconfigured connector must leave pipeline
    // building available instead of turning the whole composer into an error.
    api("/confluence/pages")
      .then((data) => {
        if (!active) return;
        const pages = Array.isArray(data?.pages) ? data.pages : [];
        setConfluencePages(pages);
        setConfluenceStart(0);
        setConfluenceHasMore(pages.length >= 25);
        setConfluenceStatus("ready");
      })
      .catch(() => {
        if (!active) return;
        setConfluencePages([]);
        setConfluenceHasMore(false);
        setConfluenceStatus("unconfigured");
      });
    return () => { active = false; };
  }, [planningAllowed]);

  async function loadMoreConfluence() {
    if (!planningAllowed || confluenceStatus !== "ready"
        || !confluenceHasMore || confluenceLoadingMore) return;
    setConfluenceLoadingMore(true);
    try {
      const nextStart = confluenceStart + 25;
      const data = await api(`/confluence/pages?start=${nextStart}&limit=25`);
      const next = Array.isArray(data?.pages) ? data.pages : [];
      setConfluencePages((current) => {
        const seen = new Set(current.map((page) => String(page.id)));
        return [...current, ...next.filter((page) => {
          const id = String(page?.id ?? "");
          if (!id || seen.has(id)) return false;
          seen.add(id);
          return true;
        })];
      });
      setConfluenceStart(nextStart);
      setConfluenceHasMore(next.length >= 25);
    } catch {
      // A later page is optional catalog navigation. Keep the already loaded
      // choices usable and stop offering a control that cannot make progress.
      setConfluenceHasMore(false);
    } finally {
      setConfluenceLoadingMore(false);
    }
  }

  return { planningAllowed, repositories, repositoryStatus, confluencePages,
    confluenceStatus, confluenceHasMore, confluenceLoadingMore,
    loadMoreConfluence };
}

export function PlanningContextPicker({ repositories = [], repositoryStatus = "ready",
  confluencePages = [], confluenceStatus = "ready", repositoryId = "",
  confluencePageIds = [], onRepositoryChange, onConfluenceChange, disabled = false,
  preview = false, planningAllowed = true, confluenceHasMore = false,
  confluenceLoadingMore = false, loadMoreConfluence }) {
  if (!planningAllowed) {
    return (
      <div className="meta" style={{ margin: "8px 0" }}>
        Repository and Confluence planning context is available to analysts and administrators.
      </div>
    );
  }
  const pageIds = new Set(confluencePageIds);
  return (
    <fieldset className="pl-draft" style={{ margin: "8px 0", padding: 10 }}>
      <legend>Optional planning context</legend>
      <div className="meta">
        {preview
          ? "Availability preview only — selections here are not saved. Builders choose context per draft."
          : "Repository files and Confluence text can inform this draft. They are read-only planning context, not code execution; the existing review, approval, and run path still applies."}
      </div>
      <div className="job-form-row" style={{ marginTop: 8, alignItems: "flex-start" }}>
        <label className="meta" style={{ display: "grid", gap: 4, minWidth: 220 }}>
          Repository
          <select aria-label="Repository planning context" value={repositoryId}
            disabled={disabled || repositoryStatus === "loading"}
            onChange={(event) => onRepositoryChange?.(event.target.value)}>
            <option value="">No repository context</option>
            {repositories.map((repo) => (
              <option key={repo.id} value={repo.id}>
                {repo.name}{repo.default_branch ? ` · ${repo.default_branch}` : ""}
              </option>
            ))}
          </select>
          {repositoryStatus === "unavailable" && (
            <span>Repository planning context is unavailable; building can continue.</span>
          )}
        </label>
        <label className="meta" style={{ display: "grid", gap: 4, minWidth: 280, flex: 1 }}>
          Confluence pages (up to 3)
          <select multiple aria-label="Confluence page planning context"
            value={[...pageIds]} size={Math.min(5, Math.max(2, confluencePages.length || 2))}
            disabled={disabled || confluenceStatus !== "ready" || confluencePages.length === 0}
            onChange={(event) => onConfluenceChange?.(
              Array.from(event.currentTarget.selectedOptions, (option) => option.value).slice(0, 3))}>
            {confluencePages.length === 0 && <option value="" disabled>No pages available</option>}
            {confluencePages.map((page) => (
              <option key={page.id} value={page.id}>
                {page.title}{page.space_key ? ` · ${page.space_key}` : ""}
                {page.version != null ? ` · v${page.version}` : ""}
              </option>
            ))}
          </select>
          {confluenceStatus === "unconfigured" && (
            <span>Confluence is not configured; repository-only planning can continue.</span>
          )}
          {confluenceStatus === "ready" && confluencePages.length === 0 && (
            <span>No Confluence pages are currently available.</span>
          )}
          {confluencePageIds.length > 0 && (
            <span>{confluencePageIds.length} page{confluencePageIds.length === 1 ? "" : "s"} selected</span>
          )}
          {confluenceHasMore && (
            <button type="button" className="chip" onClick={loadMoreConfluence}
              disabled={disabled || confluenceLoadingMore}>
              {confluenceLoadingMore ? "loading more…" : "Load more Confluence pages"}
            </button>
          )}
        </label>
      </div>
    </fieldset>
  );
}

function safeHttpUrl(value) {
  try {
    const parsed = new URL(value);
    return ["https:", "http:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

export function PlanningSources({ sources }) {
  const repoSource = sources?.github_repository;
  // Accept the documented flat provenance and the server's snapshot envelope
  // (`repo` + immutable ref/files) while deployments roll forward.
  const repo = repoSource?.repo || repoSource;
  const repoRef = repoSource?.ref || repo?.ref;
  const repoFiles = Array.isArray(repoSource?.files) ? repoSource.files : repo?.files;
  const pages = Array.isArray(sources?.confluence_pages) ? sources.confluence_pages : [];
  if (!repo && pages.length === 0) return null;
  const repoUrl = safeHttpUrl(repo?.url);
  return (
    <div className="pl-draft" aria-label="Pipeline planning provenance">
      <div className="meta">
        <b>Planning context</b> — selected repository files and Confluence text informed
        this draft; none was executed as code. Execution still follows the existing
        review, approval, and run controls.
      </div>
      {repo && (
        <div className="meta">
          📦 {repoUrl
            ? <a href={repoUrl} target="_blank" rel="noreferrer">{repo.name}</a>
            : repo.name}
          {repoRef ? ` · ${repoRef}` : ""}
          {Array.isArray(repoFiles) ? ` · ${repoFiles.length} referenced file${repoFiles.length === 1 ? "" : "s"}` : ""}
        </div>
      )}
      {pages.length > 0 && (
        <div className="inputs-row">
          <span className="meta">Confluence:</span>
          {pages.map((page) => {
            const pageUrl = safeHttpUrl(page.url);
            const label = `${page.title}${page.space_key ? ` · ${page.space_key}` : ""}${page.version != null ? ` · v${page.version}` : ""}`;
            return pageUrl
              ? <a key={page.id} className="chip chip-input" href={pageUrl} target="_blank" rel="noreferrer">📄 {label}</a>
              : <span key={page.id} className="chip chip-input">📄 {label}</span>;
          })}
        </div>
      )}
    </div>
  );
}
