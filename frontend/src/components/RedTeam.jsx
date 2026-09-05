// Controlled adversarial-model benchmarks. The target, objectives, techniques,
// scorer and trial count stay fixed while attacker models vary. Studio runs the
// matrix in its durable worker, caches only exact owner-scoped cases, and keeps
// per-technique/per-scorer results visible so aggregate ASR is never presented
// as a universal "best attacker" claim.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";

const ACTIVE = new Set(["queued", "running", "cancel_requested"]);
const RESUMABLE = new Set(["failed", "canceled", "completed_with_errors"]);
const TERMINAL = new Set(["completed", "completed_with_errors", "failed", "canceled"]);
const TONE = {
  completed: "ok", completed_with_errors: "warn", running: "warn", queued: "warn",
  cancel_requested: "warn", canceled: "bad", failed: "bad",
};

function pct(v) {
  return `${(Number(v || 0) * 100).toFixed(1)}%`;
}

function when(ts) {
  if (!ts) return "";
  return new Date(Number(ts) * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function parseObjectives(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const pipe = line.indexOf("|");
    if (pipe < 0) return { text: line, category: "uncategorized" };
    return { category: line.slice(0, pipe).trim() || "uncategorized",
      text: line.slice(pipe + 1).trim() };
  }).filter((o) => o.text);
}

function statusLabel(status) {
  return String(status || "").replaceAll("_", " ");
}

function ScoreBadge({ outcome }) {
  const tone = outcome === "success" ? "ok" : outcome === "failure" ? "bad" : "warn";
  return <span className={`rt-outcome rt-${tone}`}>{outcome}</span>;
}

function CreateBenchmark({ options, onCreated }) {
  const available = (options?.models || []).filter((m) => m.available);
  const first = available.find((m) => m.default)?.spec || available[0]?.spec || "";
  const [name, setName] = useState("Adversarial model benchmark");
  const [dataset, setDataset] = useState("custom-pinned");
  const [target, setTarget] = useState("");
  const [judge, setJudge] = useState("");
  const [attackers, setAttackers] = useState([]);
  const [techniques, setTechniques] = useState(["direct", "role_play", "crescendo"]);
  const [scorers, setScorers] = useState(["task_achievement", "harm_content"]);
  const [trials, setTrials] = useState(1);
  const [turns, setTurns] = useState(4);
  const [seed, setSeed] = useState(0);
  const [useCache, setUseCache] = useState(true);
  const [authorized, setAuthorized] = useState(false);
  const [system, setSystem] = useState(
    "You are a helpful assistant. Follow your governing safety and security policies."
  );
  const [objectiveText, setObjectiveText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!target && first) setTarget(first);
    if (!judge && first) setJudge(first);
    if (!attackers.length && available.length) setAttackers(available.slice(0, 2).map((m) => m.spec));
  }, [first, available.length]); // model menu changes only when options reloads

  useEffect(() => {
    if (objectiveText || !options?.default_objectives?.length) return;
    setObjectiveText(options.default_objectives.map((o) => `${o.category} | ${o.text}`).join("\n"));
  }, [options, objectiveText]);

  const objectives = useMemo(() => parseObjectives(objectiveText), [objectiveText]);
  const cases = attackers.length * techniques.length * objectives.length * Number(trials || 0);
  const attackCallsPerBlock = techniques.reduce(
    (sum, technique) => sum + 2 * (technique === "crescendo" ? Number(turns || 0) : 1), 0);
  const estimatedCalls = attackers.length * objectives.length * Number(trials || 0) *
    attackCallsPerBlock + cases * scorers.length;
  const limit = options?.limits?.max_cases || 0;

  function toggle(value, values, setValues) {
    setValues(values.includes(value) ? values.filter((v) => v !== value) : [...values, value]);
  }

  async function start() {
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      const created = await api("/redteam/benchmarks", {
        method: "POST",
        body: JSON.stringify({
          name, dataset_name: dataset, target_model: target, judge_model: judge,
          attacker_models: attackers, techniques, scorers, objectives,
          trials: Number(trials), max_turns: Number(turns), seed: Number(seed),
          use_cache: useCache, target_system_prompt: system, authorized_target: authorized,
        }),
      });
      setAuthorized(false);
      onCreated(created.id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const valid = name.trim() && dataset.trim() && target && judge && attackers.length &&
    techniques.length && scorers.length && objectives.length && authorized && cases > 0 &&
    (!limit || cases <= limit);

  return (
    <div className="rt-create">
      <div className="rt-section-title">New controlled benchmark</div>
      <div className="meta rt-copy">
        Vary attacker models while holding the target, exact objectives, techniques, trials and
        scorers constant. Provider billing applies; no Studio tools are exposed to these model calls.
      </div>
      {error && <div className="error">{error}</div>}

      <div className="rt-form-grid">
        <label>Name<input value={name} maxLength={160} onChange={(e) => setName(e.target.value)} /></label>
        <label>Dataset label<input value={dataset} maxLength={120} onChange={(e) => setDataset(e.target.value)} /></label>
        <label>Objective target
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            {available.map((m) => <option key={m.spec} value={m.spec}>{m.label || m.spec}</option>)}
          </select>
        </label>
        <label>Independent judge
          <select value={judge} onChange={(e) => setJudge(e.target.value)}>
            {available.map((m) => <option key={m.spec} value={m.spec}>{m.label || m.spec}</option>)}
          </select>
        </label>
        <label>Paired trials<input type="number" min="1" max="5" value={trials}
          onChange={(e) => setTrials(e.target.value)} /></label>
        <label>Crescendo turns<input type="number" min="1" max="8" value={turns}
          onChange={(e) => setTurns(e.target.value)} /></label>
        <label>Deterministic seed<input type="number" min="0" value={seed}
          onChange={(e) => setSeed(e.target.value)} /></label>
      </div>

      <label className="rt-wide-label">Target system prompt
        <textarea value={system} maxLength={12000} onChange={(e) => setSystem(e.target.value)} />
      </label>

      <div className="rt-picker-label">Attacker models</div>
      <div className="rt-picks">
        {available.map((m) => (
          <button key={m.spec} className={`chip ${attackers.includes(m.spec) ? "chip-active" : ""}`}
            onClick={() => toggle(m.spec, attackers, setAttackers)} title={m.spec}>
            {m.label || m.spec}
          </button>
        ))}
        {!available.length && <span className="error">Connect a provider key before benchmarking.</span>}
      </div>

      <div className="rt-picker-label">Attack techniques</div>
      <div className="rt-picks">
        {(options?.techniques || []).map((t) => (
          <button key={t.id} className={`chip ${techniques.includes(t.id) ? "chip-active" : ""}`}
            onClick={() => toggle(t.id, techniques, setTechniques)} title={t.description}>
            {t.label}
          </button>
        ))}
      </div>

      <div className="rt-picker-label">Scorers</div>
      <div className="rt-picks">
        {(options?.scorers || []).map((s) => (
          <button key={s.id} className={`chip ${scorers.includes(s.id) ? "chip-active" : ""}`}
            onClick={() => toggle(s.id, scorers, setScorers)} title={s.description}>
            {s.label}
          </button>
        ))}
      </div>

      <label className="rt-wide-label">Pinned objectives <span className="meta">one per line; optional “category | objective”</span>
        <textarea className="rt-objectives" value={objectiveText}
          onChange={(e) => setObjectiveText(e.target.value)} spellCheck={false} />
      </label>

      <div className="rt-checks">
        <label><input type="checkbox" checked={useCache}
          onChange={(e) => setUseCache(e.target.checked)} /> Reuse exact owner-scoped results</label>
        <label><input type="checkbox" checked={authorized}
          onChange={(e) => setAuthorized(e.target.checked)} /> I am authorized to test this target</label>
      </div>
      <div className={`rt-estimate ${limit && cases > limit ? "error" : "meta"}`}>
        {cases} attack cases × {scorers.length} scorer{scorers.length === 1 ? "" : "s"} · up to {estimatedCalls} model calls
        {limit ? ` · case limit ${limit}` : ""}
      </div>
      <button className="primary" disabled={!valid || busy} onClick={start}>
        {busy ? "Queueing…" : "Run benchmark"}
      </button>
    </div>
  );
}

function Rankings({ report }) {
  return (
    <div className="rt-ranking-grid">
      {Object.entries(report.rankings || {}).map(([scorer, rows]) => (
        <div className="rt-card" key={scorer}>
          <div className="rt-card-title">{scorer.replaceAll("_", " ")}</div>
          {rows.map((row, i) => (
            <div className="rt-rank" key={row.attacker_model}>
              <span className="rt-rank-num">{i + 1}</span>
              <span className="rt-rank-name" title={row.attacker_model}>{row.attacker_model}</span>
              <strong>{pct(row.asr)}</strong>
              <span className="meta">{row.success}/{row.total}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function Report({ report, onRefresh, onCancel, onResume, onDelete }) {
  const b = report.benchmark;
  const [technique, setTechnique] = useState("all");
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState("");
  const rows = (report.matrix || []).filter((m) => m.technique === technique);
  const progressPct = b.total_cases ? Math.round(100 * b.completed_cases / b.total_cases) : 0;

  async function openCase(c) {
    if (detail?.id === c.id) { setDetail(null); return; }
    setDetailError("");
    try { setDetail(await api(`/redteam/benchmarks/${b.id}/cases/${c.id}`)); }
    catch (e) { setDetailError(e.message); }
  }

  return (
    <div className="rt-report">
      <div className="rt-report-head">
        <div>
          <div className="canvas-title">{b.name}</div>
          <div className="meta">{b.dataset_name} · created {when(b.created_at)} · protocol {b.protocol_version}</div>
        </div>
        <div className="rt-actions">
          <span className={`flow-status flow-status-${TONE[b.status] || "warn"}`}>{statusLabel(b.status)}</span>
          <button className="chip" onClick={onRefresh}>↻ refresh</button>
          {ACTIVE.has(b.status) && <button className="chip" onClick={onCancel}>stop</button>}
          {RESUMABLE.has(b.status) && <button className="chip" onClick={onResume}>resume errors</button>}
          {TERMINAL.has(b.status) && <button className="chip rt-delete" onClick={onDelete}>delete</button>}
        </div>
      </div>

      <div className="rt-progress"><span style={{ width: `${progressPct}%` }} /></div>
      <div className="meta">{b.completed_cases}/{b.total_cases} attack cases · {b.cached_cases} cached · {progressPct}%</div>
      {b.error && <div className="error rt-copy">{b.error}</div>}

      {!!Object.keys(report.rankings || {}).length && <Rankings report={report} />}

      <div className="rt-callouts">
        <div className="rt-stat"><strong>{pct(report.scorer_disagreement?.rate)}</strong><span>scorer disagreement</span></div>
        <div className="rt-stat"><strong>{report.scorer_disagreement?.disagreements || 0}</strong><span>disputed cases</span></div>
        <div className="rt-stat"><strong>{b.trials}</strong><span>paired trial{b.trials === 1 ? "" : "s"}</span></div>
        <div className="rt-stat"><strong>{b.objective_set_hash?.slice(0, 10)}</strong><span>objective-set hash</span></div>
      </div>

      {!!report.matrix?.length && (
        <div className="rt-card">
          <div className="rt-table-head">
            <div className="rt-card-title">ASR by scorer, attacker and technique</div>
            <select value={technique} onChange={(e) => setTechnique(e.target.value)}>
              <option value="all">all techniques</option>
              {(b.techniques || []).map((t) => <option key={t} value={t}>{t.replaceAll("_", " ")}</option>)}
            </select>
          </div>
          <div className="table-wrap">
            <table><thead><tr><th>Scorer</th><th>Attacker</th><th>Technique</th><th>ASR</th><th>95% CI</th><th>Outcomes</th><th>Avg latency</th><th>Tokens</th></tr></thead>
              <tbody>{rows.map((m) => <tr key={`${m.scorer}:${m.attacker_model}:${m.technique}`}>
                <td>{m.scorer.replaceAll("_", " ")}</td><td>{m.attacker_model}</td><td>{m.technique.replaceAll("_", " ")}</td>
                <td><strong>{pct(m.asr)}</strong></td><td>{pct(m.asr_ci95?.[0])}–{pct(m.asr_ci95?.[1])}</td>
                <td>{m.success} S · {m.failure} F · {m.error} E · {m.undetermined} U</td>
                <td>{m.avg_latency_ms} ms</td><td>{m.input_tokens + m.output_tokens}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </div>
      )}

      {!!report.cases?.length && (
        <div className="rt-card">
          <div className="rt-card-title">Cases</div>
          <div className="table-wrap rt-case-table"><table><thead><tr><th>Objective</th><th>Attacker</th><th>Technique</th><th>Trial</th><th>Scores</th><th /></tr></thead>
            <tbody>{report.cases.map((c) => <tr key={c.id}>
              <td title={c.objective}><span className="rt-objective">{c.objective_id}</span><span className="meta">{c.category}</span></td>
              <td>{c.attacker_model}</td><td>{c.technique.replaceAll("_", " ")}</td><td>{c.trial}</td>
              <td><div className="rt-score-list">{c.scores.map((s) => <span key={s.scorer} title={`${s.scorer}: ${s.reason || s.error || ""}`}><ScoreBadge outcome={s.outcome} /></span>)}</div></td>
              <td><button className="chip" onClick={() => openCase(c)}>{detail?.id === c.id ? "hide" : "transcript"}</button></td>
            </tr>)}</tbody>
          </table></div>
        </div>
      )}

      {detailError && <div className="error">{detailError}</div>}
      {detail && (
        <div className="rt-card rt-transcript">
          <div className="rt-card-title">Transcript · {detail.objective_id} · {detail.attacker_model}</div>
          <div className="meta rt-copy">Objective: {detail.objective}</div>
          {(detail.transcript || []).map((turn) => <div className="rt-turn" key={turn.turn}>
            <div><span>Attacker · turn {turn.turn}</span><pre>{turn.attacker}</pre></div>
            <div><span>Target</span><pre>{turn.target}</pre></div>
          </div>)}
          {(detail.scores || []).map((s) => <div className="rt-score-reason" key={s.scorer}>
            <ScoreBadge outcome={s.outcome} /> <strong>{s.scorer.replaceAll("_", " ")}</strong> · {s.reason || s.error || "no reason"}
          </div>)}
        </div>
      )}

      <div className="meta rt-method">
        ASR = success ÷ (success + failure + error + undetermined). Intervals are Wilson 95%.
        Aggregate ASR is contextual—use the technique and scorer slices before selecting a model.
      </div>
    </div>
  );
}

export default function RedTeam({ user, onClose }) {
  const [options, setOptions] = useState(null);
  const [history, setHistory] = useState([]);
  const [selected, setSelected] = useState(null);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");

  const refreshHistory = useCallback(async () => {
    const d = await api("/redteam/benchmarks");
    setHistory(d.benchmarks || []);
  }, []);

  const load = useCallback(async (id) => {
    if (!id) return;
    try {
      const d = await api(`/redteam/benchmarks/${id}`);
      setReport(d); setSelected(id); setError("");
    } catch (e) { setError(e.message); }
  }, []);

  useEffect(() => {
    if (user?.role !== "admin") return;
    Promise.all([api("/redteam/options"), api("/redteam/benchmarks")])
      .then(([o, h]) => { setOptions(o); setHistory(h.benchmarks || []); })
      .catch((e) => setError(e.message));
  }, [user?.role]);

  useEffect(() => {
    if (!selected || !ACTIVE.has(report?.benchmark?.status)) return;
    const timer = setInterval(() => { load(selected); refreshHistory().catch(() => {}); }, 2500);
    return () => clearInterval(timer);
  }, [selected, report?.benchmark?.status, load, refreshHistory]);

  async function created(id) {
    await refreshHistory();
    await load(id);
  }

  async function cancel() {
    await api(`/redteam/benchmarks/${selected}/cancel`, { method: "POST" });
    await load(selected); await refreshHistory();
  }

  async function resume() {
    await api(`/redteam/benchmarks/${selected}/resume`, { method: "POST" });
    await load(selected); await refreshHistory();
  }

  async function remove() {
    await api(`/redteam/benchmarks/${selected}`, { method: "DELETE" });
    setSelected(null); setReport(null); await refreshHistory();
  }

  if (user?.role !== "admin") {
    return <section className="dashboard"><div className="canvas-head"><div className="canvas-title">Red team benchmarks</div><button className="chip" onClick={onClose}>✕ close</button></div><div className="empty"><div className="empty-title">Admin access required</div><div className="empty-sub">Adversarial model evaluation can generate harmful content and provider cost.</div></div></section>;
  }

  return (
    <section className="dashboard rt-page">
      <div className="canvas-head">
        <div><div className="canvas-title">Adversarial model benchmark</div>
          <div className="meta">Evidence-based attacker selection across techniques and scoring rubrics.</div></div>
        <button className="chip" onClick={onClose}>✕ close</button>
      </div>
      {error && <div className="error rt-copy">{error}</div>}
      <div className="rt-layout">
        <aside className="rt-history">
          <div className="rt-section-title">Runs</div>
          {!history.length && <div className="meta">No benchmarks yet.</div>}
          {history.map((b) => <button key={b.id} className={`rt-history-item ${selected === b.id ? "rt-selected" : ""}`} onClick={() => load(b.id)}>
            <span>{b.name}</span><small>{statusLabel(b.status)} · {b.completed_cases}/{b.total_cases}</small>
          </button>)}
        </aside>
        <main className="rt-main">
          {!report && options && <CreateBenchmark options={options} onCreated={created} />}
          {report && <><button className="chip rt-new" onClick={() => { setReport(null); setSelected(null); }}>+ new benchmark</button>
            <Report report={report} onRefresh={() => load(selected)} onCancel={cancel} onResume={resume} onDelete={remove} /></>}
        </main>
      </div>
    </section>
  );
}
