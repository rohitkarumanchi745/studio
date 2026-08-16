// The named agent crew. One worker agent per connected data source (briefed by
// that source's skill file); a cross-source question adds the Aggregator. Shows
// which agents are callable for your role and the tables each is briefed on —
// so you can see exactly which agent answers which question.
import { useEffect, useState } from "react";
import { api, getUser } from "../api";

export default function Agents({ onClose }) {
  const [data, setData] = useState(null);
  const [training, setTraining] = useState(null);
  const [error, setError] = useState("");
  const isAdmin = getUser()?.role === "admin";

  useEffect(() => {
    api("/agents").then(setData).catch((e) => setError(e.message));
    if (isAdmin) api("/training").then(setTraining).catch(() => {});
  }, [isAdmin]);

  const agents = data?.agents || [];
  const live = agents.filter((a) => a.accessible);
  const offline = agents.filter((a) => !a.accessible);

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Agents</div>
          <div className="meta">
            Each connected source has its own worker agent. A single-source question
            is answered by that one agent; a cross-source question (source “all”) fans
            out to every accessible worker and the <b>Aggregator</b> synthesizes them.
            The answer header always names the agent(s) that were called.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {data && (
        <div className="meta" style={{ marginBottom: 10 }}>
          {live.length} agent{live.length === 1 ? "" : "s"} callable for your role ·{" "}
          {data.multi_source
            ? "questions across “all sources” add the Aggregator"
            : "one accessible source — no fan-out needed"}
        </div>
      )}

      <div className="query-list">
        {live.map((a) => (
          <div key={a.name} className="query-card">
            <div className="query-head">
              <div className="query-title">
                {a.name}
                <span className="query-tag">{a.source}</span>
                <span className="query-tag">{a.table_count} tables</span>
              </div>
              <span className="query-badge">● live</span>
            </div>
            {a.tables?.length > 0 && (
              <div className="query-body">
                <div className="inputs-row">
                  <span className="meta">briefed on</span>
                  {a.tables.slice(0, 12).map((t) => (
                    <span key={t} className="chip chip-input">▦ {t}</span>
                  ))}
                  {a.table_count > 12 && <span className="meta">+{a.table_count - 12}</span>}
                </div>
              </div>
            )}
          </div>
        ))}

        {/* The reduce step, named. */}
        {data?.multi_source && (
          <div className="query-card">
            <div className="query-head">
              <div className="query-title">
                {data.aggregator.name}
                <span className="query-tag">reduce</span>
              </div>
              <span className="query-badge">● live</span>
            </div>
            <div className="query-body meta">
              Synthesizes the workers’ independent answers into one; runs only when a
              question spans more than one source.
            </div>
          </div>
        )}
      </div>

      {isAdmin && training && (
        <div className="mcp-block">
          <div className="canvas-title" style={{ fontSize: 15 }}>Train our own model</div>
          <div className="meta">
            Every prompt users ask is collected as a rollout. Once enough accumulate,
            they train our own policy — prompt optimization now, weight RL when a
            self-hosted model is available.
          </div>
          <div className="train-bar">
            <div className="train-fill" style={{ width: `${Math.round(training.progress * 100)}%` }} />
          </div>
          <div className="sess-meters" style={{ marginTop: 6 }}>
            <span className="meta"><b>{training.collected}</b> / {training.threshold} prompts collected</span>
            <span className="meta">{training.usable} reward-labeled</span>
            <span className="meta">{training.human_labeled} human-rated</span>
            <span className={"query-tag " + (training.ready ? "flow-badge-ok" : "")}>
              {training.ready ? "ready to train" : "collecting"}
            </span>
          </div>
          <div className="meta" style={{ marginTop: 4 }}>method: {training.method} · store: {training.store}</div>
        </div>
      )}

      {(data?.pipeline_crew?.length > 0 || data?.utility_agents?.length > 0) && (
        <>
          <div className="meta" style={{ margin: "14px 0 6px" }}>
            Pipeline &amp; utility agents — named stages every run passes through
          </div>
          <div className="offline-agents">
            {(data.pipeline_crew || []).map((a) => (
              <span key={a.name} className="chip chip-on" title={`emits ${a.produces}`}>
                {a.name}{a.produces ? ` → ${a.produces}` : ""}
              </span>
            ))}
            {(data.utility_agents || []).map((a) => (
              <span key={a.name} className="chip chip-on" title={`emits ${a.produces}`}>
                {a.name}
              </span>
            ))}
          </div>
        </>
      )}

      {offline.length > 0 && (
        <>
          <div className="meta" style={{ margin: "14px 0 6px" }}>Not connected</div>
          <div className="offline-agents">
            {offline.map((a) => (
              <span key={a.name} className="chip" title={a.configured ? "no access for your role" : "source not configured"}>
                {a.name}
              </span>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
