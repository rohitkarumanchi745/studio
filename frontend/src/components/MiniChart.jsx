// A real chart, small. The D3 renderer draws the chart at a design size and
// the drawing is scaled down to fit its box — so a thumbnail is the chart
// itself (its data, its type, its marks), not a picture standing in for one.
// It draws only once it is on screen, so a strip of them stays light.
import { useEffect, useRef, useState } from "react";
import D3View, { d3Type } from "./renderers/D3View";

/** The size a mini chart is drawn at before it is scaled to its box. */
export const STAGE = { w: 480, h: 240 };

export default function MiniChart({ columns, rows, chart, height = 84 }) {
  const box = useRef(null);
  const [width, setWidth] = useState(0);
  const [seen, setSeen] = useState(typeof IntersectionObserver === "undefined");

  useEffect(() => {
    const el = box.current;
    if (!el) return undefined;
    const measure = () => setWidth(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (seen || !box.current) return undefined;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        setSeen(true);
        io.disconnect();
      }
    }, { rootMargin: "120px" });
    io.observe(box.current);
    return () => io.disconnect();
  }, [seen]);

  const scale = width ? width / STAGE.w : 0;
  const spec = { ...(chart || {}), type: d3Type(chart?.type) };
  return (
    <div ref={box} className="mini-chart" style={{ height }} aria-hidden="true">
      {seen && scale > 0 && columns?.length > 0 && rows?.length > 0 && (
        <div className="mini-chart-stage"
             style={{ width: STAGE.w, height: height / scale, transform: `scale(${scale})` }}>
          <D3View columns={columns} rows={rows} spec={spec} fill />
        </div>
      )}
    </div>
  );
}
