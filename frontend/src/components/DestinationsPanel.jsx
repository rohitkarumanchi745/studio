// Microsoft Teams and Slack channels answers can be sent to (backend
// app/destinations.py). An administrator adds one by its incoming-webhook URL
// — kept encrypted, never shown again — and says which roles may send to it;
// everyone else sees the channels their role may use.
import { useState } from "react";
import { api } from "../api";

export const HOW_TO = {
  teams: "In Teams, open the channel's ••• menu → Workflows → “Post to a channel when a "
    + "webhook request is received” (or Connectors → Incoming Webhook), and copy the URL it gives you.",
  slack: "In Slack, create an app (api.slack.com/apps) → Incoming Webhooks → Add New Webhook to "
    + "Workspace → pick the channel, and copy the hooks.slack.com URL.",
};
const LABEL = { teams: "Microsoft Teams", slack: "Slack" };
const ROLE_LABEL = { admin: "admins", analyst: "analysts", viewer: "viewers" };

export function rolesText(roles = []) {
  const names = ["admin", "analyst", "viewer"].filter((r) => roles.includes(r)).map((r) => ROLE_LABEL[r]);
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] || "";
}

export default function DestinationsPanel({ kind, channels = [], isAdmin, reload, onNote, onError }) {
  const [form, setForm] = useState({ name: "", url: "", analyst: true, viewer: false, test: true });
  const [busy, setBusy] = useState("");
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  async function add() {
    if (!form.name.trim() || !form.url.trim() || busy) return;
    setBusy("add");
    onError("");
    try {
      const roles = ["admin", ...(form.analyst ? ["analyst"] : []), ...(form.viewer ? ["viewer"] : [])];
      const d = await api("/share/destinations", { method: "POST", body: JSON.stringify({
        name: form.name.trim(), kind, url: form.url.trim(), roles, test: form.test }) });
      setForm({ name: "", url: "", analyst: true, viewer: false, test: true });
      onNote(`“${d.name}” added${form.test ? " — a test message was posted there" : ""}.`);
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function remove(d) {
    if (!confirm(`Stop sending answers to “${d.name}”?`)) return;
    setBusy(d.id);
    try {
      await api(`/share/destinations/${d.id}`, { method: "DELETE" });
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="dest-panel" aria-label={`${LABEL[kind]} channels`}>
      <p className="meta">
        Send any answer to a {LABEL[kind]} channel from its <b>↗ Share</b> menu: its text, the
        first rows of its result, optionally its SQL, and a link back to the chat.
      </p>
      {channels.length > 0 ? (
        <div className="conn-instances">
          {channels.map((d) => (
            <div key={d.id} className="dbconn-row">
              <b>{d.name}</b>
              <span className="meta">sent to by {rolesText(d.roles)}</span>
              {isAdmin && (
                <button className="chip ctx-danger" onClick={() => remove(d)} disabled={!!busy}>
                  {busy === d.id ? "removing…" : "✕ Remove"}
                </button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="meta">No {LABEL[kind]} channel yet.{isAdmin ? "" : " An administrator adds them."}</p>
      )}
      {isAdmin && (
        <div className="dest-add">
          <h4>Add a {LABEL[kind]} channel</h4>
          <p className="meta">{HOW_TO[kind]}</p>
          <input value={form.name} onChange={set("name")} aria-label="Channel name"
                 placeholder={kind === "slack" ? "Name it, e.g. #sales-weekly" : "Name it, e.g. Sales team · General"} />
          <input type="password" value={form.url} onChange={set("url")} aria-label="Webhook URL"
                 autoComplete="off"
                 placeholder={kind === "slack" ? "https://hooks.slack.com/services/…"
                   : "https://…webhook.office.com/… or https://…logic.azure.com/…"} />
          <div className="inputs-row">
            <span className="meta">Who may send:</span>
            <label className="meta"><input type="checkbox" checked disabled /> admins</label>
            <label className="meta"><input type="checkbox" checked={form.analyst} onChange={set("analyst")} /> analysts</label>
            <label className="meta"><input type="checkbox" checked={form.viewer} onChange={set("viewer")} /> viewers</label>
          </div>
          <label className="meta"><input type="checkbox" checked={form.test} onChange={set("test")} /> post a test message now</label>
          <div>
            <button className="primary" onClick={add} disabled={!form.name.trim() || !form.url.trim() || !!busy}>
              {busy === "add" ? "adding…" : "＋ Add channel"}
            </button>
          </div>
          <p className="meta">The URL is stored encrypted and never shown again. Anyone in the channel
            sees what is sent there.</p>
        </div>
      )}
    </div>
  );
}
