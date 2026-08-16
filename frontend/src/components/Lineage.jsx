// Source → table → step provenance diagram, rendered under a pipeline. When a
// request spans multiple sources, this is where you SEE which source feeds
// which table into which step. A failed run marks the failing step in red.
// Self-contained SVG — three columns (sources | tables | steps) with the
// edges from the backend `lineage` graph drawn as connectors.
const NODE_H = 30;
const V_GAP = 12;
const COL_W = 156;
const COL_GAP = 64;
const PAD = 14;

const GOLD = "#c9a86a";
const AVOCADO = "#6b8e3d";
const FAIL = "#c05a44";

function layout(lineage) {
  const cols = [
    lineage.sources || [],
    lineage.tables || [],
    lineage.steps || [],
  ];
  const pos = {};
  cols.forEach((nodes, k) => {
    const x = PAD + k * (COL_W + COL_GAP);
    nodes.forEach((n, j) => {
      const y = PAD + j * (NODE_H + V_GAP);
      pos[n.id] = { x, y, w: COL_W, h: NODE_H, cx: x + COL_W / 2, cy: y + NODE_H / 2, node: n };
    });
  });
  const maxRows = Math.max(1, ...cols.map((c) => c.length));
  const height = PAD * 2 + maxRows * (NODE_H + V_GAP) - V_GAP;
  const width = PAD * 2 + 3 * COL_W + 2 * COL_GAP;
  return { pos, width, height };
}

function edgePath(a, b) {
  const x1 = a.x + a.w, y1 = a.cy, x2 = b.x, y2 = b.cy;
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

function Node({ p, kind }) {
  const n = p.node;
  const failed = kind === "step" && n.failed;
  const stroke = failed ? FAIL : kind === "source" ? AVOCADO : GOLD;
  const fill = failed ? "rgba(192,90,68,0.16)" : kind === "source" ? "rgba(107,142,61,0.14)" : "#232322";
  const label = kind === "step" && typeof n.index === "number" ? `${n.index + 1}. ${n.label}` : n.label;
  const clip = label.length > 22 ? label.slice(0, 21) + "…" : label;
  return (
    <g>
      <rect x={p.x} y={p.y} width={p.w} height={p.h} rx={7}
        fill={fill} stroke={stroke} strokeWidth={failed ? 1.8 : 1} />
      <text x={p.x + 10} y={p.cy + 4} fontSize={12} fill="#e9e8df" fontWeight={kind === "source" ? 600 : 400}>
        {clip}
      </text>
      {kind === "step" && failed && (
        <text x={p.x + p.w - 8} y={p.cy + 4} fontSize={11} fill={FAIL} textAnchor="end">✕</text>
      )}
    </g>
  );
}

export default function Lineage({ lineage }) {
  if (!lineage || !(lineage.steps || []).length) return null;
  const { pos, width, height } = layout(lineage);
  const kindOf = (id) => (id.startsWith("s:") ? "source" : id.startsWith("t:") ? "table" : "step");

  const headers = [
    { x: PAD, label: "Source" },
    { x: PAD + COL_W + COL_GAP, label: "Table" },
    { x: PAD + 2 * (COL_W + COL_GAP), label: "Step" },
  ];

  return (
    <div className="lineage">
      <div className="lineage-head meta">
        Data lineage — {lineage.multi_source ? "multiple sources" : "single source"} · which
        source feeds which table into which step
      </div>
      <div className="lineage-scroll">
        <svg width={width} height={height + 22} viewBox={`0 0 ${width} ${height + 22}`}
          style={{ maxWidth: "100%" }}>
          {headers.map((h) => (
            <text key={h.label} x={h.x} y={12} fontSize={10} fill="#898781"
              letterSpacing={1}>{h.label.toUpperCase()}</text>
          ))}
          <g transform="translate(0,18)">
            {(lineage.edges || []).map((e, i) => {
              const a = pos[e.from], b = pos[e.to];
              if (!a || !b) return null;
              const failEdge = kindOf(e.to) === "step" && b.node.failed;
              return (
                <path key={i} d={edgePath(a, b)} fill="none"
                  stroke={failEdge ? FAIL : GOLD} strokeOpacity={failEdge ? 0.85 : 0.4}
                  strokeWidth={failEdge ? 1.6 : 1} />
              );
            })}
            {Object.values(pos).map((p) => (
              <Node key={p.node.id} p={p} kind={kindOf(p.node.id)} />
            ))}
          </g>
        </svg>
      </div>
    </div>
  );
}
