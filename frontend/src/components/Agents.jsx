// The named agent crew. One worker agent per connected data source (briefed by
// that source's skill file); a cross-source question adds the Aggregator. Shows
// which agents are callable for your role and the tables each is briefed on —
// so you can see exactly which agent answers which question.
import { useEffect, useState } from "react";
import { api, getUser } from "../api";

// Each named agent is scored on its OWN decision (per-agent reward shaping):
// a worker on its answer, the aggregator on its synthesis, each pipeline stage
// on its own artifact. This chip shows that agent's average reward + rollouts.
function RewardChip({ r }) {
  if (!r || r.n === 0) return null;
  const v = r.avg_reward;
  const tone = v == null ? "" : v >= 0.7 ? "flow-badge-ok" : v >= 0.4 ? "flow-badge-warn" : "flow-badge-bad";
  return (
    <span className={"query-tag " + tone} title={`${r.n} rollout${r.n === 1 ? "" : "s"}`}>
      reward {v == null ? "—" : v.toFixed(2)} · {r.n}
    </span>
  );
}

export default function Agents({ onClose }) {
  const [data, setData] = useState(null);
  const [training, setTraining] = useState(null);
  const [rewards, setRewards] = useState({}); // agent name -> {n, avg_reward}
  const [error, setError] = useState("");
  const isAdmin = getUser()?.role === "admin";

  useEffect(() => {
    api("/agents").then(setData).catch((e) => setError(e.message));
    if (isAdmin) {
      api("/training").then(setTraining).catch(() => {});
      api("/learning")
        .then((d) => setRewards(Object.fromEntries((d.by_agent || []).map((a) => [a.agent, a]))))
        .catch(() => {});
    }
  }, [isAdmin]);

  const rw = (name) => rewards[name];
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
            The answer header names the agent(s) called, and each agent is scored on
            its <i>own</i> decision — a worker on its answer, the aggregator on its
            synthesis — so their policies improve independently.
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
                <RewardChip r={rw(a.name)} />
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
                <RewardChip r={rw(data.aggregator.name)} />
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
            Pipeline &amp; utility agents — each scored on its own artifact (per-agent reward)
          </div>
          <div className="offline-agents">
            {[...(data.pipeline_crew || []), ...(data.utility_agents || [])].map((a) => {
              const r = rw(a.name);
              return (
                <span key={a.name} className="chip chip-on" title={a.produces ? `emits ${a.produces}` : ""}>
                  {a.name}{a.produces ? ` → ${a.produces}` : ""}
                  {r && r.avg_reward != null ? ` · ${r.avg_reward.toFixed(2)}` : ""}
                </span>
              );
            })}
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
