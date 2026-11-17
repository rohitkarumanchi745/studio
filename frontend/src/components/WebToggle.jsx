// The chat's web search control: a switch (the agent may search the web, or
// not) and, while it is on, which search: Auto, or a provider the server has
// configured (Bing, Google, Brave, Claude's own). What is offered comes from
// /web/providers; the choice is remembered in this browser and sent with each
// question as `web`. Hidden when an administrator has turned the web off.

const KEY = "studio_web";

/** The remembered choice — "off", "auto" or a provider id. */
export function readWebChoice() {
  try {
    return localStorage.getItem(KEY) || "auto";
  } catch {
    return "auto";
  }
}

export function writeWebChoice(value) {
  try { localStorage.setItem(KEY, value); } catch { /* a convenience */ }
}

/** What to send: a provider no longer offered falls back to "auto". */
export function webForRequest(choice, offer) {
  if (!offer) return choice === "off" ? "off" : undefined;    // not loaded yet: the server's default
  if (!offer.enabled) return "off";
  if (choice === "off" || choice === "auto") return choice;
  return (offer.providers || []).some((p) => p.id === choice) ? choice : "auto";
}

export default function WebToggle({ value, offer, onChange, disabled = false }) {
  if (!offer?.enabled) return null;
  const providers = offer.providers || [];
  const on = value !== "off";
  const pick = webForRequest(value, offer);
  const auto = providers.find((p) => p.id === offer.default);
  const set = (next) => { writeWebChoice(next); onChange(next); };
  return (
    <span className={"web-toggle" + (on ? " on" : "")}>
      <label className="web-switch" title={on ? "The agent may search the web" : "No web search"}>
        <input type="checkbox" role="switch" aria-checked={on} checked={on} disabled={disabled}
               aria-label="Web search" onChange={(e) => set(e.target.checked ? "auto" : "off")} />
        <span className="web-switch-track" aria-hidden="true"><span className="web-switch-thumb" /></span>
        🌐 Web
      </label>
      {on && providers.length > 0 && (
        <select className="web-pick" value={pick} disabled={disabled} aria-label="Web search provider"
                title="Which web search the agent uses" onChange={(e) => set(e.target.value)}>
          <option value="auto">Auto{auto ? ` (${auto.label})` : ""}</option>
          {providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        </select>
      )}
    </span>
  );
}
