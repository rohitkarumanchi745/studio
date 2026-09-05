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
import { api } from "../api";
import Lineage from "./Lineage";

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
        body: JSON.stringify({ prompt: prompt.trim() }),
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

        {draft && (
          <div className="pl-draft">
            <div className="meta">
              Routed to <b>{draft.source}</b> · matched:{" "}
              {draft.matched_tables?.slice(0, 5).join(", ") || "—"}
            </div>
            {draft.repo && (
              <div className="meta pl-repo">
                📦 scripts from{" "}
                <a href={draft.repo.url} target="_blank" rel="noreferrer">{draft.repo.name}</a>
                {draft.repo.description ? ` — ${draft.repo.description}` : ""}
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
