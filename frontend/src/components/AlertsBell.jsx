// Autopilot alerts in the sidebar: how many fired runs of the user's own
// agents are unread (GET /autopilot/alerts), shown where it is always visible
// — Tools & settings may be collapsed. Checked every minute, and at once when
// the Autopilot screen marks alerts read (the "studio-alerts" event).
import { useEffect, useState } from "react";
import { api } from "../api";

export const ALERTS_EVENT = "studio-alerts";

/** The unread alert count, kept current. */
export function useAutopilotAlerts(every = 60000) {
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => api("/autopilot/alerts")
      .then((d) => { if (alive) setUnread(Number(d?.unread) || 0); })
      .catch(() => {});
    load();
    const timer = setInterval(load, every);
    window.addEventListener(ALERTS_EVENT, load);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener(ALERTS_EVENT, load);
    };
  }, [every]);
  return unread;
}

/** Tell the bell to look again (after alerts were read). */
export function alertsChanged() {
  try {
    window.dispatchEvent(new Event(ALERTS_EVENT));
  } catch {
    /* no window (tests) */
  }
}

export default function AlertsBell({ count, onOpen }) {
  if (!count) return null;
  return (
    <button className="ap-alerts-btn" onClick={onOpen}
            title="Autopilot agents fired — open Autopilot to see what they found">
      🔔 {count} new Autopilot {count === 1 ? "alert" : "alerts"}
    </button>
  );
}
