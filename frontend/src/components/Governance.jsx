// Governance-as-code editor (admin only). One YAML owns per-role source/table
// access and per-table compliance (deny/mask columns, row caps). Validate
// before applying; applying hot-reloads with no redeploy. Clearing reverts to
// Studio's built-in RBAC.
import { Fragment, useEffect, useState } from "react";
import { api, getToken } from "../api";
import { PlanningContextPicker, usePlanningCatalog } from "./planningContext";

// Chat result-row retention (retention.py): what the scheduled pass removes,
// and — before the first purge of existing history — the notice, a download of
// exactly those rows, and the choice to start now or postpone.
export function Retention() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    api("/admin/retention").then(setSt).catch((e) => setErr(e.message));
  }, []);

  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

  async function act(path, body) {
    setErr("");
    setBusy(path);
    try {
      setSt(await api(`/admin/retention/${path}`, { method: "POST", body: JSON.stringify(body || {}) }));
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  async function download() {
    setErr("");
    setBusy("backup");
    try {
      const res = await fetch("/api/admin/retention/backup",
                              { headers: { Authorization: `Bearer ${getToken()}` } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `download failed (${res.status})`);
      const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1]
        || "studio-result-rows.jsonl";
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  if (!st && !err) return null;
  return (
    <div className="mcp-block retention">
      <div className="canvas-title" style={{ fontSize: 15 }}>Data retention — chat result rows</div>
      {err && <div className="error">{err}</div>}
      {st && !st.enabled && (
        <div className="meta">
          Off — result rows are kept forever. Set STUDIO_MESSAGE_ROWS_RETENTION_DAYS to start;
          the first removal of existing history is announced here before it happens.
        </div>
      )}
      {st?.enabled && (
        <div className="meta">
          Rows older than {st.days} days are removed by a scheduled pass every{" "}
          {Math.max(1, Math.round(st.tick_seconds / 60))} min. Answer text, SQL and chart settings stay.
        </div>
      )}
      {st?.first_purge_pending && (
        <div className="retention-notice" role="status">
          <div>
            <b>Result rows of {st.eligible} {st.eligible === 1 ? "answer" : "answers"}</b>{" "}
            (oldest from {when(st.oldest_eligible_at)}) will be removed{" "}
            {st.first_purge_at ? `on ${when(st.first_purge_at)}` : "once the notice period starts on the next pass"}.
            {st.acknowledged_by
              ? ` Backup confirmed by ${st.acknowledged_by}: removal starts on the next pass.`
              : " Back them up first."}
          </div>
          <div className="gov-actions">
            <button className="chip" onClick={download} disabled={busy === "backup"}>
              {busy === "backup" ? "preparing…" : "⇩ download these rows"}
            </button>
            {!st.acknowledged_by && (
              <button className="chip" onClick={() => act("acknowledge")} disabled={busy === "acknowledge"}>
                ✓ backup taken — start now
              </button>
            )}
            <button className="chip" onClick={() => act("postpone", { days: 7 })} disabled={busy === "postpone"}>
              ⏸ postpone 7 days
            </button>
          </div>
        </div>
      )}
      {st?.runs?.length > 0 && (
        <table className="retention-runs">
          <thead><tr><th>pass</th><th>status</th><th>eligible</th><th>removed</th><th /></tr></thead>
          <tbody>
            {st.runs.slice(0, 5).map((r) => (
              <tr key={r.started_at} className={r.status === "failed" ? "bad" : ""}>
                <td>{when(r.started_at)}</td><td>{r.status.replace(/_/g, " ")}</td>
                <td>{r.eligible ?? "—"}</td><td>{r.purged ?? "—"}</td><td>{r.error || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function McpServers() {
  const [servers, setServers] = useState([]);
  const [envServers, setEnvServers] = useState([]);
  const [form, setForm] = useState({ name: "", transport: "streamable_http", url: "", command: "", args: "" });
  const [err, setErr] = useState("");

  const load = () =>
    api("/settings/mcp")
      .then((d) => {
        setServers(d.servers || []);
        setEnvServers(d.env_servers || []);
      })
      .catch((e) => setErr(e.message));

  useEffect(() => {
    load();
  }, []);

  async function add() {
    setErr("");
    try {
      const body = {
        name: form.name.trim(),
        transport: form.transport,
        url: form.url.trim() || undefined,
        command: form.command.trim() || undefined,
        args: form.args.trim() ? form.args.split(/\s+/) : undefined,
      };
      await api("/settings/mcp", { method: "POST", body: JSON.stringify(body) });
      setForm({ name: "", transport: form.transport, url: "", command: "", args: "" });
      load();
    } catch (e) {
      setErr(e.message);
    }
  }

  async function remove(name) {
    await api(`/settings/mcp/${name}`, { method: "DELETE" });
    load();
  }

  const stdio = form.transport === "stdio";
  return (
    <div className="mcp-block">
      <div className="canvas-title" style={{ fontSize: 15 }}>MCP servers</div>
      <div className="meta">
        Register servers that expose your existing scripts (a filesystem or git
        server) or internal tools; agents pick up their tools automatically.
      </div>
      {err && <div className="error">{err}</div>}
      <div className="job-form-row" style={{ marginTop: 8 }}>
        <input className="sqllab-prompt" style={{ maxWidth: 150 }} placeholder="name"
          value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })}>
          <option value="streamable_http">streamable_http</option>
          <option value="sse">sse</option>
          <option value="stdio">stdio</option>
        </select>
        {stdio ? (
          <>
            <input className="sqllab-prompt" style={{ maxWidth: 120 }} placeholder="command (npx)"
              value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} />
            <input className="sqllab-prompt" style={{ flex: 1 }} placeholder="args (space-separated)"
              value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })} />
          </>
        ) : (
          <input className="sqllab-prompt" style={{ flex: 1 }} placeholder="https://mcp.internal/… "
            value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
        )}
        <button className="chip chip-on" onClick={add} disabled={!form.name.trim()}>＋ register</button>
      </div>
      <div className="share-list">
        {servers.map((s) => (
          <div key={s.name} className="share-row">
            <div>
              <div>{s.name} <span className="query-tag">{s.transport}</span></div>
              <div className="meta">{s.url || `${s.command} ${(s.args || []).join(" ")}`}</div>
            </div>
            <button className="chip" onClick={() => remove(s.name)}>remove</button>
          </div>
        ))}
        {servers.length === 0 && (
          <div className="meta">
            No servers registered{envServers.length ? ` (env: ${envServers.join(", ")})` : ""}.
          </div>
        )}
      </div>
    </div>
  );
}

function GithubRepos() {
  const [repos, setRepos] = useState([]);
  const [form, setForm] = useState({ name: "", url: "", description: "", default_branch: "main" });
  const [err, setErr] = useState("");

  const load = () =>
    api("/settings/repos")
      .then((d) => setRepos(d.repos || []))
      .catch((e) => setErr(e.message));

  useEffect(() => {
    load();
  }, []);

  async function add() {
    setErr("");
    try {
      await api("/settings/repos", {
        method: "POST",
        body: JSON.stringify({
          name: form.name.trim(),
          url: form.url.trim(),
          description: form.description.trim() || undefined,
          default_branch: form.default_branch.trim() || "main",
        }),
      });
      setForm({ name: "", url: "", description: "", default_branch: "main" });
      load();
    } catch (e) {
      setErr(e.message);
    }
  }

  async function remove(name) {
    await api(`/settings/repos/${name}`, { method: "DELETE" });
    load();
  }

  return (
    <div className="mcp-block">
      <div className="canvas-title" style={{ fontSize: 15 }}>GitHub repositories</div>
      <div className="meta">
        Register repositories that analysts may select when building a pipeline
        in Chat or Pipelines. Studio reads bounded pipeline-file context; it does
        not execute repository code. Set a server-side GITHUB_TOKEN for private repos.
      </div>
      {err && <div className="error">{err}</div>}
      <div className="job-form-row" style={{ marginTop: 8 }}>
        <input className="sqllab-prompt" style={{ maxWidth: 150 }} placeholder="name"
          value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input className="sqllab-prompt" style={{ flex: 1 }} placeholder="https://github.com/org/repo"
          value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
        <input className="sqllab-prompt" style={{ flex: 1 }} placeholder="what it does (keywords help matching)"
          value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        <input className="sqllab-prompt" style={{ maxWidth: 120 }} placeholder="default branch"
          value={form.default_branch} onChange={(e) => setForm({ ...form, default_branch: e.target.value })} />
        <button className="chip chip-on" onClick={add}
          disabled={!form.name.trim() || !form.url.includes("github.com")}>＋ register</button>
      </div>
      <div className="share-list">
        {repos.map((r) => (
          <div key={r.name} className="share-row">
            <div>
              <div>{r.name} <span className="query-tag">{r.default_branch || "main"}</span></div>
              <div className="meta">{r.url}{r.description ? ` — ${r.description}` : ""}</div>
              <div className="meta">Repository ID: {r.id}</div>
            </div>
            <button className="chip" onClick={() => remove(r.name)}>remove</button>
          </div>
        ))}
        {repos.length === 0 && <div className="meta">No repositories registered.</div>}
      </div>
    </div>
  );
}

function PlanningSourcesPreview() {
  const catalog = usePlanningCatalog();
  const [repositoryId, setRepositoryId] = useState("");
  const [confluencePageIds, setConfluencePageIds] = useState([]);
  return (
    <div className="mcp-block">
      <div className="canvas-title" style={{ fontSize: 15 }}>Pipeline planning sources</div>
      <div className="meta">
        Preview the repositories and Confluence pages builders can select. This does
        not set a default or authorize a run; selections are made per draft in Chat
        or Pipelines.
      </div>
      <PlanningContextPicker {...catalog} preview repositoryId={repositoryId}
        confluencePageIds={confluencePageIds} onRepositoryChange={setRepositoryId}
        onConfluenceChange={setConfluencePageIds} />
    </div>
  );
}

// The built-in stores, in display order; the server adds the stores connected
// on the Integrations screen (`kinds`: source name → s3 / azure_blob / gcs).
const STORE_SOURCES = ["s3", "azure_blob", "gcs"];
const URI_HINT = {
  s3: "s3://bucket/prefix/*.parquet",
  azure_blob: "az://container/path/*.parquet",
  gcs: "gs://bucket/prefix/*.parquet",
};

/** Every store source, built-ins first: [[name, kind]]. */
export function storeSources(kinds) {
  const all = { ...Object.fromEntries(STORE_SOURCES.map((s) => [s, s])), ...(kinds || {}) };
  return Object.entries(all).sort(([a], [b]) =>
    (STORE_SOURCES.includes(b) - STORE_SOURCES.includes(a))
    || (STORE_SOURCES.indexOf(a) - STORE_SOURCES.indexOf(b)) || a.localeCompare(b));
}

function ObjectStores() {
  const [datasets, setDatasets] = useState([]);
  const [kinds, setKinds] = useState({});
  const [form, setForm] = useState({ source: "s3", name: "", uri: "", format: "parquet" });
  const [err, setErr] = useState("");

  const load = () =>
    api("/settings/objectstore")
      .then((d) => {
        setDatasets(d.datasets || []);
        setKinds(d.kinds || {});
      })
      .catch((e) => setErr(e.message));
  const sources = storeSources(kinds);
  const hint = (s) => URI_HINT[kinds[s] || s];

  useEffect(() => {
    load();
  }, []);

  async function add() {
    setErr("");
    try {
      await api("/settings/objectstore", {
        method: "POST",
        body: JSON.stringify({
          source: form.source,
          name: form.name.trim(),
          uri: form.uri.trim(),
          format: form.format,
        }),
      });
      setForm({ source: form.source, name: "", uri: "", format: form.format });
      load();
    } catch (e) {
      setErr(e.message);
    }
  }

  async function remove(source, name) {
    await api(`/settings/objectstore/${encodeURIComponent(source)}/${encodeURIComponent(name)}`,
              { method: "DELETE" });
    load();
  }

  return (
    <div className="mcp-block">
      <div className="canvas-title" style={{ fontSize: 15 }}>Object storage datasets</div>
      <div className="meta">
        Object storage has no tables, so nothing is queryable until you name it here.
        Each registered dataset becomes a named view that agents query with ordinary
        SQL under the same RBAC and query guard as a warehouse table — agents never
        see the raw bucket URI.
      </div>
      {err && <div className="error">{err}</div>}
      <div className="job-form-row" style={{ marginTop: 8 }}>
        <select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })}>
          {sources.map(([s, kind]) => (
            <option key={s} value={s}>{s === kind ? s : `${s} (${kind})`}</option>
          ))}
        </select>
        <input className="sqllab-prompt" style={{ maxWidth: 150 }} placeholder="name (the table agents see)"
          value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input className="sqllab-prompt" style={{ flex: 1 }} placeholder={hint(form.source)}
          value={form.uri} onChange={(e) => setForm({ ...form, uri: e.target.value })} />
        <select value={form.format} onChange={(e) => setForm({ ...form, format: e.target.value })}>
          <option value="parquet">parquet</option>
          <option value="csv">csv</option>
          <option value="json">json</option>
        </select>
        <button className="chip chip-on" onClick={add}
          disabled={!form.name.trim() || !form.uri.trim()}>＋ register</button>
      </div>
      <div className="share-list">
        {sources.map(([s]) => {
          const rows = datasets.filter((d) => d.source === s);
          if (rows.length === 0) return null;
          return (
            <Fragment key={s}>
              <div className="meta">{s} — {hint(s)}</div>
              {rows.map((d) => (
                <div key={`${d.source}/${d.name}`} className="share-row">
                  <div>
                    <div>{d.name} <span className="query-tag">{d.format}</span></div>
                    <div className="meta">{d.uri}</div>
                  </div>
                  <button className="chip" onClick={() => remove(d.source, d.name)}>remove</button>
                </div>
              ))}
            </Fragment>
          );
        })}
        {datasets.length === 0 && <div className="meta">No datasets registered.</div>}
      </div>
    </div>
  );
}

export default function Governance({ onClose }) {
  const [text, setText] = useState("");
  const [meta, setMeta] = useState({ loaded: false, source: null });
  const [check, setCheck] = useState(null); // {ok, errors, summary}
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    api("/governance")
      .then((d) => {
        setText(d.yaml || "");
        setMeta({ loaded: d.loaded, source: d.source });
      })
      .catch((e) => setError(e.message));
  }, []);

  async function loadTemplate() {
    setError("");
    try {
      const d = await api("/governance/template");
      setText(d.yaml);
      setCheck(null);
      setNote("Loaded a starter document from your live sources and roles.");
    } catch (e) {
      setError(e.message);
    }
  }

  async function validate() {
    setBusy("validate");
    setError("");
    setNote("");
    try {
      const d = await api("/governance/validate", {
        method: "POST",
        body: JSON.stringify({ yaml: text }),
      });
      setCheck(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function apply() {
    setBusy("apply");
    setError("");
    setNote("");
    try {
      const d = await api("/governance", {
        method: "PUT",
        body: JSON.stringify({ yaml: text }),
      });
      setMeta({ loaded: d.loaded, source: d.source });
      setNote("Applied — access and compliance are now governed by this document.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function clear() {
    if (!confirm("Revert to Studio's built-in RBAC and drop the applied document?")) return;
    setBusy("clear");
    try {
      const d = await api("/governance", { method: "DELETE" });
      setMeta({ loaded: d.loaded, source: null });
      setCheck(null);
      setNote("Reverted to built-in RBAC.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Governance</div>
          <div className="meta">
            One document owns per-role source/table access and per-table compliance
            (deny / mask columns, row caps). {meta.loaded
              ? `Active — loaded from ${meta.source}.`
              : "Not loaded — Studio is using built-in RBAC."}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {note && <div className="meta sqllab-saved">{note}</div>}

      <div className="gov-actions">
        <button className="chip" onClick={loadTemplate}>⎘ load template</button>
        <button className="chip" onClick={validate} disabled={busy === "validate" || !text.trim()}>
          {busy === "validate" ? "checking…" : "✓ validate"}
        </button>
        <button
          className={"chip" + (check?.ok ? " chip-on" : "")}
          onClick={apply}
          disabled={busy === "apply" || !text.trim()}
          title="Applies immediately (validated server-side)"
        >
          {busy === "apply" ? "applying…" : "⇧ apply"}
        </button>
        {meta.loaded && (
          <button className="chip ctx-danger" onClick={clear} disabled={busy === "clear"}>
            ↺ revert to built-in RBAC
          </button>
        )}
      </div>

      {check && (
        <div className={"sqllab-result " + (check.ok ? "ok" : "bad")}>
          {check.ok ? "✓ Valid document" : "✗ " + (check.errors || []).join("; ")}
        </div>
      )}
      {check?.ok && check.summary && (
        <div className="gov-summary">
          <div className="meta">Grants</div>
          {Object.entries(check.summary.roles).map(([role, g]) => (
            <div key={role} className="gov-role">
              <b>{role}</b>:{" "}
              {g === "all sources"
                ? "all sources"
                : Object.entries(g)
                    .map(([s, t]) => `${s} → ${Array.isArray(t) ? t.join(", ") : t}`)
                    .join(" · ")}
            </div>
          ))}
          {Object.keys(check.summary.compliance_tables || {}).length > 0 && (
            <div className="meta" style={{ marginTop: 6 }}>
              Compliance on:{" "}
              {Object.entries(check.summary.compliance_tables)
                .map(([s, ts]) => `${s}(${ts.join(", ")})`)
                .join(" · ")}
            </div>
          )}
        </div>
      )}

      <textarea
        className="gov-yaml"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setCheck(null);
        }}
        spellCheck={false}
        placeholder="Load the template to start from your current setup, or paste a governance document…"
      />

      <Retention />
      <McpServers />
      <GithubRepos />
      <PlanningSourcesPreview />
      <ObjectStores />
    </section>
  );
}
