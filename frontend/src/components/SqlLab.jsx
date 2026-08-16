// Edit the agent's SQL, verify it against the source, and save it to the
// verified library — but only once it verifies. Save stays disabled until a
// successful verify, and the server re-verifies on save too, so nothing
// unverified can reach the library.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function SqlLab({ sql, source, table, requirement }) {
  const [draft, setDraft] = useState(sql || "");
  const [prompt, setPrompt] = useState(requirement || "");
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState(null); // {ok, row_count, columns, error}
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [shareOrg, setShareOrg] = useState(false);
  const [error, setError] = useState("");

  const edited = draft.trim() !== (sql || "").trim();
  // Editing the SQL after a verify invalidates it — must re-verify to save.
  useEffect(() => {
    setResult(null);
    setSaved(false);
  }, [draft]);

  async function verify() {
    setVerifying(true);
    setError("");
    try {
      const r = await api("/queries/verify", {
        method: "POST",
        body: JSON.stringify({ source, table, sql: draft }),
      });
      setResult(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setVerifying(false);
    }
  }

  async function save() {
    if (!result?.ok) return;
    setSaving(true);
    setError("");
    try {
      await api("/queries", {
        method: "POST",
        body: JSON.stringify({
          prompt: prompt.trim(),
          sql: result.sql || draft,
          source,
          table,
          edited,
          visibility: shareOrg ? "org" : "private",
        }),
      });
      setSaved(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="sqllab">
      <label className="meta sqllab-label">Requirement — what this SQL should answer</label>
      <input
        className="sqllab-prompt"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder="e.g. Monthly revenue by region for 2025"
      />
      <label className="meta sqllab-label">
        SQL {edited && <span className="sqllab-edited">· edited</span>}
      </label>
      <textarea
        className="sqllab-sql"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        rows={Math.min(12, Math.max(4, draft.split("\n").length + 1))}
      />

      <div className="sqllab-actions">
        <button className="chip" onClick={verify} disabled={verifying || !draft.trim()}>
          {verifying ? "verifying…" : "✓ verify"}
        </button>
        <button
          className={"chip" + (result?.ok ? " chip-on" : "")}
          onClick={save}
          disabled={!result?.ok || saving || !prompt.trim() || saved}
          title={
            result?.ok
              ? "Save to the verified library"
              : "Verify the SQL before saving"
          }
        >
          {saved ? "✓ saved" : saving ? "saving…" : "＋ save to library"}
        </button>
        <label className="sqllab-org meta" title="Let teammates with access see this query">
          <input type="checkbox" checked={shareOrg} onChange={(e) => setShareOrg(e.target.checked)} />
          share with team
        </label>
      </div>

      {result && (
        <div className={"sqllab-result " + (result.ok ? "ok" : "bad")}>
          {result.ok ? (
            <>
              ✓ Verified — {result.row_count} row{result.row_count === 1 ? "" : "s"},{" "}
              {result.columns?.length || 0} column{result.columns?.length === 1 ? "" : "s"}
              {result.columns?.length > 0 && (
                <span className="meta"> · {result.columns.slice(0, 6).join(", ")}
                  {result.columns.length > 6 ? " …" : ""}</span>
              )}
            </>
          ) : (
            <>✗ {result.error}</>
          )}
          {result.agent && (
            <span className="meta"> · by {result.agent}
              {result.trace_id ? ` · trace ${String(result.trace_id).slice(0, 8)}` : ""}</span>
          )}
        </div>
      )}
      {saved && (
        <div className="meta sqllab-saved">Saved to your verified library — find it under ⌗ Saved SQL.</div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  );
}
