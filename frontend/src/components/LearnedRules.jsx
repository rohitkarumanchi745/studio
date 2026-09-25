// Learned rules — guidance the worker drafts from failed runs (SQL errors,
// rejected queries, thumbs-down). A draft reaches no agent until an admin
// approves it here, optionally after editing: it was written from every
// user's runs and will be read by every user's agent.
import { useEffect, useState } from "react";
import { api } from "../api";

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

export function RulesView({ data, draftText, onDraftText, onApprove, onReject, onRetire, busy }) {
  const proposal = data.history.find((r) => r.status === "proposed");
  const past = data.history.filter((r) => r.status !== "proposed");
  return (
    <div className="query-list">
      <div className="query-card">
        <div className="query-head">
          <div className="query-title">
            Live in every agent prompt
            {data.active && <span className="query-tag">approved {when(data.active.decided_at)}</span>}
          </div>
          {data.active && (
            <button className="chip" disabled={busy} onClick={onRetire}>retire</button>
          )}
        </div>
        <div className="query-body">
          {data.active
            ? <pre className="query-sql">{data.active.rules}</pre>
            : <div className="meta">No learned rules are active.</div>}
        </div>
      </div>

      <div className="query-card">
        <div className="query-head">
          <div className="query-title">
            Waiting for review
            {proposal && (
              <span className="query-tag">
                from {proposal.evidence_count} failed runs · {when(proposal.created_at)}
              </span>
            )}
          </div>
        </div>
        <div className="query-body">
          {proposal ? (
            <>
              <div className="meta">
                Check that no rule quotes a user's question, a name or a filter value
                before approving. Edit freely; only "- " lines are kept.
              </div>
              <textarea className="ap-goal" rows={8} spellCheck={false} value={draftText}
                onChange={(e) => onDraftText(e.target.value)} disabled={busy} />
              <div className="gov-actions" style={{ marginTop: 6 }}>
                <button className="chip" disabled={busy} onClick={() => onApprove(proposal)}>
                  ✓ approve
                </button>
                <button className="chip" disabled={busy} onClick={() => onReject(proposal)}>
                  ✕ reject
                </button>
              </div>
            </>
          ) : (
            <div className="meta">
              No draft yet. The worker drafts one after {data.min_failures} new failed runs.
            </div>
          )}
        </div>
      </div>

      {past.map((r) => (
        <div key={r.id} className="query-card">
          <div className="query-head">
            <div className="query-title">
              <span className="query-tag">{r.status}</span>
              <span className="query-tag">{when(r.created_at)}</span>
            </div>
          </div>
          <div className="query-body"><pre className="query-sql">{r.rules}</pre></div>
        </div>
      ))}
    </div>
  );
}

export default function LearnedRules({ onClose }) {
  const [data, setData] = useState(null);
  const [draftText, setDraftText] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  function show(d) {
    setData(d);
    setDraftText(d.history.find((r) => r.status === "proposed")?.rules || "");
  }

  useEffect(() => {
    api("/learned-rules").then(show).catch((e) => setError(e.message));
  }, []);

  async function run(fn) {
    setBusy(true);
    setError("");
    setNote("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const draftNow = () => run(async () => {
    const r = await api("/learned-rules/draft", { method: "POST" });
    if (!r.proposal) setNote(`No draft: ${r.reason}.`);
    show(await api("/learned-rules"));
  });
  const approve = (p) => run(async () => show(await api(`/learned-rules/${p.id}/approve`, {
    method: "POST", body: JSON.stringify({ rules: draftText }),
  })));
  const reject = (p) => run(async () => show(await api(`/learned-rules/${p.id}/reject`,
    { method: "POST" })));
  const retire = () => {
    if (!confirm("Remove the learned rules from every agent prompt?")) return;
    run(async () => show(await api("/learned-rules/retire", { method: "POST" })));
  };

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Learned rules</div>
          <div className="meta">
            Guidance drafted from failed runs. Nothing reaches an agent until you approve it.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" disabled={busy} onClick={draftNow}>draft now</button>
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      {note && <div className="meta">{note}</div>}
      {data && (
        <RulesView data={data} draftText={draftText} onDraftText={setDraftText}
          onApprove={approve} onReject={reject} onRetire={retire} busy={busy} />
      )}
    </section>
  );
}
