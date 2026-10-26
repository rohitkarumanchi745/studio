// The frame around a visual in the D3 & artifacts window: the reader decides
// how it fits. Size presets (S / M / L / XL), a handle to drag the height
// freely (double-click resets; arrow keys work), a wide mode that breaks out
// of the chat column, and full screen (Esc closes). The visual inside fills
// whatever the frame is — D3 charts redraw to the new size, custom visuals
// re-run their draw — so nothing is cropped or stretched. A table (`fit`) is
// as tall as its rows, up to the chosen size, and scrolls past it.
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export const SIZES = [["S", 260], ["M", 380], ["L", 520], ["XL", 720]];
export const MIN_HEIGHT = 180;
export const MAX_HEIGHT = 1400;
const DEFAULT_HEIGHT = 440;
const KEY = "studio_viz_height";

export const clampHeight = (h) => Math.round(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Number(h) || DEFAULT_HEIGHT)));

/** The height a new visual starts at: the reader's last choice, if any. */
export function rememberedHeight() {
  try {
    const v = Number(globalThis.localStorage?.getItem(KEY));
    return v ? clampHeight(v) : DEFAULT_HEIGHT;
  } catch {
    return DEFAULT_HEIGHT;
  }
}

function remember(h) {
  try {
    globalThis.localStorage?.setItem(KEY, String(clampHeight(h)));
  } catch {
    /* private mode, blocked storage: the size still applies to this visual */
  }
}

// Full screen renders into <body>: an ancestor with a transform, filter or
// contain (an animated message, wide mode) would otherwise become the box a
// position:fixed overlay fills, and "full screen" would be a few hundred px.
function overlay(node) {
  return typeof document !== "undefined" && document.body ? createPortal(node, document.body) : node;
}

export default function VizFrame({ title, children, fit = false }) {
  const [height, setHeight] = useState(rememberedHeight);
  const [wide, setWide] = useState(false);
  const [full, setFull] = useState(false);
  const live = useRef(height);
  live.current = height;

  const choose = (h) => {
    const next = clampHeight(h);
    setHeight(next);
    remember(next);
  };

  useEffect(() => {
    if (!full) return undefined;
    const onKey = (e) => { if (e.key === "Escape") setFull(false); };
    globalThis.addEventListener?.("keydown", onKey);
    return () => globalThis.removeEventListener?.("keydown", onKey);
  }, [full]);

  function startDrag(e) {
    e.preventDefault();
    const startY = e.clientY;
    const startH = live.current;
    const move = (ev) => setHeight(clampHeight(startH + ev.clientY - startY));
    const up = () => {
      globalThis.removeEventListener("pointermove", move);
      globalThis.removeEventListener("pointerup", up);
      remember(live.current);
    };
    globalThis.addEventListener("pointermove", move);
    globalThis.addEventListener("pointerup", up);
  }

  function onHandleKey(e) {
    const step = e.shiftKey ? 100 : 20;
    if (e.key === "ArrowDown") choose(height + step);
    else if (e.key === "ArrowUp") choose(height - step);
    else if (e.key === "Home") choose(MIN_HEIGHT);
    else if (e.key === "End") choose(MAX_HEIGHT);
    else return;
    e.preventDefault();
  }

  const controls = (
    <div className="viz-controls" role="toolbar" aria-label="Visual size">
      <span className="viz-seg" role="group" aria-label="Height">
        {SIZES.map(([label, h]) => (
          <button key={label} type="button" className={"viz-size" + (height === h ? " is-on" : "")}
                  aria-pressed={height === h} title={`${h}px tall`} onClick={() => choose(h)}>
            {label}
          </button>
        ))}
      </span>
      <button type="button" className={"viz-size" + (wide ? " is-on" : "")} aria-pressed={wide}
              title={wide ? "Back to the chat column's width" : "Use the full width of the window"}
              onClick={() => setWide(!wide)}>
        {wide ? "⇥ column" : "⇤⇥ wide"}
      </button>
      <button type="button" className="viz-size" title="Full screen (Esc to close)"
              onClick={() => setFull(true)}>⤢ full screen</button>
    </div>
  );

  return (
    <div className={"viz-frame" + (wide ? " is-wide" : "")}>
      {controls}
      {full ? (
        <div className="viz-placeholder" style={{ height }}>Shown full screen — press Esc to return.</div>
      ) : (
        <div className={"viz-body" + (fit ? " viz-fit" : "")}
             style={fit ? { maxHeight: height } : { height }}>{children}</div>
      )}
      {!full && (
        <div className="viz-handle" role="separator" aria-orientation="horizontal"
             aria-label="Drag to resize the visual" aria-valuenow={height}
             aria-valuemin={MIN_HEIGHT} aria-valuemax={MAX_HEIGHT} tabIndex={0}
             title="Drag to resize · double-click to reset · arrow keys work too"
             onPointerDown={startDrag} onDoubleClick={() => choose(DEFAULT_HEIGHT)}
             onKeyDown={onHandleKey} />
      )}
      {full && overlay(
        <div className="viz-full" role="dialog" aria-modal="true" aria-label={title || "Visual, full screen"}>
          <div className="viz-full-head">
            <span className="chart-title">{title || "Visual"}</span>
            <button type="button" className="chip" onClick={() => setFull(false)}>✕ exit full screen</button>
          </div>
          <div className="viz-full-body">{children}</div>
        </div>
      )}
    </div>
  );
}
