// The ＋ menu in the chat composer: build an approval-gated Airflow DAG from
// the prompt, or open the platform panel. It closes once an option is picked,
// on a click anywhere else, and on Escape — left open, it covered the text
// box and the suggestions above it.
import { useEffect, useRef } from "react";

export default function ComposerPlus({ buildAirflow, busy, onBuildAirflow, onPlatform }) {
  const menu = useRef(null);
  const close = () => {
    if (menu.current) menu.current.open = false;
  };

  useEffect(() => {
    const outside = (e) => {
      if (menu.current?.open && !menu.current.contains(e.target)) close();
    };
    const escape = (e) => {
      if (e.key !== "Escape" || !menu.current?.open) return;
      close();
      menu.current.querySelector("summary")?.focus();
    };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, []);

  return (
    <details className="composer-plus" ref={menu}>
      <summary className="composer-plus-btn" aria-label="More options"
               title="Build an Airflow DAG, or run on a platform">＋</summary>
      <div className="composer-menu" role="menu">
        <label className="composer-pipeline-option menu-item">
          <input type="checkbox" checked={buildAirflow} disabled={busy}
            onChange={(e) => { onBuildAirflow(e.target.checked); close(); }} />
          Build an approval-gated Airflow DAG from this prompt
        </label>
        <button type="button" className="menu-item" disabled={busy}
                onClick={() => { onPlatform(); close(); }}>
          Run on Airflow / another platform
        </button>
      </div>
    </details>
  );
}
