// What Studio has discovered about a source (metagraph.py): its tables, views
// and when it last looked. Shown under each connected source; administrators
// can ask for a fresh discovery.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

const KIND_WORDS = { table: ["table", "tables"], view: ["view", "views"] };

function ago(seconds, now) {
  const d = Math.max(0, now - Number(seconds));
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(Number(seconds) * 1000).toLocaleDateString();
}

/** One line for a source's discovery summary from GET /metagraph/sources. */
export function metadataText(summary, now = Date.now() / 1000) {
  if (!summary || summary.status === "not_discovered" || !summary.status) {
    return "Metadata: not discovered yet";
  }
  const busy = summary.status === "running" || summary.status === "queued";
  if (busy && !summary.discovered_at) {
    return summary.status === "running" ? "Metadata: discovering…" : "Metadata: discovery queued…";
  }
  if (summary.status === "failed" && !summary.discovered_at) return "Metadata: discovery failed";
  const kinds = summary.kinds || {};
  const parts = [];
  for (const [kind, [one, many]] of Object.entries(KIND_WORDS)) {
    if (kinds[kind]) parts.push(`${kinds[kind]} ${kinds[kind] === 1 ? one : many}`);
  }
  const other = Object.entries(kinds)
    .filter(([kind]) => !KIND_WORDS[kind]).reduce((n, [, v]) => n + v, 0);
  if (other) parts.push(`${other} ${other === 1 ? "object" : "objects"}`);
  if (!parts.length) parts.push("nothing found");
  let text = `Metadata: ${parts.join(" · ")}`;
  if (summary.discovered_at) text += ` · discovered ${ago(summary.discovered_at, now)}`;
  if (busy) text += " · rediscovering…";
  if (summary.status === "partial") text += " · some columns could not be read";
  if (summary.status === "failed") text += " · the last discovery failed";
  if (summary.mirror_status === "failed") text += " · Neo4j mirror failed";
  if (summary.mirror_status === "refused") text += " · Neo4j mirror refused";
  return text;
}

/** Discovery summaries by source name, with a reload. */
export function useMetadataStatus() {
  const [byName, setByName] = useState({});
  const alive = useRef(true);
  const reload = useCallback(() => api("/metagraph/sources")
    .then((d) => {
      if (!alive.current) return {};
      const map = Object.fromEntries((d?.sources || []).map((s) => [s.source, s]));
      setByName(map);
      return map;
    })
    .catch(() => ({})), []);
  useEffect(() => {
    alive.current = true;
    reload();
    return () => { alive.current = false; };
  }, [reload]);
  // While a discovery is queued or running — a source just connected, say —
  // keep the line current (every 3 s, for about two minutes).
  const polls = useRef(0);
  const pending = Object.values(byName).some((s) => s.status === "queued" || s.status === "running");
  useEffect(() => {
    if (!pending || polls.current >= 40) return undefined;
    const timer = setTimeout(() => { polls.current += 1; reload(); }, 3000);
    return () => clearTimeout(timer);
  }, [pending, byName, reload]);
  return { byName, reload };
}

export default function MetadataLine({ name, summary, canRediscover, reload }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function rediscover() {
    setBusy(true);
    setError("");
    try {
      await api(`/metagraph/sources/${encodeURIComponent(name)}/discover`, { method: "POST" });
      const before = summary?.discovered_at || 0;
      // Poll until THIS discovery finishes (about two minutes at most): it has
      // been seen running and stopped, or it recorded a new discovery time. An
      // earlier run's "failed" must not end the wait before this one starts.
      let seenRunning = false;
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const next = (await reload())[name];
        if (!next) continue;
        if (next.status === "running") seenRunning = true;
        else if (seenRunning || (next.discovered_at || 0) !== before) break;
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="metadata-line meta" title={summary?.error || summary?.mirror_error || ""}>
      <span>{busy ? "Metadata: discovering…" : metadataText(summary)}</span>
      {canRediscover && (
        <button className="chip" onClick={rediscover} disabled={busy}>
          {busy ? "discovering…" : "↻ Rediscover"}
        </button>
      )}
      {error && <span className="error">{error}</span>}
    </div>
  );
}
