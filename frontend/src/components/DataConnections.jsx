// Connect to data — the Tableau-style picker.
//
// One grid of source tiles; click the one you want and it asks for exactly that
// source's credentials. The form is not hand-written per source: the backend's
// /connections/types serves a field spec per connectable type and this renders
// it, so a connector gained on the server shows up here with the right fields
// and no frontend change. GitHub is a separate pipeline-planning context, not
// a SQL data connector; its registry has a dedicated panel. Microsoft Teams and
// Slack are where answers are sent, not sources: their tiles list the channels
// set up for sending (DestinationsPanel). The tile art comes
// from sourceCatalog.jsx.
//
// The grid deliberately shows sources this screen cannot set up — the marketing
// APIs are configured by an operator through environment variables — because
// the useful question is "what can Studio read?", and a tile that explains how
// it gets switched on beats a tile that isn't there. Object stores (S3, Azure
// Blob, GCS) are set up here: keys plus the first files to read as a table,
// and more files from the source's row (StoreFiles).
//
// Nothing here ever sees a stored secret. Microsoft 365 runs its OAuth grant on
// Microsoft's site; database credentials are posted once, encrypted at rest by
// the backend, and only ever read back as a non-secret hint (host / account /
// project). Creating, disconnecting and removing connections is admin-only on the server —
// this screen mirrors that instead of hiding it, so a non-admin still sees the
// map of available sources and who to ask.
import { useEffect, useMemo, useState } from "react";
import { api, getUser } from "../api";
import { CATEGORIES, FEATURED, MARKETPLACE, SourceIcon, metaFor } from "./sourceCatalog";
import MetadataLine, { metadataText, useMetadataStatus } from "./MetadataLine";
import { backdropClose } from "../backdrop";
import DestinationsPanel from "./DestinationsPanel";

const STATUS_LABEL = {
  onboarding: "Onboarding — first sync running",
  connected: "Connected",
  error: "Error — will retry",
  revoked: "Disconnected — reconnect to resume",
};

const NAME_RE = /^[a-z0-9][a-z0-9_-]{1,30}$/;

// A connection form is not a login form, but browsers treat any password input
// as one: autoComplete="off" is ignored there, and the SAVED Studio login lands
// in the form — the email in whatever text field precedes the secret (an HTTP
// path, an app ID) and the Studio password in the token field, ready to be
// sent to a warehouse. "new-password" tells the browser it has no saved value
// for this field; the data-* flags do the same for 1Password, LastPass,
// Bitwarden and Dashlane, which run their own heuristics.
const NO_AUTOFILL = { "data-1p-ignore": "true", "data-lpignore": "true",
                      "data-bwignore": "true", "data-form-type": "other" };

// Fields whose values run long (a URL, a key file, a path, a connection string).
const WIDE_FIELDS = new Set(["dsn", "credentials_json", "path", "connection_string"]);

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

/** The object stores: a source of files, which become tables once named. */
export const STORES = ["s3", "azure_blob", "gcs"];

const FILE_FORMATS = { parquet: "parquet", pq: "parquet", csv: "csv", tsv: "csv",
                       json: "json", jsonl: "json", ndjson: "json" };

/** The format a path's files are in, by their extension ("" when it can't
 * tell) — the server's objectstore.guess_format. */
export function guessFormat(path) {
  const tail = String(path || "").toLowerCase().split("/").pop().replace(/\.gz$/, "");
  const ext = tail.includes(".") ? tail.split(".").pop() : "";
  return FILE_FORMATS[ext] || "";
}

/** A store's name from its bucket or container (s3://trips/… → trips), free
 * of the names taken; "" when the path names none. */
export function nameFromPath(path, taken) {
  const bucket = String(path || "").split("://")[1]?.split("/")[0] || "";
  const base = bucket.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^[-_]+|-+$/g, "")
    .slice(0, 28).replace(/-+$/, "");
  return base.length >= 2 ? suggestName(base, taken) : "";
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
/**
 * How a marketplace tile connects, given what this deployment offers:
 *   "ready"  — opens its native type's form, pre-filled;
 *   "driver" — its type is here but its driver is not installed;
 *   "admin"  — only an administrator sees connection types (or the type is off);
 *   "soon"   — not available yet.
 * Exported for tests.
 */
export function marketplaceStatus(via, typeById) {
  if (!via || !via.ctype) return "soon";
  const base = typeById.get(via.ctype);
  if (!base) return "admin";
  if (via.dialect && !(base.drivers || []).some((d) => d.split("+")[0] === via.dialect)) {
    return "driver";
  }
  if (base.available === false) return "driver";
  return "ready";
}

// The Any-SQL tile a connection belongs on, by the engine its URL names.
const SQL_TILE_BY_DIALECT = {};
for (const [id, via] of Object.entries(MARKETPLACE)) {
  if (via.ctype === "sql" && via.dialect && !SQL_TILE_BY_DIALECT[via.dialect]) {
    SQL_TILE_BY_DIALECT[via.dialect] = id;
  }
}

function sqlTile(ctype, dialect) {
  return ctype === "sql" && SQL_TILE_BY_DIALECT[dialect] ? SQL_TILE_BY_DIALECT[dialect] : ctype;
}

/** Tiles for where answers are sent, rather than sources Studio reads. */
export const SEND_TO = ["teams", "slack"];

export function buildTiles({ types = [], conns = [], sources = [], repos = [], envs = [],
                             channels = [], q = "" }) {
  const byId = new Map();
  const put = (id, extra) => {
    if (!byId.has(id)) {
      byId.set(id, { id, meta: metaFor(id), connectable: false, fields: null, instances: [] });
    }
    return Object.assign(byId.get(id), extra || {});
  };

  for (const t of types) {
    put(t.ctype, { connectable: true, fields: t.fields, ns: t.ns || {}, spans: !!t.spans });
  }
  put("m365");
  put("github");
  put("teams");
  put("slack");
  // The featured integrations are on everyone's map, connectable or not.
  for (const id of FEATURED) put(id);
  // The marketplace: every integration on the map, each saying how it connects.
  const typeById = new Map(types.map((t) => [t.ctype, t]));
  for (const [id, via] of Object.entries(MARKETPLACE)) {
    const status = marketplaceStatus(via, typeById);
    const base = typeById.get(via.ctype);
    put(id, { via: { ...via, status }, connectable: status === "ready",
              fields: base ? base.fields : null, ns: base ? base.ns || {} : {},
              spans: !!base?.spans, ctype: via.ctype || null });
  }

  for (const repo of repos) {
    put("github").instances.push({
      name: repo.name,
      url: repo.url,
      description: repo.description || "",
      defaultBranch: repo.default_branch || "main",
      configured: true,
      kind: "repository",
    });
  }

  // Channels answers can be sent to (/share/destinations) sit on their own tile.
  for (const ch of channels) {
    if (!SEND_TO.includes(ch.kind)) continue;
    put(ch.kind).instances.push({ name: ch.name, id: ch.id, roles: ch.roles || [],
                                  configured: true, kind: "channel" });
  }

  const connByName = new Map(conns.map((c) => [c.name, c]));
  const envByName = new Map(envs.map((e) => [e.name, e]));
  for (const s of sources) {
    const own = connByName.get(s.name);
    // A user connection whose ctype this bundle doesn't know (server ahead of
    // the frontend) still needs a home; put() gives it a fallback tile.
    const tile = put(sqlTile(own?.ctype || s.ctype || s.name, s.dialect));
    tile.instances.push({
      name: s.name,
      configured: !!s.configured,
      allowed: !!s.allowed,
      dialect: s.dialect,
      kind: own || s.ctype ? "user" : "env",
      connId: own?.id,
      hint: own?.hint || envByName.get(s.name)?.hint || "",
      // Which sign-in it uses (a fingerprint, no credential), what it reads,
      // and whether that is everything the sign-in can see.
      account: own?.account || envByName.get(s.name)?.account || "",
      namespace: own?.namespace || envByName.get(s.name)?.namespace || "",
      everything: !!own?.everything,
      // How an admin set a built-in source on this screen (/connections/env):
      // "environment" | "replaced" | "disconnected". Absent for user
      // connections — and for everyone but admins, who alone can change it.
      mode: own || s.ctype ? undefined : envByName.get(s.name)?.mode,
      disconnected: false,
    });
  }
  // A disconnected connection is gone from the catalog — that is what
  // disconnecting means — so only /connections knows it exists. It still
  // belongs on its tile, where the admin can reconnect or remove it.
  for (const c of conns) {
    if (c.enabled !== false) continue;
    put(sqlTile(c.ctype, (c.hint || "").split(" · ")[0])).instances.push({
      name: c.name,
      configured: false,
      allowed: false,
      dialect: "",
      kind: "user",
      connId: c.id,
      hint: c.hint || "",
      account: c.account || "",
      namespace: c.namespace || "",
      everything: !!c.everything,
      disconnected: true,
    });
  }

  const list = [...byId.values()];
  const needle = q.trim().toLowerCase();
  return needle
    ? list.filter((t) => (t.meta.label + " " + t.id + " " + (t.meta.blurb || ""))
        .toLowerCase().includes(needle))
    : list;
}

export default function DataConnections({ onClose }) {
  const user = getUser();
  const isAdmin = user?.role === "admin";

  const [sources, setSources] = useState([]);   // /catalog/sources — every source
  const [types, setTypes] = useState([]);       // /connections/types — connectable
  const [conns, setConns] = useState([]);       // /connections — user-created
  const [envs, setEnvs] = useState([]);         // /connections/env — built-in sources' settings
  const [m365, setM365] = useState(null);       // /m365/status
  const [picked, setPicked] = useState(null);   // tile id being viewed, or null
  const [q, setQ] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [repos, setRepos] = useState([]);       // configured GitHub planning repositories
  const [channels, setChannels] = useState([]); // /share/destinations — Teams and Slack
  // What Studio has scanned from each connected source, for the Connected
  // sources panel (administrators), and the names being queued for a rescan.
  const scans = useMetadataStatus(isAdmin);
  const [rescanning, setRescanning] = useState([]);
  const [combining, setCombining] = useState("");  // the sign-in group being combined

  async function rediscover(names) {
    setRescanning((now) => [...now, ...names]);
    setError("");
    try {
      for (const name of names) {
        await api(`/metagraph/sources/${encodeURIComponent(name)}/discover`, { method: "POST" });
      }
    } catch (e) {
      setError(e.message);
    } finally {
      await scans.reload();          // queued now: the status keeps itself current
      setRescanning((now) => now.filter((n) => !names.includes(n)));
    }
  }

  // “Combine into one”: the source that already reads everything — else the
  // first by name — reads everything, and the others with its sign-in go.
  async function combine(group) {
    const keep = keeperOf(group);
    const others = group.sources.filter((s) => s !== keep).map((s) => s.name);
    if (!confirm(everythingQuestion(keep.name, others))) return;
    setCombining(group.key);
    setError("");
    setNote("");
    try {
      setNote(combinedNote(await api(`/connections/${keep.connId}/everything`, { method: "POST" })));
      load();
      await scans.reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setCombining("");
    }
  }

  function load() {
    api("/catalog/sources").then((d) => setSources(Array.isArray(d) ? d : [])).catch(() => {});
    api("/m365/status").then(setM365).catch(() => setM365({ configured: false }));
    api("/share/destinations").then((d) => setChannels(d.destinations || [])).catch(() => {});
    if (isAdmin) {
      api("/settings/repos").then((d) => setRepos(d.repos || [])).catch((e) => setError(e.message));
    } else if (user?.role === "analyst") {
      api("/repos").then((d) => setRepos(d.repos || [])).catch((e) => setError(e.message));
    }
    if (isAdmin) {
      api("/connections/types").then((d) => setTypes(Array.isArray(d) ? d : [])).catch(() => {});
      api("/connections").then((d) => setConns(Array.isArray(d) ? d : [])).catch(() => {});
      api("/connections/env").then((d) => setEnvs(Array.isArray(d) ? d : [])).catch(() => {});
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

  const tiles = useMemo(() => buildTiles({ types, conns, sources, repos, envs, channels, q }),
                        [types, conns, sources, repos, envs, channels, q]);

  // A disconnected connection is absent from /catalog/sources but still owns
  // its name on the server, so it counts as taken here too.
  const takenNames = useMemo(() => new Set([...sources.map((s) => s.name),
                                            ...conns.map((c) => c.name)]), [sources, conns]);
  const tile = picked ? tiles.find((t) => t.id === picked) : null;

  function back() {
    setPicked(null);
    setError("");
  }

  return (
    <div className="modal-backdrop" {...backdropClose(onClose)}>
      <div className="modal connect-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">
              {tile ? (
                <button className="conn-back" onClick={back}>← All integrations</button>
              ) : (
                "Integrations marketplace"
              )}
            </div>
            <div className="meta">
              {tile
                ? tile.meta.blurb || `Connect Studio to ${tile.meta.label}.`
                : "Connect a database, warehouse, app or workspace. Connected sources appear in the chat data picker; GitHub repositories are selected when building a pipeline."}
            </div>
          </div>
          <div className="conn-head-right">
            {!tile && (
              <input
                className="conn-search"
                placeholder="Search integrations…"
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
            <>
              {isAdmin && !q.trim() && (
                <ConnectedSources tiles={tiles} scans={scans.byName} rescanning={rescanning}
                  onRediscover={rediscover} onCombine={combine} combining={combining}
                  onPick={(id) => { setPicked(id); setError(""); setNote(""); }} />
              )}
              <Grid tiles={tiles} isAdmin={isAdmin} featured={!q.trim()}
                onPick={(id) => { setPicked(id); setError(""); setNote(""); }} />
            </>
          ) : tile.id === "m365" ? (
            <M365Panel
              state={m365}
              reload={load}
              onNote={setNote}
              onError={setError}
            />
          ) : tile.id === "github" ? (
            <GithubPanel repos={repos} isAdmin={isAdmin} reload={load}
              onNote={setNote} onError={setError} />
          ) : SEND_TO.includes(tile.id) ? (
            <DestinationsPanel kind={tile.id} channels={tile.instances} isAdmin={isAdmin}
              reload={load} onNote={setNote} onError={setError} />
          ) : tile.connectable && isAdmin ? (
            <ConnectForm
              tile={tile}
              preset={tile.via?.preset}
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

// ── Connected sources: what Studio has scanned from each ───────────────

/** A source with no scan to show for: never scanned, or its first scan failed. */
export function needsScan(summary) {
  if (!summary || !summary.status || summary.status === "not_discovered") return true;
  return summary.status === "failed";
}

/** What a source reads, for a listing: "all catalogs" for one that reads
 * everything its sign-in can see (the server's own word for it). */
export function scopeText(inst) {
  return inst.everything ? "all catalogs" : inst.namespace || "";
}

/**
 * Connected sources grouped by sign-in — one entry per account. A source whose
 * sign-in Studio does not know (demo; a built-in one not set up here) is its
 * own entry. Exported for tests.
 */
export function signInGroups(tiles) {
  const groups = new Map();
  for (const tile of tiles) {
    if (tile.id === "github" || tile.id === "m365" || SEND_TO.includes(tile.id)) continue;
    for (const inst of tile.instances) {
      if (!inst.configured || inst.disconnected) continue;
      const key = inst.account ? `${tile.id}\u0000${inst.account}` : `\u0000${inst.name}`;
      if (!groups.has(key)) groups.set(key, { key, tile, sources: [] });
      groups.get(key).sources.push(inst);
    }
  }
  return [...groups.values()]
    .map((g) => ({ ...g, sources: [...g.sources].sort((a, b) => a.name.localeCompare(b.name)) }))
    .sort((a, b) => a.sources[0].name.localeCompare(b.sources[0].name));
}

/** Which source “Combine into one” keeps: one that already reads everything,
 * else the first by name. */
export function keeperOf(group) {
  return group.sources.find((s) => s.everything) || group.sources[0];
}

/** A group that is one sign-in connected several times where one source could
 * read all of it: “Combine into one” applies (connections made here only). */
export function combinable(group) {
  return group.sources.length > 1 && !!group.tile.spans
    && group.sources.every((s) => s.kind === "user" && s.connId);
}

/**
 * Every connected source in one place, for administrators: one compact line
 * per sign-in — what it reads, what Studio has scanned from it (the metadata
 * Auto routes questions with) and a Rediscover button. One sign-in connected
 * several times shows as one entry, with “Combine into one” where a single
 * source can read it all. Stateless: the scan status, the rescans and the
 * combine in flight belong to the marketplace.
 */
export function ConnectedSources({ tiles, scans, rescanning, onRediscover, onPick,
                                   onCombine = null, combining = "" }) {
  const groups = signInGroups(tiles);
  if (!groups.length) return null;
  const names = groups.flatMap((g) => g.sources.map((s) => s.name));
  const running = (name) => rescanning.includes(name)
    || ["queued", "running"].includes(scans[name]?.status);
  const waiting = names.filter((name) => needsScan(scans[name]) && !running(name));
  const scanned = (inst) => (
    <span className="metadata-line meta"
          title={scans[inst.name]?.error || scans[inst.name]?.mirror_error || ""}>
      <span>{metadataText(scans[inst.name]).replace(/^Metadata: /, "")}</span>
      <button className="chip" onClick={() => onRediscover([inst.name])}
              disabled={running(inst.name)} title={`Read ${inst.name}'s tables and columns again`}>
        {running(inst.name) ? "discovering…" : "↻ Rediscover"}
      </button>
    </span>
  );

  return (
    <section className="conn-group conn-connected" aria-label="Connected sources">
      <div className="conn-group-title conn-connected-head">
        <span>Connected · {groups.length}</span>
        {waiting.length > 0 && (
          <button className="chip" onClick={() => onRediscover(waiting)}>
            ↻ Rediscover {waiting.length} not scanned
          </button>
        )}
      </div>
      <div className="conn-connected-list">
        {groups.map((g) => {
          const many = g.sources.length > 1;
          const first = g.sources[0];
          const where = many ? [g.tile.meta.label, first.hint]
            : [g.tile.meta.label, first.hint, scopeText(first)];
          return (
            <div key={g.key} className={`conn-connected-row${combinable(g) ? " conn-connected-dup" : ""}`}>
              <div className="conn-connected-line">
                <SourceIcon id={g.tile.id} size={18} />
                <b>{many ? g.tile.meta.label : first.name}</b>
                <span className="meta conn-connected-where">
                  {where.filter(Boolean).slice(many ? 1 : 0).join(" · ")}
                </span>
                {!many && scanned(first)}
                <button className="chip conn-connected-open" onClick={() => onPick(g.tile.id)}>
                  Manage
                </button>
              </div>
              {many && (
                <>
                  <div className="meta conn-connected-why">
                    {combinable(g)
                      ? `One sign-in connected ${g.sources.length} times — each reads part of the `
                        + "same data. One source can read all of it."
                      : `${g.sources.length} schemas through one sign-in.`}
                    {combinable(g) && onCombine && (
                      <button className="chip conn-combine" onClick={() => onCombine(g)}
                              disabled={!!combining}>
                        {combining === g.key ? "combining…" : "⧉ Combine into one"}
                      </button>
                    )}
                  </div>
                  {g.sources.map((inst) => (
                    <div key={inst.name} className="conn-connected-sub">
                      <b>{inst.name}</b>
                      <span className="meta conn-connected-where">{scopeText(inst)}</span>
                      {scanned(inst)}
                    </div>
                  ))}
                </>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ── The grid ────────────────────────────────────────────────────────────

function Grid({ tiles, isAdmin, onPick, featured = false }) {
  const shelves = CATEGORIES
    .map((c) => ({ ...c, items: tiles.filter((t) => t.meta.category === c.key) }))
    .filter((g) => g.items.length);
  // The featured shelf repeats tiles that also sit on their own shelf, in the
  // order chosen rather than alphabetically.
  const picks = featured
    ? FEATURED.map((id) => tiles.find((t) => t.id === id)).filter(Boolean)
    : [];
  const groups = picks.length
    ? [{ key: "featured", label: "Featured", items: picks, ordered: true }, ...shelves]
    : shelves;

  if (!groups.length) {
    return <div className="meta conn-empty">No source matches that search.</div>;
  }

  return (
    <>
      {groups.map((g) => (
        <section key={g.key} className="conn-group">
          <div className="conn-group-title">{g.label}</div>
          <div className="conn-grid">
            {(g.ordered ? g.items : g.items.slice()
              .sort((a, b) => a.meta.label.localeCompare(b.meta.label)))
              .map((t) => <Tile key={t.id} tile={t} isAdmin={isAdmin} onPick={onPick} />)}
          </div>
        </section>
      ))}
    </>
  );
}

const VIA_LABEL = {
  ready: "Connect", driver: "Driver needed", admin: "Ask an admin", soon: "Coming soon",
};

function Tile({ tile, isAdmin, onPick }) {
  const live = tile.instances.filter((i) => i.configured);
  const dormant = !live.length && !tile.connectable
    && !((tile.id === "github" || SEND_TO.includes(tile.id)) && isAdmin);
  return (
    <button
      className={`conn-tile${dormant ? " conn-tile-off" : ""}`}
      onClick={() => onPick(tile.id)}
      title={tile.meta.blurb}
    >
      <SourceIcon id={tile.id} size={36} />
      <span className="conn-tile-name">{tile.meta.label}</span>
      <span className="conn-tile-status">
        {tile.id === "github"
          ? live.length ? `${live.length} registered` : isAdmin ? "Register repository" : "Ask an admin"
          : SEND_TO.includes(tile.id)
          ? live.length ? `${live.length} channel${live.length === 1 ? "" : "s"}`
            : isAdmin ? "Add a channel" : "Ask an admin"
          : live.length
          ? `${live.length} connected`
          : tile.connectable
            ? "Connect"
            : tile.via
              ? VIA_LABEL[tile.via.status]
              : isAdmin ? "Not configured" : "Ask an admin"}
      </span>
      {live.length > 0 && <span className="conn-dot" aria-hidden="true" />}
    </button>
  );
}

// ── Connect: credentials, then which schemas to bind ─────────────────────
//
// Tableau's shape: sign in to the server, then choose what to work with — and
// one sign-in is one source. Where the connector can read everything from one
// source (Databricks: schema "*", every table named catalog.schema.table, so a
// grant for one `orders` never admits another schema's), that is the default.
// Where a source reads one schema (Postgres, Snowflake…), the pick says which.
// The server refuses a second connection with a sign-in already here when it
// would read any of the same data (connections._refuse_duplicate).

/** The config to POST: the form as typed, pinned to the namespace picked. */
export function pinnedConfig({ cfg, ns, chosen = null }) {
  if (!chosen) return cfg;
  return {
    ...cfg,
    [ns.schema]: chosen.schema,
    ...(ns.database && chosen.database ? { [ns.database]: chosen.database } : {}),
    // "Everything the credential can see" spans catalogs: no catalog pin,
    // whatever was typed in that field.
    ...(ns.database && chosen.schema === ALL && !chosen.database ? { [ns.database]: "" } : {}),
  };
}

/** Whether two {database, schema} scopes read any of the same data — the
 * server's rule (connections._overlaps), to mark picks already connected. */
export function overlaps(a, b) {
  const same = (a.database || "") === (b.database || "") && a.schema === b.schema;
  const covers = (x, y) => x.schema === ALL && (!x.database || x.database === (y.database || ""));
  return same || covers(a, b) || covers(b, a);
}

/** "a", "a and b", "a, b and c". */
export function listNames(names) {
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0] || "";
}

/** What “Read everything” / “Combine into one” asks before it acts. */
export function everythingQuestion(name, others = []) {
  return `Make “${name}” read everything this sign-in can see?`
    + (others.length
      ? ` ${listNames(others.map((o) => `“${o}”`))} ${others.length > 1 ? "use" : "uses"} the same `
        + `sign-in and will be removed — chats and grants that name ${others.length > 1 ? "them" : "it"} `
        + "stop working."
      : "")
    + " Its tables will be named catalog.schema.table, so roles need them granted again in "
    + "Governance.";
}

/** What to say once a source reads everything (POST …/everything). */
export function combinedNote(out) {
  return `“${out.name}” now reads everything this sign-in can see`
    + (out.removed?.length ? ` — removed ${listNames(out.removed.map((n) => `“${n}”`))}` : "")
    + ". Its tables are being rediscovered.";
}

/** A schema value meaning every schema (Databricks: as one source, tables
 * named catalog.schema.table). */
export const ALL = "*";

/** "catalog.schema" (or just "schema") for messages. */
export function namespaceLabel(n) {
  if (n?.schema === ALL) return n.database ? `${n.database} (all schemas)` : "all catalogs";
  return n?.database ? `${n.database}.${n.schema}` : String(n?.schema || "");
}

/** What the picker shows for a namespace. */
export function schemaLabel(n) {
  if (n?.schema !== ALL) return n?.schema;
  return n.database ? "✳ all schemas — one source"
    : "✳ everything this credential can see — one source (recommended)";
}

export function namespaceKey(n) {
  return `${n?.database || ""}\u0000${n?.schema || ""}`;
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

/** The connect form for one tile. Exported for testing. */
export function ConnectForm({ tile, preset, defaultName, takenNames, onDone, onError,
                             onRemoved }) {
  const fields = tile.fields || [];
  const ns = tile.ns || {};
  // A marketplace tile posts as its native type (MySQL → "sql").
  const ctype = tile.ctype || tile.id;
  const initial = () => ({
    ...Object.fromEntries(fields.filter((f) => f.default).map((f) => [f.key, f.default])),
    ...(preset || {}),
  });
  const [name, setName] = useState(defaultName);
  const [cfg, setCfg] = useState(initial);
  const [test, setTest] = useState(null);       // null | {ok, tables?, sample?, error?}
  const [browse, setBrowse] = useState(null);   // null | {ok, namespaces[], error?}
  const [picked, setPicked] = useState([]);     // the one "db\u0000schema" key picked, or none
  const [busy, setBusy] = useState("");
  // A built-in source being re-pointed with this form (its name), or "".
  const [replacing, setReplacing] = useState("");
  const [rowBusy, setRowBusy] = useState("");

  // What the admin chose themselves, which a typed path must not overwrite.
  const [chose, setChose] = useState({ name: false, format: false });

  // A credential edit invalidates everything downstream of it.
  function setField(k, v) {
    setCfg((c) => {
      const next = { ...c, [k]: v };
      // A store's files say their format and (by their bucket) a name.
      if (k === "path" && !chose.format && guessFormat(v)) next.format = guessFormat(v);
      return next;
    });
    if (k === "format") setChose((c) => ({ ...c, format: true }));
    if (k === "path" && !chose.name && !replacing) {
      const named = nameFromPath(v, takenNames);
      if (named) setName(named);
    }
    setTest(null);
    setBrowse(null);
    setPicked([]);
  }

  // The namespace step only exists where the connector has namespaces to offer
  // (Neo4j does not) and the type says which fields a pick writes into.
  const canBrowse = !!ns.schema;
  const key = namespaceKey;
  // One source can read everything this sign-in sees (Databricks): the default.
  const spans = !!tile.spans;
  // An object store: keys plus the files to read as a table.
  const store = STORES.includes(ctype);
  // Sources this sign-in is already connected as that read (some of) `n`.
  const takenBy = (result, n) => (result?.connected_as || [])
    .filter((c) => overlaps(c, n)).map((c) => c.name);

  async function runTest() {
    setBusy(canBrowse ? "browse" : "test");
    setTest(null);
    setBrowse(null);
    try {
      if (canBrowse) {
        // Discovery itself is the credential probe.  /test lists tables and
        // therefore cannot succeed until a namespace has already been picked.
        const result = await api("/connections/browse", {
          method: "POST", body: JSON.stringify({ ctype, config: cfg }),
        });
        setBrowse(result);
        if (result.ok) {
          setTest({ ok: true, discovery: true });
          const every = spans && (result.namespaces || []).find((n) => n.schema === ALL && !n.database);
          if (every && !takenBy(result, every).length) setPicked([key(every)]);
        }
      } else {
        setTest(await api("/connections/test", {
          method: "POST", body: JSON.stringify({ ctype, config: cfg }),
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
  // Problems the server found in the form itself, by field — before it signed
  // in anywhere. Editing a field clears them (setField resets test/browse).
  const fieldProblems = browse?.fields || test?.fields || {};
  const formRefused = Object.keys(fieldProblems).length > 0;
  // How far it got: "check" (refused before any sign-in), "sign_in" (the
  // workspace refused the credential or path), "signed_in" (signed in, but the
  // catalog/schema is wrong or empty).
  const refusedAt = browse?.stage || test?.stage || "check";

  // A save refused for the form's fields marks them, like a sign-in would.
  function refusedSave(e) {
    if (e.detail?.fields) {
      setTest({ ok: false, error: e.detail.error, fields: e.detail.fields,
                stage: e.detail.stage });
      onError("");
      return true;
    }
    return false;
  }

  async function save() {
    if (replacing) return saveReplacement();
    setBusy("save");
    onError("");
    try {
      const made = await api("/connections", {
        method: "POST",
        body: JSON.stringify({ name: trimmed, ctype,
                               config: pinnedConfig({ cfg, ns, chosen: chosen[0] || null }) }),
      });
      onDone(made?.everything
        ? `Connected — “${trimmed}” reads everything this sign-in can see, and is in the chat `
          + "picker now (open a new chat or refresh to see it)."
        : `Connected — “${trimmed}” is now a source in the chat picker (open a new chat or `
          + "refresh to see it).");
    } catch (e) {
      if (!refusedSave(e)) onError(e.message);
    } finally {
      setBusy("");
    }
  }

  // Re-point a built-in source. ONE namespace: it stays one source with one
  // name — the reason to replace instead of adding a second connection.
  async function saveReplacement() {
    setBusy("save");
    onError("");
    const config = chosen.length === 1 ? pinnedConfig({ cfg, ns, chosen: chosen[0] }) : cfg;
    try {
      await api(`/connections/env/${encodeURIComponent(replacing)}`, {
        method: "PUT", body: JSON.stringify({ config }),
      });
      onDone(`“${replacing}” now reads from the connection entered here — same name, `
        + "so the roles granted it and the work saved against it follow it.");
    } catch (e) {
      if (!refusedSave(e)) onError(e.message);
    } finally {
      setBusy("");
    }
  }

  function startReplacing(sourceName) {
    setReplacing(sourceName);
    setCfg(initial());
    setChose({ name: false, format: false });
    setTest(null);
    setBrowse(null);
    setPicked([]);
    onError("");
  }

  // A built-in source's row: Change / Set up opens this form for it; the rest
  // act at once. Nothing here edits the deployment's environment.
  async function manage(inst, verb) {
    if (verb === "change" || verb === "setup") {
      startReplacing(inst.name);
      return;
    }
    const ask = {
      disconnect: `Disconnect “${inst.name}”? Chats, dashboards and pipelines that read it stop `
        + "until it is reconnected. The deployment's environment is left as it is.",
      revert: `Switch “${inst.name}” back to the deployment's environment settings? `
        + "What was set up for it here is forgotten.",
    }[verb];
    if (ask && !confirm(ask)) return;
    setRowBusy(`${verb}:${inst.name}`);
    onError("");
    try {
      const path = `/connections/env/${encodeURIComponent(inst.name)}`;
      await api(verb === "revert" ? path : `${path}/${verb}`,
                { method: verb === "revert" ? "DELETE" : "POST" });
      onRemoved();
    } catch (e) {
      onError(e.message);
    } finally {
      setRowBusy("");
    }
  }

  // A connection made here: disconnect parks it (credential and name kept,
  // reconnect is one click); remove forgets it. Both take it out of the picker
  // immediately.
  // "schemas" opens the add-schemas step under the rows instead of calling out;
  // "files", a store's files.
  const [adding, setAdding] = useState("");     // source name whose schemas are open
  const [filing, setFiling] = useState("");     // store source whose files are open
  async function act(inst, verb) {
    if (verb === "schemas") {
      setAdding((a) => (a === inst.name ? "" : inst.name));
      return;
    }
    if (verb === "files") {
      setFiling((a) => (a === inst.name ? "" : inst.name));
      return;
    }
    if (verb === "everything") {
      const others = tile.instances.filter((o) => o !== inst && o.kind === "user" && o.account
        && o.account === inst.account).map((o) => o.name);
      if (!confirm(everythingQuestion(inst.name, others))) return;
      setRowBusy(`everything:${inst.name}`);
      onError("");
      try {
        onDone(combinedNote(await api(`/connections/${inst.connId}/everything`, { method: "POST" })));
      } catch (e) {
        onError(e.message);
      } finally {
        setRowBusy("");
      }
      return;
    }
    const ask = {
      disconnect: `Disconnect “${inst.name}”? It leaves the chat picker and queries against it stop, `
        + "but the credential is kept so you can reconnect without re-entering it.",
      remove: `Remove “${inst.name}” permanently? Its stored credential is deleted — `
        + "connecting it again means entering it again.",
    }[verb];
    if (ask && !confirm(ask)) return;
    setRowBusy(`${verb}:${inst.name}`);
    onError("");
    try {
      await api(verb === "remove" ? `/connections/${inst.connId}` : `/connections/${inst.connId}/${verb}`,
                { method: verb === "remove" ? "DELETE" : "POST" });
      onRemoved();
    } catch (e) {
      onError(e.message);
    } finally {
      setRowBusy("");
    }
  }

  const trimmed = name.trim();
  const nameUsable = NAME_RE.test(trimmed) && !takenNames.has(trimmed);
  const { credentialsFilled, saveFilled } = connectionReadiness({ fields, ns, cfg, chosen });
  // What this sign-in is already connected as (after signing in), and the one
  // that already reads everything, when there is one: nothing left to connect.
  const connectedAs = browse?.ok ? browse.connected_as || [] : [];
  const whole = connectedAs.find((c) => c.everything);

  return (
    <div className="conn-detail">
      <Instances tile={tile} onAct={act} onManage={manage} busy={rowBusy} />

      {replacing && (
        <div className="meta share-notice conn-replacing">
          Setting up <b>{replacing}</b> here. It keeps its name, so the roles granted it and
          the chats, dashboards and pipelines that read it follow it to the connection
          entered below. “Back to environment” on its row undoes this.{" "}
          <button className="chip" disabled={!!busy}
            onClick={() => { setReplacing(""); setTest(null); setBrowse(null); setPicked([]); }}>
            Cancel
          </button>
        </div>
      )}
      {adding && (
        <AddSchemas key={adding} source={adding} takenNames={takenNames}
          onDone={onDone} onError={onError} onPartial={onRemoved}
          onCancel={() => setAdding("")} />
      )}
      {filing && (
        <StoreFiles key={filing} source={filing} onError={onError}
          onClose={() => setFiling("")} />
      )}

      <div className="conn-form">
        {fields.map((f) => (
          <label key={f.key} className={`conn-field${WIDE_FIELDS.has(f.key) ? " conn-field-wide" : ""}`}>
            <span className="conn-label">
              {f.label}{f.required && <b className="conn-req"> *</b>}
            </span>
            {f.options ? (
              <select
                name={`conn-${tile.id}-${f.key}`}
                className={fieldProblems[f.key] ? "conn-input-bad" : undefined}
                aria-invalid={fieldProblems[f.key] ? "true" : undefined}
                value={cfg[f.key] ?? f.default ?? ""}
                onChange={(e) => setField(f.key, e.target.value)}
                disabled={!!busy}
              >
                {f.options.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <input
                type={f.secret ? "password" : "text"}
                name={`conn-${tile.id}-${f.key}`}
                className={fieldProblems[f.key] ? "conn-input-bad" : undefined}
                aria-invalid={fieldProblems[f.key] ? "true" : undefined}
                placeholder={f.placeholder || ""}
                value={cfg[f.key] ?? ""}
                onChange={(e) => setField(f.key, e.target.value)}
                disabled={!!busy}
                {...NO_AUTOFILL}
                autoComplete={f.secret ? "new-password" : "off"}
              />
            )}
            {fieldProblems[f.key] && <span className="conn-field-error">{fieldProblems[f.key]}</span>}
          </label>
        ))}
        {!replacing && (
          <label className="conn-field">
            <span className="conn-label">Name it in Studio</span>
            <input value={name}
                   onChange={(e) => { setName(e.target.value); setChose((c) => ({ ...c, name: true })); }}
                   disabled={!!busy} {...NO_AUTOFILL} autoComplete="off" />
          </label>
        )}
      </div>

      {!replacing && trimmed && !nameUsable && (
        <div className="meta conn-warn">
          {takenNames.has(trimmed)
            ? `A source named “${trimmed}” already exists — pick another name.`
            : "Name must be 2–31 characters: lowercase letters, digits, - or _."}
        </div>
      )}

      {formRefused ? (
        <div className="error">
          {refusedAt === "signed_in"
            ? "Signed in, but not saved — fix the marked field."
            : refusedAt === "sign_in"
            ? `${tile.meta?.label || "The server"} refused the sign-in — fix the marked field.`
            : <>Fix the marked {Object.keys(fieldProblems).length > 1 ? "fields" : "field"} — nothing
              was sent to {tile.meta?.label || "the server"} yet.</>}
        </div>
      ) : test && (test.ok
        ? <div className="meta share-notice">
            {test.discovery
              ? `✓ Signed in — ${browse?.namespaces?.length || 0} schemas visible`
              : store
              ? `✓ Signed in and read the files — they become the table “${test.sample?.[0] || ""}”`
              : `✓ Connected — ${test.tables} table${test.tables === 1 ? "" : "s"}`
                + `${test.sample?.length ? `: ${test.sample.join(", ")}` : ""}`}
          </div>
        : <div className="error">{test.error}</div>)}

      {busy === "browse" && <div className="meta">Looking up schemas…</div>}
      {busy && tile.id === "databricks" && (
        <div className="meta">
          Signing in to the workspace — up to a minute if the SQL warehouse is starting.
        </div>
      )}
      {connectedAs.length > 0 && !replacing && (
        <div className="meta conn-warn conn-already" role="status">
          {whole ? (
            <>This sign-in is already connected as <b>{whole.name}</b>, which reads everything it
              can see — there is nothing more to connect. Use {whole.name}.</>
          ) : (
            <>This sign-in is already connected as{" "}
              {listNames(connectedAs.map((c) => `${c.name} (${c.namespace || "default"})`))}.{" "}
              {spans ? "To read everything with one source, use Combine into one under Connected."
                : "Pick another schema, or use that source."}</>
          )}
        </div>
      )}
      {browse && !formRefused && !whole && <Namespaces single
        browse={browse} picked={picked} setPicked={setPicked} keyOf={key}
        takenBy={(n) => takenBy(browse, n)} />}

      <div className="m365-actions">
        <button className="chip" onClick={runTest} disabled={!!busy || !credentialsFilled}>
          {busy === "test" ? "signing in…" : busy === "browse" ? "reading schemas…"
            : canBrowse ? "⚡ Sign in and list schemas" : "⚡ Test connection"}
        </button>
        <button className="primary" onClick={save}
          disabled={!!busy || !saveFilled || (!replacing && (!nameUsable || !!whole))}>
          {busy === "save" ? "connecting…"
            : replacing ? `✓ Use for “${replacing}”` : "✓ Connect"}
        </button>
      </div>
      <div className="meta">
        Credentials are encrypted at rest and never displayed again. One sign-in is connected
        once.{" "}
        {spans
          ? "One source reads every catalog and schema this credential can see — grant its " +
            "tables to other roles in Governance."
          : store
          ? "The files are read as one table; add more from the source's row (＋ Add files). " +
            "Only administrators reach it until it is granted to other roles in Governance."
          : ns.schema
          ? "The source reads the schema you pick — grant it to other roles in Governance."
          : "Only administrators reach a new source until it is granted to other roles in " +
            "Governance."}
      </div>
    </div>
  );
}

// ── Add schemas to a source that is already connected ────────────────────
//
// The form above can list schemas only while the admin holds the credential
// they just typed. A live source — the env-configured warehouse above all,
// whose secret nobody types here — gets the same step from its row, on the
// credential the server already has. Same rule as connecting: each picked
// schema becomes its own pinned source.

/** Sources to create for schemas added to `source`: wh + analytics -> wh-analytics. */
/** A name for a schema added to `base`: the base and the schema, as a slug
 * (`postgres` + `messy_retail` → `postgres-messy-retail`; every schema, `*`,
 * → `-all`), numbered past names already taken. The suffix gets its room
 * before the cut to 31, so a long name cannot lose it. */
function schemaSourceName(base, ns, taken) {
  const slug = ns.schema === ALL ? "all"
    : String(ns.schema).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const candidate = `${base}-${slug}`.slice(0, 31).replace(/-+$/, "");
  if (!taken.has(candidate)) return candidate;
  for (let i = 2; i < 10000; i++) {
    const suffix = `-${i}`;
    const next = `${candidate.slice(0, 31 - suffix.length).replace(/-+$/, "")}${suffix}`;
    if (!taken.has(next)) return next;
  }
  throw new Error("Could not generate a unique source name");
}

export function planSchemaAdds({ source, chosen = [], takenNames = new Set() }) {
  const taken = new Set(takenNames);
  return chosen.map((n) => {
    const name = schemaSourceName(source, n, taken);
    taken.add(name);
    return { name, schema: n.schema, database: n.database || "" };
  });
}

function AddSchemas({ source, takenNames, onDone, onError, onPartial, onCancel }) {
  const [browse, setBrowse] = useState(null);
  const [picked, setPicked] = useState([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const key = (n) => `${n.database || ""}\u0000${n.schema}`;
  const path = `/connections/sources/${encodeURIComponent(source)}`;

  useEffect(() => {
    let live = true;
    api(`${path}/namespaces`)
      .then((d) => live && setBrowse(d))
      .catch((e) => live && setBrowse({ ok: false, error: e.message, namespaces: [] }));
    return () => { live = false; };
  }, [path]);

  const chosen = (browse?.namespaces || []).filter((n) => picked.includes(key(n)));
  const plan = planSchemaAdds({ source, chosen, takenNames });

  async function save() {
    setBusy(true);
    onError("");
    // Sequential for the same reason as connecting: each POST re-probes, and a
    // partial failure names the schema that failed and keeps the ones that landed.
    const done = [];
    for (const p of plan) {
      setProgress(`adding ${p.name} (${done.length + 1} of ${plan.length})…`);
      try {
        await api(`${path}/schemas`, { method: "POST", body: JSON.stringify(p) });
        done.push(p.name);
      } catch (e) {
        setBusy(false);
        setProgress("");
        onError(done.length ? `Added ${done.join(", ")}, then “${p.name}” failed: ${e.message}`
                            : e.message);
        if (done.length) onPartial();
        return;
      }
    }
    onDone(`Added ${done.length === 1 ? `“${done[0]}”` : `${done.length} sources — ${done.join(", ")} —`}`
      + ` from ${source}, each scoped to its own schema. Grant them to other roles in Governance.`);
  }

  return (
    <div className="conn-add-schemas">
      {!browse ? <div className="meta">Looking up schemas {source} can see…</div> : (
        <Namespaces browse={browse} picked={picked} setPicked={setPicked} keyOf={key}
          current={browse.current}
          failHint="Nothing was changed."
          emptyHint="This account can't see any other schemas." />
      )}
      {plan.length > 0 && (
        <div className="meta">Will add: {plan.map((p) => p.name).join(", ")}</div>
      )}
      <div className="m365-actions">
        <button className="primary" onClick={save} disabled={busy || !plan.length}>
          {busy ? (progress || "adding…")
            : plan.length > 1 ? `✓ Add ${plan.length} schemas` : "✓ Add schema"}
        </button>
        <button className="chip" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
      <div className="meta">
        Uses {source}'s existing credential — nothing to re-enter. {source} itself stays
        pinned to its own schema.
      </div>
    </div>
  );
}

// ── A store's files: what it reads as tables, and more of them ───────────
//
// Object storage has no tables: a set of files (one file, a folder/*, a
// pattern) is named as one. The source's own setup brings the first; more are
// added here on the keys it already has. The server reads them before saving
// (POST /settings/objectstore with check), so a wrong path or format is
// refused here rather than on some later question.

const EXAMPLE_PATH = { s3: "s3://bucket/sales/*.parquet", azure_blob: "az://container/sales/*.parquet",
                       gcs: "gs://bucket/sales/*.parquet" };
const TABLE_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** A store source's tables, {name, uri, format, removable}, from
 * GET /settings/objectstore. Only the registry's own rows can be removed: the
 * first one belongs to the source's setup, the environment's to the deployment. */
export function storeTables(state, source) {
  const own = new Set((state?.datasets || []).filter((d) => d.source === source)
    .map((d) => d.name));
  return (state?.live?.[source] || []).map((d) => ({ ...d, removable: own.has(d.name) }));
}

/** What is wrong with the add-files form before it is sent, or "". */
export function fileFormProblem(form, tables = [], schemes = []) {
  const name = String(form.name || "").trim();
  const uri = String(form.uri || "").trim();
  if (uri && schemes.length && !schemes.some((s) => uri.startsWith(s))) {
    return `Files here start with ${schemes.join(" or ")}.`;
  }
  if (name && !TABLE_NAME_RE.test(name)) {
    return "A table name is letters, digits and _, starting with a letter.";
  }
  if (name && tables.some((t) => t.name === name)) {
    return `“${name}” is already a table here — pick another name.`;
  }
  return "";
}

export function StoreFiles({ source, onError, onClose }) {
  const [state, setState] = useState(null);     // GET /settings/objectstore
  const [form, setForm] = useState({ name: "", uri: "", format: "parquet" });
  const [chose, setChose] = useState(false);    // the format was picked by hand
  const [problems, setProblems] = useState({}); // the server's, by field ("_": the rest)
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

  const load = () => api("/settings/objectstore").then(setState)
    .catch((e) => setState({ error: e.message }));
  useEffect(() => { load(); }, [source]);

  const tables = storeTables(state, source);
  const schemes = state?.sources?.[source] || [];
  const problem = fileFormProblem(form, tables, schemes);

  function set(k, v) {
    setForm((f) => {
      const next = { ...f, [k]: v };
      if (k === "uri" && !chose && guessFormat(v)) next.format = guessFormat(v);
      return next;
    });
    if (k === "format") setChose(true);
    setProblems({});
    setNote("");
  }

  async function add() {
    const name = form.name.trim();
    setBusy("add");
    setProblems({});
    setNote("");
    onError("");
    try {
      await api("/settings/objectstore", {
        method: "POST",
        body: JSON.stringify({ source, name, uri: form.uri.trim(), format: form.format, check: true }),
      });
      // Queue a scan, so the new table's columns reach the agents' map of the source.
      api(`/metagraph/sources/${encodeURIComponent(source)}/discover`, { method: "POST" })
        .catch(() => {});
      setNote(`✓ Read the files — “${name}” is a table on ${source} now.`);
      setForm((f) => ({ name: "", uri: "", format: f.format }));
      setChose(false);
      load();
    } catch (e) {
      const fields = e.detail?.fields || {};
      setProblems(Object.keys(fields).length ? fields : { _: e.message });
    } finally {
      setBusy("");
    }
  }

  async function remove(name) {
    if (!confirm(`Stop reading “${name}” from ${source}? The files stay where they are; `
      + `chats and dashboards that query “${name}” stop working.`)) return;
    setBusy(`remove:${name}`);
    setNote("");
    onError("");
    try {
      await api(`/settings/objectstore/${encodeURIComponent(source)}/${encodeURIComponent(name)}`,
                { method: "DELETE" });
      load();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  const bad = (k) => (problems[k] ? { className: "conn-input-bad", "aria-invalid": "true" } : {});
  return (
    <div className="conn-add-schemas conn-files">
      <div className="conn-ns-head">
        <span className="conn-label">Files {source} reads, each as a table</span>
        <button className="chip" onClick={onClose} disabled={!!busy}>Close</button>
      </div>
      {!state ? <div className="meta">Looking up {source}'s files…</div>
        : state.error ? <div className="meta conn-warn">Couldn't list the files ({state.error}).</div>
        : (
          <div className="conn-files-list">
            {tables.map((t) => (
              <div key={t.name} className="conn-file">
                <b>{t.name}</b>
                <span className="query-tag">{t.format}</span>
                <span className="meta conn-file-uri" title={t.uri}>{t.uri}</span>
                {t.removable ? (
                  <button className="chip ctx-danger" onClick={() => remove(t.name)} disabled={!!busy}>
                    {busy === `remove:${t.name}` ? "working…" : "✕ Remove"}
                  </button>
                ) : <span className="meta">part of {source}'s setup</span>}
              </div>
            ))}
            {!tables.length && <div className="meta">No files read yet.</div>}
          </div>
        )}
      <div className="conn-form">
        <label className="conn-field conn-field-wide">
          <span className="conn-label">Files<b className="conn-req"> *</b></span>
          <input value={form.uri} onChange={(e) => set("uri", e.target.value)} disabled={!!busy}
                 placeholder={`${EXAMPLE_PATH[state?.kinds?.[source]] || "s3://bucket/sales/*.parquet"}`
                   + " — a file, a folder/* or a pattern"}
                 {...bad("uri")} {...NO_AUTOFILL} autoComplete="off" />
          {problems.uri && <span className="conn-field-error">{problems.uri}</span>}
        </label>
        <label className="conn-field">
          <span className="conn-label">Name them as a table<b className="conn-req"> *</b></span>
          <input value={form.name} onChange={(e) => set("name", e.target.value)} disabled={!!busy}
                 placeholder="the table agents query, e.g. stations"
                 {...bad("name")} {...NO_AUTOFILL} autoComplete="off" />
          {problems.name && <span className="conn-field-error">{problems.name}</span>}
        </label>
        <label className="conn-field">
          <span className="conn-label">Format</span>
          <select value={form.format} onChange={(e) => set("format", e.target.value)}
                  disabled={!!busy} {...bad("format")}>
            {(state?.formats || ["csv", "json", "parquet"]).map((f) => (
              <option key={f} value={f}>{f}</option>
            ))}
          </select>
          {problems.format && <span className="conn-field-error">{problems.format}</span>}
        </label>
      </div>
      {problem && <div className="meta conn-warn">{problem}</div>}
      {problems._ && <div className="error">{problems._}</div>}
      {note && <div className="meta share-notice">{note}</div>}
      <div className="m365-actions">
        <button className="primary" onClick={add}
          disabled={!!busy || !form.name.trim() || !form.uri.trim() || !!problem}>
          {busy === "add" ? "reading the files…" : "✓ Add files"}
        </button>
      </div>
      <div className="meta">
        Uses {source}'s keys — nothing to re-enter. The files are read before they are added,
        so a wrong path or format shows up here.
      </div>
    </div>
  );
}

/** The picked-schema checklist, grouped by database / catalog / project. */
export function Namespaces({ browse, picked, setPicked, keyOf, current = null, single = false,
                             takenBy = null,
                             failHint = "Type the schema in the field above and connect — this only affects the picker, not the connection.",
                             emptyHint = "No schemas visible to this account — type one above." }) {
  if (!browse.ok) {
    return <div className="meta conn-warn">Couldn't list schemas ({browse.error}). {failHint}</div>;
  }
  if (!browse.namespaces.length) {
    return <div className="meta">{emptyHint}</div>;
  }
  // Table counts come with the listing where the warehouse can say (Databricks
  // with Unity Catalog). Every counted schema empty almost always means the
  // credential lacks read access, not that the workspace has no data.
  const counted = browse.namespaces.filter((n) => typeof n.tables === "number");
  const allEmpty = counted.length > 0 && counted.length === browse.namespaces.length
    && counted.every((n) => n.tables === 0);
  // The schema the source is already pinned to is shown, but not pickable.
  // Postgres reports its database while the pin has none, so a blank side
  // matches any database.
  const isCurrent = (n) => !!current && n.schema === current.schema
    && (!current.database || !n.database || n.database === current.database);

  const groups = [];
  for (const n of browse.namespaces) {
    const db = n.database || "";
    const g = groups.find((x) => x.db === db) || (groups.push({ db, items: [] }), groups.at(-1));
    g.items.push(n);
  }

  function toggle(k) {
    if (single) setPicked((p) => (p.includes(k) ? [] : [k]));
    else setPicked((p) => p.includes(k) ? p.filter((x) => x !== k) : [...p, k]);
  }

  return (
    <div className="conn-ns">
      <div className="conn-ns-head">
        <span className="conn-label">
          {single ? "What this source reads — pick one"
            : "Schemas this account can see — pick the ones to add as sources"}
        </span>
        {!single && <span className="meta">{picked.length} selected</span>}
      </div>
      {groups.map((g) => (
        <div key={g.db} className="conn-ns-group">
          {g.db && <div className="conn-ns-db">{g.db}</div>}
          <div className="conn-ns-list">
            {g.items.map((n) => {
              const k = keyOf(n);
              const here = isCurrent(n);
              const empty = n.tables === 0;
              const owners = takenBy ? takenBy(n) : [];
              return (
                <label key={k} title={empty ? "No tables this credential can see" : undefined}
                       className={`conn-ns-item${picked.includes(k) ? " conn-ns-on" : ""}${empty ? " conn-ns-empty" : ""}`}>
                  <input type={single ? "radio" : "checkbox"} checked={here || picked.includes(k)}
                         disabled={here || owners.length > 0 || (empty && !picked.includes(k))}
                         onChange={() => toggle(k)} />
                  <span>{schemaLabel(n)}</span>
                  {here && <span className="meta"> (this source)</span>}
                  {owners.length > 0 && <span className="meta"> (connected as {owners.join(", ")})</span>}
                  {typeof n.tables === "number" && (
                    <span className="meta">{empty ? "empty" : `${n.tables} table${n.tables === 1 ? "" : "s"}`}</span>
                  )}
                </label>
              );
            })}
          </div>
        </div>
      ))}
      {allEmpty && (
        <div className="meta conn-warn">
          This credential can see these schemas but no tables in any of them. It most likely
          needs USE CATALOG, USE SCHEMA and SELECT on the tables (in Unity Catalog, for the
          user or service principal that owns the token).
        </div>
      )}
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
  const via = tile.via;
  let say;
  if (via?.status === "soon") {
    say = `${tile.meta.label} has no connector in Studio yet. Until it does, land its data in a ` +
      "warehouse Studio reads (Snowflake, BigQuery, Databricks, Postgres…) — for example with " +
      "Fivetran or Airbyte — and connect that.";
  } else if (via?.status === "driver") {
    say = isAdmin
      ? `${tile.meta.label} connects through ${metaFor(via.ctype).label} once its driver is ` +
        `installed on the deployment: add the \u201c${via.driver || "cassandra-driver"}\u201d ` +
        "package to the backend's requirements and redeploy. Then this tile opens the form, " +
        "pre-filled."
      : "An administrator has to install this integration's driver and connect it.";
  } else if (!isAdmin) {
    say = "An administrator connects sources. Sources your role may use appear in the chat " +
      "picker automatically — ask an admin to connect this one and grant it to your role.";
  } else {
    say = "This source is configured by an operator through environment variables on the " +
      "deployment (see .env.example), not from this screen. Once its keys are set it appears " +
      "in the chat picker like any other source.";
  }
  return (
    <div className="conn-detail">
      <Instances tile={tile} />
      <div className="meta">{say}</div>
    </div>
  );
}

/** The status a source row shows. Exported for tests. */
export function instanceBadge(i) {
  if (i.kind !== "env") {
    if (i.disconnected) return "disconnected";
    // On but unconfigured: the stored credential no longer decrypts (the
    // STUDIO_SECRET rotated). Reconnect can't help, so the badge must not
    // promise it — the way back is remove and connect again.
    return i.configured ? "connected" : "credential unreadable";
  }
  if (i.mode === "disconnected") return "disconnected here";
  if (i.mode === "replaced") return i.configured ? "set up here" : "credential unreadable";
  return i.configured ? "configured by environment" : "not configured";
}

/**
 * What an admin can do to a built-in source from its row, in button order.
 * Only rows that carry a `mode` (admins load /connections/env) get any.
 * Exported for tests.
 */
export function envActions(i) {
  if (i.kind !== "env" || !i.mode) return [];
  if (i.mode === "disconnected") return ["reconnect", "revert"];
  const out = [i.configured || i.mode === "replaced" ? "change" : "setup"];
  if (i.configured) out.push("disconnect");
  if (i.mode === "replaced") out.push("revert");
  return out;
}

/**
 * What an admin can do to a connection made on this screen: park it
 * (disconnect — credential and name kept) or bring it back, and remove it.
 * An unreadable one can only be removed. Exported for tests.
 */
export function userActions(i) {
  if (i.kind !== "user") return [];
  if (i.disconnected) return ["reconnect", "remove"];
  return i.configured ? ["disconnect", "remove"] : ["remove"];
}

const ACTION_LABEL = {
  change: "✎ Change", setup: "✎ Set up", disconnect: "⏸ Disconnect",
  reconnect: "↻ Reconnect", revert: "↺ Back to environment", remove: "✕ Remove",
};

export function Instances({ tile, onAct, onManage, busy = "" }) {
  const metadata = useMetadataStatus();
  if (!tile.instances.length) return null;
  const is = (verb, i) => busy === `${verb}:${i.name}`;
  // Any live source of a type with schemas can bind more of them — the
  // env-configured one included, since that reuses its credential and changes
  // nothing about the source itself.
  const canAdd = (i) => !!tile.ns?.schema && !tile.spans && i.configured && !i.disconnected;
  // Where one source can read everything (Databricks), a row that reads part
  // of it widens instead — and takes the other copies of its sign-in with it.
  const canWiden = (i) => !!tile.spans && i.kind === "user" && i.configured && !i.disconnected
    && !i.everything;
  // A store reads more files on the keys it already has.
  const canFile = (i) => STORES.includes(tile.id) && i.configured && !i.disconnected;
  return (
    <div className="conn-instances">
      {tile.instances.map((i) => (
        <div key={i.name} className={`dbconn-row${i.disconnected ? " dbconn-row-off" : ""}`}>
          <SourceIcon id={tile.id} size={20} />
          <b>{i.name}</b>
          {(i.hint || i.namespace || i.everything) && (
            <span className="meta">{[i.hint, scopeText(i)].filter(Boolean).join(" · ")}</span>
          )}
          <span className={`m365-badge${i.configured ? " m365-badge-on" : " m365-badge-off"}`}>
            {instanceBadge(i)}
          </span>
          {onAct && canAdd(i) && (
            <button className="chip" onClick={() => onAct(i, "schemas")} disabled={!!busy}>
              ＋ Add schemas
            </button>
          )}
          {onAct && canFile(i) && (
            <button className="chip" onClick={() => onAct(i, "files")} disabled={!!busy}
                    title="Read more files from this store, each as a table">
              ＋ Add files
            </button>
          )}
          {onAct && canWiden(i) && (
            <button className="chip" onClick={() => onAct(i, "everything")} disabled={!!busy}
                    title="One source for every catalog and schema this sign-in can see">
              {is("everything", i) ? "working…" : "✳ Read everything"}
            </button>
          )}
          {onManage && envActions(i).map((verb) => (
            <button key={verb} className={`chip${verb === "disconnect" ? " ctx-danger" : ""}`}
              onClick={() => onManage(i, verb)} disabled={!!busy}>
              {is(verb, i) ? "working…" : ACTION_LABEL[verb]}
            </button>
          ))}
          {onAct && userActions(i).length > 0 && (
            <span className="dbconn-actions">
              {userActions(i).map((verb) => (
                <button key={verb} className={`chip${verb === "reconnect" ? "" : " ctx-danger"}`}
                  onClick={() => onAct(i, verb)} disabled={!!busy}>
                  {is(verb, i) ? "working…" : ACTION_LABEL[verb]}
                </button>
              ))}
            </span>
          )}
          {i.configured && !i.disconnected && (
            <MetadataLine name={i.name} summary={metadata.byName[i.name]}
              canRediscover={!!(onAct || onManage)} reload={metadata.reload} />
          )}
        </div>
      ))}
    </div>
  );
}

// ── GitHub: registered, untrusted pipeline planning context ────────────

const GITHUB_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\/([A-Za-z0-9_.-]{1,100})\/?$/;
const GITHUB_BRANCH_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$/;

export function githubRegistrationReady(form, repos = []) {
  const name = String(form.name || "").trim();
  const url = String(form.url || "").trim();
  const branch = String(form.default_branch || "").trim() || "main";
  const repoName = GITHUB_URL_RE.exec(url)?.[1] || "";
  const canonicalRepoName = repoName.endsWith(".git") ? repoName.slice(0, -4) : repoName;
  return !!name && name.length <= 120 && !repos.some((repo) => repo.name === name)
    && !!canonicalRepoName && ![".", ".."].includes(canonicalRepoName)
    && GITHUB_BRANCH_RE.test(branch) && !branch.includes("..")
    && !branch.includes("//") && !branch.endsWith("/") && !branch.endsWith(".")
    && branch.split("/").every((part) => !part.startsWith(".") && !part.endsWith(".lock"));
}

export function GithubPanel({ repos, isAdmin, reload, onNote, onError }) {
  const [form, setForm] = useState({ name: "", url: "", description: "", default_branch: "main" });
  const [busy, setBusy] = useState("");

  async function register() {
    if (!isAdmin || !githubRegistrationReady(form, repos)) return;
    setBusy("register");
    onError("");
    onNote("");
    try {
      await api("/settings/repos", {
        method: "POST",
        body: JSON.stringify({
          name: form.name.trim(),
          url: form.url.trim(),
          description: form.description.trim(),
          default_branch: form.default_branch.trim() || "main",
        }),
      });
      setForm({ name: "", url: "", description: "", default_branch: "main" });
      onNote("Repository registered. Analysts can now select it when building a pipeline.");
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function remove(name) {
    if (!isAdmin || !confirm(`Remove “${name}” from pipeline planning sources?`)) return;
    setBusy(name);
    onError("");
    onNote("");
    try {
      await api(`/settings/repos/${encodeURIComponent(name)}`, { method: "DELETE" });
      onNote(`Removed “${name}” from future pipeline builds.`);
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="conn-detail">
      <div className="meta">
        GitHub repositories provide bounded file excerpts as untrusted planning context for
        prompt-built pipelines. They are not warehouse data connections, and Studio does not
        execute code from the repository. Select a registered repository in Chat or Pipelines
        when building a draft.
      </div>

      {isAdmin ? (
        <>
          <div className="meta">
            Register an HTTPS github.com repository below. For private repositories, set
            <code> GITHUB_TOKEN </code>on the Studio server and worker; never enter it in this browser.
            Registration does not check GitHub access. Studio reads a pinned snapshot when a
            repository is selected for a build. Registered repositories are selectable by
            Studio analysts and administrators, so add only repositories suitable for them.
          </div>
          <div className="conn-form">
            <label className="conn-field">
              <span className="conn-label">Name in Studio</span>
              <input placeholder="sales-pipelines" value={form.name} disabled={!!busy}
                onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))} />
            </label>
            <label className="conn-field">
              <span className="conn-label">GitHub repository URL</span>
              <input type="url" placeholder="https://github.com/org/repo" value={form.url}
                disabled={!!busy} onChange={(e) => setForm((prev) => ({ ...prev, url: e.target.value }))} />
            </label>
            <label className="conn-field">
              <span className="conn-label">Default branch</span>
              <input value={form.default_branch} disabled={!!busy}
                onChange={(e) => setForm((prev) => ({ ...prev, default_branch: e.target.value }))} />
            </label>
            <label className="conn-field conn-field-wide">
              <span className="conn-label">Description (optional)</span>
              <input placeholder="What pipeline examples live here?" value={form.description}
                disabled={!!busy} onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))} />
            </label>
          </div>
          {repos.some((repo) => repo.name === form.name.trim()) && (
            <div className="meta conn-warn">That name is already registered. Choose a new name.</div>
          )}
          <div className="m365-actions">
            <button className="primary" onClick={register}
              disabled={!!busy || !githubRegistrationReady(form, repos)}>
              {busy === "register" ? "registering…" : "＋ Register repository"}
            </button>
          </div>
        </>
      ) : (
        <div className="meta">An administrator registers repositories. Analysts can select
          registered repositories when building pipelines.</div>
      )}

      {repos.length ? (
        <div className="conn-instances">
          {repos.map((repo) => (
            <div key={repo.id || repo.name} className="dbconn-row">
              <SourceIcon id="github" size={20} />
              <b>{repo.name}</b>
              <span className="query-tag">{repo.default_branch || "main"}</span>
              <span className="meta">{repo.url}{repo.description ? ` — ${repo.description}` : ""}</span>
              <span className="m365-badge m365-badge-on">registered</span>
              {isAdmin && (
                <button className="chip ctx-danger" onClick={() => remove(repo.name)} disabled={!!busy}
                  aria-label={`Remove ${repo.name}`}>✕</button>
              )}
            </div>
          ))}
        </div>
      ) : <div className="meta">No repositories registered yet.</div>}
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
