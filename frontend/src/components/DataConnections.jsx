// Connect to data — the Tableau-style picker.
//
// One grid of source tiles; click the one you want and it asks for exactly that
// source's credentials. The form is not hand-written per source: the backend's
// /connections/types serves a field spec per connectable type and this renders
// it, so a connector gained on the server shows up here with the right fields
// and no frontend change. The tile art comes from sourceCatalog.jsx.
//
// The grid deliberately shows sources this screen cannot set up — object stores
// and the marketing APIs are configured by an operator through environment
// variables — because the useful question is "what can Studio read?", and a
// tile that explains how it gets switched on beats a tile that isn't there.
//
// Nothing here ever sees a stored secret. Microsoft 365 runs its OAuth grant on
// Microsoft's site; database credentials are posted once, encrypted at rest by
// the backend, and only ever read back as a non-secret hint (host / account /
// project). Creating and deleting connections is admin-only on the server —
// this screen mirrors that instead of hiding it, so a non-admin still sees the
// map of available sources and who to ask.
import { useEffect, useMemo, useState } from "react";
import { api, getUser } from "../api";
import { CATEGORIES, SourceIcon, metaFor } from "./sourceCatalog";

const STATUS_LABEL = {
  onboarding: "Onboarding — first sync running",
  connected: "Connected",
  error: "Error — will retry",
  revoked: "Disconnected — reconnect to resume",
};

const NAME_RE = /^[a-z0-9][a-z0-9_-]{1,30}$/;

function fmtWhen(epoch) {
  if (!epoch) return "never";
  return new Date(epoch * 1000).toLocaleString();
}

/** A free source name for a new connection of this type: pg, pg-2, pg-3… */
export function suggestName(ctype, taken) {
  const base = String(ctype).replace(/_/g, "-");
  if (!taken.has(base)) return base;
  for (let i = 2; i < 50; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
  return "";
}

/**
 * The grid's model. A tile is a KIND of source; its instances are the live
 * sources of that kind — the env-configured one that shares the connector's
 * name, plus every user connection built from it. That is why "PostgreSQL" can
 * read "2 connected" while still offering to add a third.
 *
 * Exported because it is the only real logic on this screen, and a pure
 * function is worth testing directly.
 */
export function buildTiles({ types = [], conns = [], sources = [], q = "" }) {
  const byId = new Map();
  const put = (id, extra) => {
    if (!byId.has(id)) {
      byId.set(id, { id, meta: metaFor(id), connectable: false, fields: null, instances: [] });
    }
    return Object.assign(byId.get(id), extra || {});
  };

  for (const t of types) put(t.ctype, { connectable: true, fields: t.fields, ns: t.ns || {} });
  put("m365");

  const connByName = new Map(conns.map((c) => [c.name, c]));
  for (const s of sources) {
    const own = connByName.get(s.name);
    // A user connection whose ctype this bundle doesn't know (server ahead of
    // the frontend) still needs a home; put() gives it a fallback tile.
    const tile = put(own?.ctype || s.ctype || s.name);
    tile.instances.push({
      name: s.name,
      configured: !!s.configured,
      allowed: !!s.allowed,
      dialect: s.dialect,
      kind: own || s.ctype ? "user" : "env",
      connId: own?.id,
      hint: own?.hint || "",
    });
  }

  const list = [...byId.values()];
  const needle = q.trim().toLowerCase();
  return needle
    ? list.filter((t) => (t.meta.label + " " + t.id).toLowerCase().includes(needle))
    : list;
}

export default function DataConnections({ onClose }) {
  const user = getUser();
  const isAdmin = user?.role === "admin";

  const [sources, setSources] = useState([]);   // /catalog/sources — every source
  const [types, setTypes] = useState([]);       // /connections/types — connectable
  const [conns, setConns] = useState([]);       // /connections — user-created
  const [m365, setM365] = useState(null);       // /m365/status
  const [picked, setPicked] = useState(null);   // tile id being viewed, or null
  const [q, setQ] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  function load() {
    api("/catalog/sources").then((d) => setSources(Array.isArray(d) ? d : [])).catch(() => {});
    api("/m365/status").then(setM365).catch(() => setM365({ configured: false }));
    if (isAdmin) {
      api("/connections/types").then((d) => setTypes(Array.isArray(d) ? d : [])).catch(() => {});
      api("/connections").then((d) => setConns(Array.isArray(d) ? d : [])).catch(() => {});
    }
  }

  // Surface the result of an OAuth round-trip: the callback comes back with
  // ?m365_connected=1 or ?m365_error=…. Read it, open the Microsoft 365 tile so
  // the outcome is on screen, then scrub the query so a refresh can't replay it.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.has("m365_connected")) {
      setNote("Microsoft 365 connected — your files and mail are syncing in the background.");
      setPicked("m365");
    } else if (p.has("m365_error")) {
      setError(`Microsoft 365 connection failed: ${p.get("m365_error") || "unknown error"}`);
      setPicked("m365");
    }
    if (p.has("m365_connected") || p.has("m365_error")) {
      p.delete("m365_connected");
      p.delete("m365_error");
      const rest = p.toString();
      window.history.replaceState({}, "", window.location.pathname +
        (rest ? `?${rest}` : "") + window.location.hash);
    }
    load();
  }, [isAdmin]);

  const tiles = useMemo(() => buildTiles({ types, conns, sources, q }),
                        [types, conns, sources, q]);

  const takenNames = useMemo(() => new Set(sources.map((s) => s.name)), [sources]);
  const tile = picked ? tiles.find((t) => t.id === picked) : null;

  function back() {
    setPicked(null);
    setError("");
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal connect-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">
              {tile ? (
                <button className="conn-back" onClick={back}>← Connect to data</button>
              ) : (
                "Connect to data"
              )}
            </div>
            <div className="meta">
              {tile
                ? tile.meta.blurb || `Connect Studio to ${tile.meta.label}.`
                : "Pick a source. Connected sources appear in the chat picker, and credentials are encrypted at rest — never shown again."}
            </div>
          </div>
          <div className="conn-head-right">
            {!tile && (
              <input
                className="conn-search"
                placeholder="Search sources…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            )}
            <button className="chip" onClick={onClose}>✕ close</button>
          </div>
        </div>

        {error && <div className="error">{error}</div>}
        {note && <div className="meta share-notice">{note}</div>}

        <div className="conn-body">
          {!tile ? (
            <Grid tiles={tiles} onPick={(id) => { setPicked(id); setError(""); setNote(""); }} />
          ) : tile.id === "m365" ? (
            <M365Panel
              state={m365}
              reload={load}
              onNote={setNote}
              onError={setError}
            />
          ) : tile.connectable && isAdmin ? (
            <ConnectForm
              tile={tile}
              defaultName={suggestName(tile.id, takenNames)}
              takenNames={takenNames}
              onDone={(msg) => { setNote(msg); setPicked(null); load(); }}
              onError={setError}
              onRemoved={load}
            />
          ) : (
            <DetailPanel tile={tile} isAdmin={isAdmin} />
          )}
        </div>
      </div>
    </div>
  );
}

// ── The grid ────────────────────────────────────────────────────────────

function Grid({ tiles, onPick }) {
  const groups = CATEGORIES
    .map((c) => ({ ...c, items: tiles.filter((t) => t.meta.category === c.key) }))
    .filter((g) => g.items.length);

  if (!groups.length) {
    return <div className="meta conn-empty">No source matches that search.</div>;
  }

  return (
    <>
      {groups.map((g) => (
        <section key={g.key} className="conn-group">
          <div className="conn-group-title">{g.label}</div>
          <div className="conn-grid">
            {g.items
              .slice()
              .sort((a, b) => a.meta.label.localeCompare(b.meta.label))
              .map((t) => <Tile key={t.id} tile={t} onPick={onPick} />)}
          </div>
        </section>
      ))}
    </>
  );
}

function Tile({ tile, onPick }) {
  const live = tile.instances.filter((i) => i.configured);
  const dormant = !live.length && !tile.connectable;
  return (
    <button
      className={`conn-tile${dormant ? " conn-tile-off" : ""}`}
      onClick={() => onPick(tile.id)}
      title={tile.meta.blurb}
    >
      <SourceIcon id={tile.id} size={36} />
      <span className="conn-tile-name">{tile.meta.label}</span>
      <span className="conn-tile-status">
        {live.length
          ? `${live.length} connected`
          : tile.connectable
            ? "Connect"
            : "Not configured"}
      </span>
      {live.length > 0 && <span className="conn-dot" aria-hidden="true" />}
    </button>
  );
}

// ── Connect: credentials, then which schemas to bind ─────────────────────
//
// Tableau's shape: sign in to the server, then choose what to work with. The
// second step is the one that needs care. A Studio source is pinned to ONE
// namespace — qualifiers() refuses every other one, and allowed_tables()
// matches BARE table names, so a source spanning two schemas would let a grant
// for `orders` admit the other schema's `orders` as well. So picking three
// schemas creates three sources, each pinned, each independently grantable in
// Governance. Browsing only decides what to bind; it never widens a binding.

/**
 * Allocate an opaque sibling name (sales-pg-1, sales-pg-2, ...).
 *
 * Source names are visible in the disabled picker even when the caller cannot
 * access their catalog.  Putting a schema name into this public identifier
 * would undo the backend's namespace redaction, so the authorized UI shows the
 * namespace separately and the stored source name stays opaque.
 */
function nameForNamespace(base, taken) {
  for (let i = 1; i < 10000; i++) {
    const suffix = `-${i}`;
    // Reserve room for the suffix. Appending and then slicing would erase it
    // when base is already at the server's 31-character limit.
    const stem = base.slice(0, 31 - suffix.length).replace(/[-_]$/, "");
    const next = `${stem}${suffix}`;
    if (!taken.has(next)) return next;
  }
  throw new Error("Could not generate a unique source name");
}

/**
 * What to POST for the schemas the admin picked: one source per namespace,
 * each carrying the same credential but pinned to its own database/schema.
 *
 * Picking nothing (or a connector with no namespaces, like Neo4j) plans the
 * single source the form already describes. Exported for testing — this is
 * where "three schemas" becomes "three independently governed sources".
 */
export function planConnections({ cfg, ns, name, chosen = [], takenNames = new Set() }) {
  const base = String(name || "").trim();
  const plan = chosen.length
    ? chosen.map((n) => ({
        ns: n,
        config: {
          ...cfg,
          [ns.schema]: n.schema,
          ...(ns.database && n.database ? { [ns.database]: n.database } : {}),
        },
      }))
    : [{ ns: null, config: cfg }];

  // Names are reserved as we go, so two picked schemas cannot collide with
  // each other any more than with a source that already exists.
  const taken = new Set(takenNames);
  return plan.map((p) => {
    // One selected namespace normally keeps the typed name.  If that name is
    // already used, it still needs the same namespace suffixing path as a
    // multi-selection; the form deliberately allows a taken prefix once a
    // namespace is selected.
    const sourceName = plan.length === 1 && (!p.ns || !taken.has(base))
      ? base
      : nameForNamespace(base, taken);
    taken.add(sourceName);
    return { ...p, name: sourceName };
  });
}

export function namespaceKey(n) {
  return `${n?.database || ""}\u0000${n?.schema || ""}`;
}

/** Remove only namespaces whose POSTs actually succeeded after a partial run. */
export function remainingPickedNamespaces(picked, named, completedNames) {
  const completed = new Set(completedNames);
  const landedKeys = new Set(named
    .filter((p) => p.ns && completed.has(p.name))
    .map((p) => namespaceKey(p.ns)));
  return picked.filter((key) => !landedKeys.has(key));
}

/** Which actions the current credential/scope values can safely enable. */
export function connectionReadiness({ fields = [], ns = {}, cfg = {}, chosen = [] }) {
  const namespaceKeys = new Set([ns.schema, ns.database].filter(Boolean));
  const present = (value) => String(value || "").trim().length > 0;
  const pickedValue = (field) => {
    if (!chosen.length) return false;
    if (field === ns.schema) return chosen.every((item) => present(item.schema));
    if (field === ns.database) return chosen.every((item) => present(item.database));
    return false;
  };
  return {
    // Discovery intentionally precedes picking a namespace.
    credentialsFilled: fields.every((f) => !f.required || namespaceKeys.has(f.key) || present(cfg[f.key])),
    // Saving remains strict: every required value must be typed or supplied by
    // every picked namespace.  The backend independently rechecks this.
    saveFilled: fields.every((f) => !f.required || present(cfg[f.key]) || pickedValue(f.key)),
  };
}

function ConnectForm({ tile, defaultName, takenNames, onDone, onError, onRemoved }) {
  const fields = tile.fields || [];
  const ns = tile.ns || {};
  const [name, setName] = useState(defaultName);
  const [cfg, setCfg] = useState(() =>
    Object.fromEntries(fields.filter((f) => f.default).map((f) => [f.key, f.default])));
  const [test, setTest] = useState(null);       // null | {ok, tables?, sample?, error?}
  const [browse, setBrowse] = useState(null);   // null | {ok, namespaces[], error?}
  const [picked, setPicked] = useState([]);     // "db\u0000schema" keys
  const [landedNames, setLandedNames] = useState([]); // successes from a partial batch
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState("");

  // A credential edit invalidates everything downstream of it.
  function setField(k, v) {
    setCfg((c) => ({ ...c, [k]: v }));
    setTest(null);
    setBrowse(null);
    setPicked([]);
  }

  // The namespace step only exists where the connector has namespaces to offer
  // (Neo4j does not) and the type says which fields a pick writes into.
  const canBrowse = !!ns.schema;
  const key = namespaceKey;

  async function runTest() {
    setBusy(canBrowse ? "browse" : "test");
    setTest(null);
    setBrowse(null);
    try {
      if (canBrowse) {
        // Discovery itself is the credential probe.  /test lists tables and
        // therefore cannot succeed until a namespace has already been picked.
        const result = await api("/connections/browse", {
          method: "POST", body: JSON.stringify({ ctype: tile.id, config: cfg }),
        });
        setBrowse(result);
        if (result.ok) setTest({ ok: true, discovery: true });
      } else {
        setTest(await api("/connections/test", {
          method: "POST", body: JSON.stringify({ ctype: tile.id, config: cfg }),
        }));
      }
    } catch (e) {
      if (canBrowse) setBrowse({ ok: false, error: e.message, namespaces: [] });
      else setTest({ ok: false, error: e.message });
    } finally {
      setBusy("");
    }
  }

  const found = browse?.namespaces || [];
  const chosen = found.filter((n) => picked.includes(key(n)));

  async function save() {
    setBusy("save");
    onError("");
    const effectiveTaken = new Set([...takenNames, ...landedNames]);
    const named = planConnections({ cfg, ns, name, chosen, takenNames: effectiveTaken });

    // Sequential, not parallel: each POST re-probes the warehouse, and a
    // partial failure has to name the source that failed and keep the ones
    // that already landed rather than leaving an indeterminate set.
    const done = [];
    for (const p of named) {
      setProgress(`connecting ${p.name} (${done.length + 1} of ${named.length})…`);
      try {
        await api("/connections", {
          method: "POST",
          body: JSON.stringify({ name: p.name, ctype: tile.id, config: p.config }),
        });
        done.push(p.name);
      } catch (e) {
        setBusy("");
        setProgress("");
        onError(done.length
          ? `Connected ${done.join(", ")}, then “${p.name}” failed: ${e.message}`
          : e.message);
        if (done.length) {
          // A retry must target only the namespaces that did not land. Keep a
          // local name reservation too: the parent's reload is asynchronous,
          // so a fast second click must not plan a colliding source name.
          setPicked((current) => remainingPickedNamespaces(current, named, done));
          setLandedNames((current) => [...new Set([...current, ...done])]);
          onRemoved();                  // reload so the landed sources show
        }
        return;
      }
    }
    setBusy("");
    setProgress("");
    onDone(done.length === 1
      ? `Connected — “${done[0]}” is now a source in the chat picker (open a new chat or refresh to see it).`
      : `Connected ${done.length} sources — ${done.join(", ")} — each scoped to its own schema.`);
  }

  async function remove(inst) {
    if (!confirm(`Remove the “${inst.name}” connection? Queries against it will stop working.`)) return;
    try {
      await api(`/connections/${inst.connId}`, { method: "DELETE" });
      onRemoved();
    } catch (e) {
      onError(e.message);
    }
  }

  const trimmed = name.trim();
  const effectiveTakenNames = new Set([...takenNames, ...landedNames]);
  const nameOk = NAME_RE.test(trimmed) && !effectiveTakenNames.has(trimmed);
  const { credentialsFilled, saveFilled } = connectionReadiness({ fields, ns, cfg, chosen });
  // With schemas picked the name is a PREFIX, so a collision on the bare name
  // is fine — nameForNamespace suffixes each one and dodges what is taken.
  const nameUsable = chosen.length ? NAME_RE.test(trimmed) : nameOk;

  return (
    <div className="conn-detail">
      <Instances tile={tile} onRemove={remove} />

      <div className="conn-form">
        {fields.map((f) => (
          <label key={f.key} className={`conn-field${f.key === "dsn" || f.key === "credentials_json" ? " conn-field-wide" : ""}`}>
            <span className="conn-label">
              {f.label}{f.required && <b className="conn-req"> *</b>}
            </span>
            <input
              type={f.secret ? "password" : "text"}
              placeholder={f.placeholder || ""}
              value={cfg[f.key] ?? ""}
              onChange={(e) => setField(f.key, e.target.value)}
              disabled={!!busy}
              autoComplete="off"
            />
          </label>
        ))}
        <label className="conn-field">
          <span className="conn-label">
            {chosen.length > 1 ? "Name these in Studio (prefix)" : "Name it in Studio"}
          </span>
          <input value={name} onChange={(e) => setName(e.target.value)}
                 disabled={!!busy} autoComplete="off" />
        </label>
      </div>

      {trimmed && !nameUsable && (
        <div className="meta conn-warn">
          {effectiveTakenNames.has(trimmed) && !chosen.length
            ? `A source named “${trimmed}” already exists — pick another name.`
            : "Name must be 2–31 characters: lowercase letters, digits, - or _."}
        </div>
      )}

      {test && (test.ok
        ? <div className="meta share-notice">
            {test.discovery
              ? `✓ Signed in — ${browse?.namespaces?.length || 0} schemas visible`
              : `✓ Connected — ${test.tables} tables${test.sample?.length ? `: ${test.sample.join(", ")}` : ""}`}
          </div>
        : <div className="error">{test.error}</div>)}

      {busy === "browse" && <div className="meta">Looking up schemas…</div>}
      {browse && <Namespaces
        browse={browse} picked={picked} setPicked={setPicked} keyOf={key} />}

      <div className="m365-actions">
        <button className="chip" onClick={runTest} disabled={!!busy || !credentialsFilled}>
          {busy === "test" ? "signing in…" : busy === "browse" ? "reading schemas…"
            : canBrowse ? "⚡ Sign in and list schemas" : "⚡ Test connection"}
        </button>
        <button className="primary" onClick={save} disabled={!!busy || !saveFilled || !nameUsable}>
          {busy === "save" ? (progress || "connecting…")
            : chosen.length > 1 ? `✓ Connect ${chosen.length} schemas` : "✓ Connect"}
        </button>
      </div>
      <div className="meta">
        Credentials are encrypted at rest and never displayed again. Each schema becomes
        its own source, scoped and granted separately — grant them to other roles in
        Governance.
      </div>
    </div>
  );
}

/** The picked-schema checklist, grouped by database / catalog / project. */
export function Namespaces({ browse, picked, setPicked, keyOf }) {
  if (!browse.ok) {
    return (
      <div className="meta conn-warn">
        Couldn't list schemas ({browse.error}). Type the schema in the field above and
        connect — this only affects the picker, not the connection.
      </div>
    );
  }
  if (!browse.namespaces.length) {
    return <div className="meta">No schemas visible to this account — type one above.</div>;
  }

  const groups = [];
  for (const n of browse.namespaces) {
    const db = n.database || "";
    const g = groups.find((x) => x.db === db) || (groups.push({ db, items: [] }), groups.at(-1));
    g.items.push(n);
  }

  function toggle(k) {
    setPicked((p) => p.includes(k) ? p.filter((x) => x !== k) : [...p, k]);
  }

  return (
    <div className="conn-ns">
      <div className="conn-ns-head">
        <span className="conn-label">
          Schemas this account can see — pick the ones to add as sources
        </span>
        <span className="meta">{picked.length} selected</span>
      </div>
      {groups.map((g) => (
        <div key={g.db} className="conn-ns-group">
          {g.db && <div className="conn-ns-db">{g.db}</div>}
          <div className="conn-ns-list">
            {g.items.map((n) => {
              const k = keyOf(n);
              return (
                <label key={k} className={`conn-ns-item${picked.includes(k) ? " conn-ns-on" : ""}`}>
                  <input type="checkbox" checked={picked.includes(k)} onChange={() => toggle(k)} />
                  <span>{n.schema}</span>
                </label>
              );
            })}
          </div>
        </div>
      ))}
      {browse.truncated && (
        <div className="meta">Showing the first {browse.namespaces.length} — type a name above to bind one not listed.</div>
      )}
    </div>
  );
}

// ── Detail: sources this screen can't set up, and the non-admin view ─────

function DetailPanel({ tile, isAdmin }) {
  // A non-admin cannot read /connections/types, so `tile.connectable` is false
  // for every tile in their session — it says nothing about the source and must
  // not drive the copy. What is true for them is the same either way: someone
  // with the admin role has to set this up. Admins fall through to the env
  // explanation, since the only tiles that reach here are the ones this screen
  // cannot configure; the rows above already show which are live.
  return (
    <div className="conn-detail">
      <Instances tile={tile} />
      <div className="meta">
        {isAdmin
          ? "This source is configured by an operator through environment variables on the deployment (see .env.example), not from this screen. Once its keys are set it appears in the chat picker like any other source."
          : "An administrator connects sources. Sources your role may use appear in the chat picker automatically — ask an admin to connect this one and grant it to your role."}
      </div>
    </div>
  );
}

function Instances({ tile, onRemove }) {
  if (!tile.instances.length) return null;
  return (
    <div className="conn-instances">
      {tile.instances.map((i) => (
        <div key={i.name} className="dbconn-row">
          <SourceIcon id={tile.id} size={20} />
          <b>{i.name}</b>
          {i.hint && <span className="meta">{i.hint}</span>}
          <span className={`m365-badge${i.configured ? " m365-badge-on" : " m365-badge-off"}`}>
            {i.configured ? (i.kind === "env" ? "configured by environment" : "connected") : "needs reconnect"}
          </span>
          {onRemove && i.kind === "user" && (
            <button className="chip ctx-danger" onClick={() => onRemove(i)}>✕</button>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Microsoft 365: OAuth, not a credential form ──────────────────────────

function M365Panel({ state, reload, onNote, onError }) {
  const [busy, setBusy] = useState("");
  const configured = state?.configured;
  const connected = state?.connected;

  async function connect() {
    setBusy("connect");
    onError("");
    onNote("");
    try {
      const d = await api("/m365/connect", { method: "POST" });
      if (d.configured === false) {
        reload();
      } else if (d.authorize_url) {
        // Delegated OAuth: hand off to Microsoft; we come back via the callback.
        window.location.href = d.authorize_url;
        return;
      } else {
        // App mode: provisioned server-side, no redirect needed.
        onNote("Microsoft 365 connected — syncing in the background.");
        reload();
      }
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function sync() {
    setBusy("sync");
    onError("");
    onNote("");
    try {
      await api("/m365/sync", { method: "POST" });
      onNote("Sync queued — new and changed items will appear shortly.");
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function disconnect() {
    if (!confirm("Disconnect Microsoft 365? Synced documents stop grounding agent answers until you reconnect.")) return;
    setBusy("disconnect");
    onError("");
    onNote("");
    try {
      await api("/m365/connect", { method: "DELETE" });
      onNote("Disconnected. Your tokens were wiped.");
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  if (state == null) return <div className="meta conn-empty">Loading connection status…</div>;

  if (configured === false) {
    return (
      <div className="conn-detail">
        <div className="meta">
          Microsoft 365 isn't set up on this deployment. An administrator needs to
          configure the Azure app credentials before this connection is available.
        </div>
      </div>
    );
  }

  return (
    <div className="conn-detail">
      {connected ? (
        <>
          <div className="m365-facts">
            <span className="m365-badge m365-badge-on">{STATUS_LABEL[state.status] || "Connected"}</span>
            {state.mode && (
              <span className="query-tag" title="how Studio authenticates to Graph">
                {state.mode === "app" ? "app (tenant-wide)" : "delegated (your account)"}
              </span>
            )}
            <span className="query-tag">{state.item_count ?? 0} items synced</span>
            <span className="meta">last sync {fmtWhen(state.last_sync)}</span>
          </div>
          <div className="m365-actions">
            <button className="chip chip-on" onClick={sync} disabled={!!busy || state.status === "revoked"}>
              {busy === "sync" ? "queuing…" : "↻ Sync now"}
            </button>
            {state.status === "revoked" && (
              <button className="chip" onClick={connect} disabled={!!busy}>
                {busy === "connect" ? "connecting…" : "↗ Reconnect"}
              </button>
            )}
            <button className="chip ctx-danger" onClick={disconnect} disabled={!!busy}>
              {busy === "disconnect" ? "disconnecting…" : "✕ Disconnect"}
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="meta">
            Connect your account to let Studio index your files and mail. You'll be sent to
            Microsoft to sign in and grant read-only access — Studio only ever receives an
            access token it stores encrypted, never your password.
          </div>
          <div className="m365-actions">
            <button className="primary" onClick={connect} disabled={!!busy}>
              {busy === "connect" ? "connecting…" : "↗ Connect Microsoft 365"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
