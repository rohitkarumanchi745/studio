// Prompt-built, access-verified pipelines. Describe what you want; the agent
// routes it to the source you can access whose tables match, drafts verified
// steps, and lets you save + trigger. A failed run emails you the failing
// step; every run is traced through Agent Lightning.
//
// Honesty rule for this view: a step's badge is DERIVED from the step, never
// stamped on. `draft.steps` holds only what actually verified (RBAC + guard +
// a real execution) and `draft.dropped` holds every failure WITH its error —
// so a step that never ran can never appear here wearing a green tick, and
// with nothing verified Save is off and says why.
import { useEffect, useState } from "react";
import { api, getUser } from "../api";
import Lineage from "./Lineage";

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

export function chatPipelineFields(buildPipeline, buildAirflow, repositoryId, confluencePageIds) {
  if (!buildPipeline && !buildAirflow) return {};
  return {
    pipeline_action: "build",
    pipeline_mode: buildAirflow ? "airflow_dag" : "read_only_sql",
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
        setConfluencePages(Array.isArray(data?.pages) ? data.pages : []);
        setConfluenceStatus("ready");
      })
      .catch(() => {
        if (!active) return;
        setConfluencePages([]);
        setConfluenceStatus("unconfigured");
      });
    return () => { active = false; };
  }, [planningAllowed]);

  return { planningAllowed, repositories, repositoryStatus, confluencePages,
    confluenceStatus };
}

export function PlanningContextPicker({ repositories = [], repositoryStatus = "ready",
  confluencePages = [], confluenceStatus = "ready", repositoryId = "",
  confluencePageIds = [], onRepositoryChange, onConfluenceChange, disabled = false,
  preview = false, planningAllowed = true }) {
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

// One drafted step. Works for both lists: the badge, the tone and the reason
// all come off the step itself. `intent_warnings` (e.g. the request said
// "monthly" and the SQL buckets by day) warn — they never hide the step.
function Step({ s }) {
  const warnings = s.intent_warnings || [];
  return (
    <div className="pl-step">
      <div className="pl-step-head">
        <span className="query-badge" style={{ color: s.verified ? "var(--ok)" : "var(--bad)" }}>
          {s.verified ? "✓ verified" : `✗ failed: ${s.error || "did not verify"}`}
        </span>
        <b>{s.name}</b>
        <span className="meta">
          {s.source}
          {s.table ? `/${s.table}` : ""}
          {s.verified ? ` · ${s.row_count} rows` : ""}
        </span>
        {warnings.map((w, i) => (
          <span key={i} className="query-badge" style={{ color: "var(--warn)" }}>
            ⚠ {w}
          </span>
        ))}
      </div>
      <pre className="query-sql">{s.sql}</pre>
    </div>
  );
}

export default function Pipelines({ onClose }) {
  const [list, setList] = useState(null);
  const [agl, setAgl] = useState(false);
  const [error, setError] = useState("");

  // Build panel
  const [prompt, setPrompt] = useState("");
  const [building, setBuilding] = useState(false);
  const [draft, setDraft] = useState(null); // {source, matched_tables, steps, dropped}
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [repositoryId, setRepositoryId] = useState("");
  const [confluencePageIds, setConfluencePageIds] = useState([]);
  const planningCatalog = usePlanningCatalog();

  // Per-pipeline run state
  const [runs, setRuns] = useState({}); // pid -> {loading|list}
  const [running, setRunning] = useState("");

  const load = () =>
    api("/pipelines")
      .then((d) => {
        setList(d.pipelines || []);
        setAgl(!!d.agent_lightning);
      })
      .catch((e) => {
        setError(e.message);
        setList([]);
      });

  useEffect(() => {
    load();
  }, []);

  async function build() {
    if (!prompt.trim() || building) return;
    setBuilding(true);
    setError("");
    setDraft(null);
    try {
      const d = await api("/pipelines/build", {
        method: "POST",
        body: JSON.stringify({
          prompt: prompt.trim(),
          ...planningContextFields(repositoryId, confluencePageIds),
        }),
      });
      setDraft(d);
      setName(prompt.trim().slice(0, 80));
    } catch (e) {
      setError(e.message);
    } finally {
      setBuilding(false);
    }
  }

  async function save() {
    if (!draft || saving) return;
    setSaving(true);
    setError("");
    try {
      await api("/pipelines", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim() || draft.prompt,
          prompt: draft.prompt,
          source: draft.source,
          steps: draft.steps,
        }),
      });
      setDraft(null);
      setPrompt("");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function trigger(pid) {
    setRunning(pid);
    try {
      await api(`/pipelines/${pid}/run`, { method: "POST" });
      await loadRuns(pid, true);
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning("");
    }
  }

  async function loadRuns(pid, open) {
    if (!open && runs[pid]) {
      setRuns((r) => ({ ...r, [pid]: undefined }));
      return;
    }
    setRuns((r) => ({ ...r, [pid]: "loading" }));
    try {
      const d = await api(`/pipelines/${pid}/runs`);
      setRuns((r) => ({ ...r, [pid]: d.runs }));
    } catch (e) {
      setRuns((r) => ({ ...r, [pid]: [] }));
    }
  }

  async function remove(p) {
    if (!confirm(`Delete pipeline "${p.name}"?`)) return;
    try {
      await api(`/pipelines/${p.id}`, { method: "DELETE" });
      setList((l) => l.filter((x) => x.id !== p.id));
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Pipelines</div>
          <div className="meta">
            Describe a job; the agent routes it to the source you can access, drafts
            verified steps, and runs them on demand. A failed step emails you.
            {agl ? " · Agent Lightning: tracing on" : " · runs traced for observability"}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {/* Build from prompt */}
      <div className="pl-build">
        <div className="composer-pill">
          <input
            className="pill-input"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && build()}
            placeholder="e.g. supply-chain status: inventory levels and production output by plant"
            disabled={building}
          />
          <button className="primary send-btn" onClick={build} disabled={building || !prompt.trim()}>
            {building ? "…" : "⚙ build"}
          </button>
        </div>
        <PlanningContextPicker {...planningCatalog}
          repositoryId={repositoryId} confluencePageIds={confluencePageIds}
          onRepositoryChange={setRepositoryId} onConfluenceChange={setConfluencePageIds}
          disabled={building} />

        {draft && (
          <div className="pl-draft">
            <div className="meta">
              Routed to <b>{draft.source}</b> · matched:{" "}
              {draft.matched_tables?.slice(0, 5).join(", ") || "—"}
            </div>
            <PlanningSources sources={draft.planning_sources} />
            {draft.repo && !draft.planning_sources?.github_repository && (
              <div className="meta pl-repo">
                📦 Suggested repository match:{" "}
                <a href={draft.repo.url} target="_blank" rel="noreferrer">{draft.repo.name}</a>
                {draft.repo.description ? ` — ${draft.repo.description}` : ""}. Select it
                above to use it as planning context on a new draft.
              </div>
            )}
            {draft.steps.length === 0 && (
              <div className="meta">
                No step verified, so there is nothing to save. Every drafted step is
                listed below with the reason it failed — fix the request (or your
                access to those tables) and build again.
              </div>
            )}
            {draft.steps.map((s, i) => (
              <Step key={i} s={s} />
            ))}
            {draft.dropped?.length > 0 && (
              <>
                <div className="meta">
                  {draft.dropped.length} step(s) dropped — they did not verify and are
                  not part of this pipeline:
                </div>
                {draft.dropped.map((s, i) => (
                  <Step key={"d" + i} s={s} />
                ))}
              </>
            )}
            <Lineage lineage={draft.lineage} />
            <div className="pl-draft-actions">
              <input
                className="sqllab-prompt"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Pipeline name"
              />
              <button
                className="chip chip-on"
                onClick={save}
                disabled={saving || !draft.steps.length}
                title={
                  draft.steps.length
                    ? "Save these verified steps"
                    : "Nothing verified — a pipeline can only be saved from steps that ran"
                }
              >
                {saving ? "saving…" : "＋ save pipeline"}
              </button>
              <button className="chip" onClick={() => setDraft(null)}>discard</button>
              {!draft.steps.length && (
                <span className="meta">
                  Save is off: no step verified, so there is no runnable pipeline here.
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Saved pipelines */}
      {list === null ? (
        <div className="meta">loading…</div>
      ) : list.length === 0 ? (
        <div className="empty">
          <div className="empty-title">No pipelines yet</div>
          <div className="empty-sub">Describe a job above and build your first one.</div>
        </div>
      ) : (
        <div className="query-list">
          {list.map((p) => {
            const rs = runs[p.id];
            return (
              <div key={p.id} className="query-card">
                <div className="query-head">
                  <div className="query-title">
                    {p.name}
                    <span className="query-tag">{p.source}</span>
                    <span className="query-tag">{p.step_count} steps</span>
                    {p.visibility === "org" && <span className="query-tag">team</span>}
                    {!p.mine && <span className="query-tag">shared</span>}
                  </div>
                  <div className="query-actions">
                    <button className="chip chip-on" onClick={() => trigger(p.id)} disabled={running === p.id}>
                      {running === p.id ? "running…" : "▷ trigger"}
                    </button>
                    <button className="chip" onClick={() => loadRuns(p.id, !rs)}>
                      {rs ? "hide runs" : "runs"}
                    </button>
                    {p.mine && (
                      <button className="chip ctx-danger" onClick={() => remove(p)}>✕</button>
                    )}
                  </div>
                </div>
                {rs && rs !== "loading" && (
                  <div className="query-body">
                    {rs.length === 0 ? (
                      <div className="meta">No runs yet — trigger it above.</div>
                    ) : (
                      rs.map((r) => (
                        <div key={r.id} className={"pl-run pl-" + r.status}>
                          <span className={"pl-status pl-" + r.status}>
                            {r.status === "success" ? "✓ success" : "✗ failed"}
                          </span>
                          <span className="meta">
                            {new Date(r.started_at * 1000).toLocaleString()} ·{" "}
                            {(r.steps_result || []).length} step(s)
                            {r.trace_id ? " · traced" : ""}
                          </span>
                          {r.status === "failed" && (
                            <div className="pl-run-err">
                              Step {(r.failed_step ?? 0) + 1} failed: {r.error}
                              {r.emailed ? " · you were emailed" : ""}
                            </div>
                          )}
                          <Lineage lineage={r.lineage} />
                        </div>
                      ))
                    )}
                  </div>
                )}
                {rs === "loading" && <div className="query-body meta">loading runs…</div>}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
