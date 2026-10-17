// Motion for the D3 & artifacts window: charts ease in when they appear and
// move — not jump — when a filter changes. One pass after any builder in
// D3View has drawn, keyed on what it drew, so every chart type gets it
// without each builder growing its own transition code:
//  - bars grow from their axis; a bar that was there before (same series and
//    category) glides from where it was instead;
//  - lines draw themselves along their path, areas fade in under them;
//  - points, bubbles, nodes and circles pop in; slices swing out from the
//    centre; cells, tiles and anything else fade and rise in.
// Someone who asked their system for reduced motion gets none of it.

const DURATION = 650;
const MOVE = 450;
const SPREAD = 320;           // the whole stagger fits in this many ms

const VERTICAL_BARS = new Set(["bar", "stacked_bar", "histogram", "waterfall", "combo"]);
const CELLS = new Set(["heatmap", "calendar", "treemap"]);
const CIRCLES = new Set(["scatter", "bubble", "beeswarm", "network", "circle_pack", "radar"]);
const ARCS = new Set(["pie", "donut"]);

export function reducedMotion() {
  try {
    return Boolean(globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  } catch {
    return false;
  }
}

const delay = (n) => (_, i) => Math.min(i * (SPREAD / Math.max(n, 1)), SPREAD);
const num = (v) => Number(v) || 0;

function barRects(el) {
  return [...el.querySelectorAll("svg rect")].filter((r) => !r.classList.contains("legend-swatch"));
}

/** A bar's identity across redraws: its series group and its category. */
function barKey(rect) {
  const d = rect.__data__;
  const cat = d?.__cat ?? d?.data?.__cat ?? d?.label ?? d?.name;
  if (cat === undefined) return null;
  const group = rect.parentNode;
  const index = group?.parentNode ? [...group.parentNode.children].indexOf(group) : 0;
  return `${index}\u0000${cat}`;
}

/** Where each bar is now — read just before a redraw, to move from. */
export function captureBars(el) {
  const out = new Map();
  if (!el) return out;
  for (const r of barRects(el)) {
    const key = barKey(r);
    if (key !== null) {
      out.set(key, { x: num(r.getAttribute("x")), y: num(r.getAttribute("y")),
                     width: num(r.getAttribute("width")), height: num(r.getAttribute("height")) });
    }
  }
  return out;
}

/** Animate what `draw` just put in `el`. `previous` is captureBars() from
 *  before the redraw (empty on first draw). Returns how many marks moved. */
export function animate(d3, el, spec, previous = new Map()) {
  if (!el || reducedMotion()) return 0;
  const type = spec?.type;
  const ease = d3.easeCubicOut;

  if (VERTICAL_BARS.has(type) || type === "hbar") {
    const rects = barRects(el);
    const horiz = type === "hbar";
    rects.forEach((node, i) => {
      const end = { x: num(node.getAttribute("x")), y: num(node.getAttribute("y")),
                    width: num(node.getAttribute("width")), height: num(node.getAttribute("height")) };
      const before = previous.get(barKey(node));
      const start = before || (horiz ? { ...end, width: 0 }
                                     : { ...end, y: end.y + end.height, height: 0 });
      d3.select(node).attr("x", start.x).attr("y", start.y)
        .attr("width", start.width).attr("height", start.height)
        .transition().delay(before ? 0 : delay(rects.length)(null, i))
        .duration(before ? MOVE : DURATION).ease(ease)
        .attr("x", end.x).attr("y", end.y).attr("width", end.width).attr("height", end.height);
    });
    if (type !== "combo") return rects.length;
  }

  if (type === "line" || type === "area" || type === "stacked_area" || type === "combo") {
    let moved = 0;
    el.querySelectorAll("svg path").forEach((node) => {
      const fill = node.getAttribute("fill");
      const stroke = node.getAttribute("stroke");
      if (fill === "none" && stroke && stroke !== "none" && !node.closest(".tick, .domain")
          && typeof node.getTotalLength === "function") {
        const length = node.getTotalLength();
        d3.select(node).attr("stroke-dasharray", `${length} ${length}`)
          .attr("stroke-dashoffset", length)
          .transition().duration(DURATION + 250).ease(d3.easeCubicInOut)
          .attr("stroke-dashoffset", 0)
          .on("end", function () { d3.select(this).attr("stroke-dasharray", null); });
        moved++;
      } else if (fill && fill !== "none" && !node.classList.contains("domain")) {
        const to = node.getAttribute("opacity") || 1;
        d3.select(node).attr("opacity", 0).transition().duration(DURATION).ease(ease)
          .attr("opacity", to);
        moved++;
      }
    });
    return moved;
  }

  if (ARCS.has(type)) {
    const arcs = [...el.querySelectorAll("svg path")].filter((p) => p.__data__?.endAngle !== undefined);
    arcs.forEach((node, i) => {
      d3.select(node).attr("transform", "scale(0.6)").attr("opacity", 0)
        .transition().delay(delay(arcs.length)(null, i)).duration(DURATION).ease(d3.easeBackOut)
        .attr("transform", "scale(1)").attr("opacity", 1);
    });
    if (arcs.length) return arcs.length;
  }

  if (CIRCLES.has(type)) {
    const dots = [...el.querySelectorAll("svg circle")]
      .filter((c) => c.getAttribute("fill") !== "transparent" && num(c.getAttribute("r")) > 0);
    dots.forEach((node, i) => {
      const r = node.getAttribute("r");
      d3.select(node).attr("r", 0).transition().delay(delay(dots.length)(null, i))
        .duration(DURATION).ease(d3.easeBackOut).attr("r", r);
    });
    if (dots.length) return dots.length;
  }

  if (CELLS.has(type)) {
    const cells = barRects(el);
    cells.forEach((node, i) => {
      const to = node.getAttribute("opacity") || 1;
      d3.select(node).attr("opacity", 0).transition().delay(delay(cells.length)(null, i))
        .duration(DURATION).ease(ease).attr("opacity", to);
    });
    if (cells.length) return cells.length;
  }

  // Everything else (gauge, KPI, funnel, sankey, boxplot, table…): the whole
  // drawing fades and rises into place.
  const root = el.querySelector("svg") || el.firstElementChild;
  if (!root) return 0;
  d3.select(root).style("opacity", 0).style("transform", "translateY(6px)")
    .transition().duration(DURATION).ease(ease)
    .style("opacity", 1).style("transform", "translateY(0px)");
  return 1;
}
