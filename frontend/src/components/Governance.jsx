// Governance-as-code editor (admin only). One YAML owns per-role source/table
// access and per-table compliance (deny/mask columns, row caps). Validate
// before applying; applying hot-reloads with no redeploy. Clearing reverts to
// Studio's built-in RBAC.
import { Fragment, useEffect, useState } from "react";
import { api } from "../api";

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
  const [form, setForm] = useState({ name: "", url: "", description: "" });
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
        }),
      });
      setForm({ name: "", url: "", description: "" });
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
        Register the repos where your scripts live. When someone builds a pipeline,
        the agent picks the repo whose scripts best fit the prompt and pulls them in
        as context. (Set a GITHUB_TOKEN env var to read private repos.)
      </div>
      {err && <div className="error">{err}</div>}
      <div className="job-form-row" style={{ marginTop: 8 }}>
        <input className="sqllab-prompt" style={{ maxWidth: 150 }} placeholder="name"
          value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input className="sqllab-prompt" style={{ flex: 1 }} placeholder="https://github.com/org/repo"
          value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
        <input className="sqllab-prompt" style={{ flex: 1 }} placeholder="what it does (keywords help matching)"
          value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        <button className="chip chip-on" onClick={add}
          disabled={!form.name.trim() || !form.url.includes("github.com")}>＋ register</button>
      </div>
      <div className="share-list">
        {repos.map((r) => (
          <div key={r.name} className="share-row">
            <div>
              <div>{r.name}</div>
              <div className="meta">{r.url}{r.description ? ` — ${r.description}` : ""}</div>
            </div>
            <button className="chip" onClick={() => remove(r.name)}>remove</button>
          </div>
        ))}
        {repos.length === 0 && <div className="meta">No repositories registered.</div>}
      </div>
    </div>
  );
}

// Order here is the display order of the groups below.
const STORE_SOURCES = ["s3", "azure_blob", "gcs"];
const URI_HINT = {
  s3: "s3://bucket/prefix/*.parquet",
  azure_blob: "az://container/path/*.parquet",
  gcs: "gs://bucket/prefix/*.parquet",
};

function ObjectStores() {
  const [datasets, setDatasets] = useState([]);
  const [form, setForm] = useState({ source: "s3", name: "", uri: "", format: "parquet" });
  const [err, setErr] = useState("");

  const load = () =>
    api("/settings/objectstore")
      .then((d) => setDatasets(d.datasets || []))
      .catch((e) => setErr(e.message));

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
    await api(`/settings/objectstore/${source}/${name}`, { method: "DELETE" });
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
          {STORE_SOURCES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <input className="sqllab-prompt" style={{ maxWidth: 150 }} placeholder="name (the table agents see)"
          value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input className="sqllab-prompt" style={{ flex: 1 }} placeholder={URI_HINT[form.source]}
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
        {STORE_SOURCES.map((s) => {
          const rows = datasets.filter((d) => d.source === s);
          if (rows.length === 0) return null;
          return (
            <Fragment key={s}>
              <div className="meta">{s} — {URI_HINT[s]}</div>
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

      <McpServers />
      <GithubRepos />
      <ObjectStores />
    </section>
  );
}
