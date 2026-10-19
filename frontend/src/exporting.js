// Downloads: an answer's data as CSV or Excel, a report as one HTML page, and
// a chart as a PNG. The server builds the files (backend app/exports.py) from
// what this screen already shows — so spreadsheet safety, escaping and the
// audit row live in one place — and the browser saves what comes back.
import { getToken } from "./api";

/** The file name the server chose (Content-Disposition), or `fallback`. */
export function attachmentName(header, fallback) {
  return /filename="([^"]+)"/.exec(header || "")?.[1] || fallback;
}

/** Hand `blob` to the browser as a download named `name`. */
export function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function postForFile(path, payload, fallback) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, { method: "POST", headers, body: JSON.stringify(payload) });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail;
    throw new Error((typeof detail === "string" && detail) || `download failed (${res.status})`);
  }
  const name = attachmentName(res.headers.get("content-disposition"), fallback);
  saveBlob(await res.blob(), name);
  return name;
}

/** One result as "csv" or "xlsx": {title, columns, rows, sql?, text?, source?}. */
export function downloadTable(format, table) {
  return postForFile(`/export/${format}`, table, `studio-export.${format}`);
}

/** A report: {title, subtitle?, sections: [{title, text, image, columns, rows, sql, source}]}. */
export function downloadReport(report) {
  return postForFile("/export/report", report, "studio-report.html");
}

/** UTF-8 text as base64 (for an SVG data URL). */
export function base64Text(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

// What an SVG needs from the page's stylesheet to look the same on its own.
const SVG_STYLE = ["fill", "fill-opacity", "stroke", "stroke-width", "stroke-opacity",
  "stroke-dasharray", "opacity", "font-family", "font-size", "font-weight", "text-anchor",
  "dominant-baseline", "visibility", "display"];

function standaloneSvg(svg, width, height) {
  const copy = svg.cloneNode(true);
  const from = [svg, ...svg.querySelectorAll("*")];
  const to = [copy, ...copy.querySelectorAll("*")];
  from.forEach((el, i) => {
    const style = getComputedStyle(el);
    const target = to[i];
    if (!target?.style) return;
    for (const prop of SVG_STYLE) target.style.setProperty(prop, style.getPropertyValue(prop));
  });
  copy.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  copy.setAttribute("width", String(width));
  copy.setAttribute("height", String(height));
  return new XMLSerializer().serializeToString(copy);
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("the chart could not be drawn as an image"));
    img.src = src;
  });
}

/**
 * The chart drawn in `container` as a PNG data URL, whatever drew it: every
 * <canvas> (ECharts, Vega) and top-level <svg> (D3, Plotly, Vega) inside it,
 * stacked where they sit on screen, on white, at `scale` × the screen size.
 */
export async function chartPng(container, { scale = 2, background = "#ffffff" } = {}) {
  if (!container) throw new Error("there is no chart to save");
  const box = container.getBoundingClientRect();
  const layers = [...container.querySelectorAll("canvas, svg")]
    .filter((el) => !(el.tagName.toLowerCase() === "svg" && el.parentElement?.closest("svg")));
  if (!layers.length || !box.width || !box.height) throw new Error("there is no chart to save");
  const out = document.createElement("canvas");
  out.width = Math.round(box.width * scale);
  out.height = Math.round(box.height * scale);
  const ctx = out.getContext("2d");
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.scale(scale, scale);
  for (const el of layers) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const x = r.left - box.left;
    const y = r.top - box.top;
    if (el.tagName.toLowerCase() === "canvas") {
      ctx.drawImage(el, x, y, r.width, r.height);
    } else {
      const svg = standaloneSvg(el, r.width, r.height);
      ctx.drawImage(await loadImage(`data:image/svg+xml;base64,${base64Text(svg)}`),
                    x, y, r.width, r.height);
    }
  }
  return out.toDataURL("image/png");
}

/** Save the chart in `container` as `<title>.png`. */
export async function downloadPng(container, title) {
  const url = await chartPng(container);
  const blob = await (await fetch(url)).blob();
  const name = `${String(title || "chart").replace(/[^\w-]+/g, "-").replace(/^-|-$/g, "")
    .slice(0, 60) || "chart"}.png`;
  saveBlob(blob, name);
  return name;
}
