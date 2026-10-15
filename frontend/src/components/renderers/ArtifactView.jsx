// Custom visuals (artifacts) — D3 code the agent wrote (backend app/visuals.py),
// run in a sandboxed iframe. The frame is the security boundary:
//  - sandbox="allow-scripts" and nothing else: an opaque origin, so the code
//    cannot reach Studio's page, cookies, localStorage (the login token) or the
//    API; no popups, forms, modals or top-level navigation.
//  - A Content-Security-Policy inside the frame: no network (connect-src
//    'none'), no frames, fonts or remote images; scripts only inline and
//    Studio's own copy of d3. Nothing can be sent anywhere.
//  - WebRTC, which CSP does not govern, is removed before the code runs, and
//    child frames are made useless for fetching a fresh copy of it.
//  - The data is the panel's own rows (already governed by the gateway),
//    embedded as JSON with "<" escaped so no value can close the script tag.
// The parent listens to its own frame only, matched by a per-render nonce, and
// treats what the frame says (an error message) as text.
//
// A srcdoc frame INHERITS the embedding page's CSP. Studio's page sends none
// today; if one is ever added, it must still let this frame run its inline
// scripts — or artifacts must move to a separate origin.
import { useEffect, useMemo, useRef, useState } from "react";
import d3Url from "../../../node_modules/d3/dist/d3.min.js?url";

/** Neutralise anything in `js` that could end the <script> element early. */
export function scriptSafe(js) {
  // Case is kept: inside a string literal "<\/SCRIPT" still means "</SCRIPT".
  return String(js).replace(/<\/(script)/gi, "<\\/$1").replace(/<!--/g, "<\\!--");
}

/** The panel's rows as JSON that is safe inside a <script> element. */
export function dataJson(columns, rows) {
  return JSON.stringify({ columns, rows }).replace(/</g, "\\u003c");
}

function csp(d3Src) {
  return [
    "default-src 'none'",
    `script-src 'unsafe-inline'${d3Src ? " " + d3Src : ""}`,
    "style-src 'unsafe-inline'",
    "img-src data: blob:",
    "font-src 'none'", "connect-src 'none'", "media-src 'none'", "object-src 'none'",
    "frame-src 'none'", "child-src 'none'", "worker-src 'none'", "manifest-src 'none'",
    "form-action 'none'", "base-uri 'none'", "webrtc 'block'",
  ].join("; ");
}

// Runs first, in its own <script>, so even a syntax error in the visual's code
// is caught and reported. Hardens the frame before any generated code runs.
const GUARD = `
(function () {
  var nonce = document.currentScript.getAttribute("data-nonce");
  var post = window.parent.postMessage.bind(window.parent);
  window.__studioSay = function (kind, detail) {
    try { post({ studioVisual: nonce, kind: kind, detail: String(detail || "").slice(0, 300) }, "*"); }
    catch (e) {}
  };
  ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel", "RTCIceCandidate",
   "RTCSessionDescription", "RTCRtpSender", "RTCRtpReceiver"].forEach(function (k) {
    try { Object.defineProperty(window, k, { value: undefined, writable: false, configurable: false }); }
    catch (e) {}
  });
  try {
    ["contentWindow", "contentDocument"].forEach(function (k) {
      Object.defineProperty(HTMLIFrameElement.prototype, k,
        { get: function () { return null; }, configurable: false });
    });
  } catch (e) {}
  window.addEventListener("error", function (e) { window.__studioSay("error", e.message || "error"); });
})();`;

function runner(code) {
  return `
(function () {
  var say = window.__studioSay;
  var payload = JSON.parse(document.getElementById("studio-data").textContent);
  var data = payload.rows.map(function (r) {
    var o = {};
    payload.columns.forEach(function (c, i) { o[c] = r[i]; });
    return o;
  });
  var el = document.getElementById("root");
  function draw(d3, data, el, size) {
${code}
  }
  function render() {
    el.replaceChildren();
    try {
      draw(window.d3, data, el, { width: el.clientWidth || innerWidth, height: el.clientHeight || innerHeight });
      say("ok", "");
    } catch (e) {
      say("error", (e && e.message) || e);
    }
  }
  var t;
  window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(render, 150); });
  render();
})();`;
}

/**
 * The frame's whole document. `d3Src` is the absolute URL of Studio's d3 file
 * (in the app); `inlineD3` is d3's source instead (a downloaded file, which
 * must stand alone). Exported for tests.
 */
export function buildArtifactDoc({ code, columns, rows, nonce, title = "", d3Src = "", inlineD3 = "" }) {
  const d3Tag = inlineD3 ? `<script>${scriptSafe(inlineD3)}</script>`
    : `<script src="${d3Src}"></script>`;
  return `<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp(inlineD3 ? "" : d3Src)}">
<meta http-equiv="x-dns-prefetch-control" content="off">
<meta name="referrer" content="no-referrer">
<title>${String(title).replace(/[<>&"]/g, "")}</title>
<style>
  html, body { margin: 0; height: 100%; background: #fff; color: #4a4844;
    font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; overflow: hidden; }
  #root { width: 100%; height: 100%; position: relative; }
</style>
<script data-nonce="${String(nonce).replace(/[^A-Za-z0-9]/g, "")}">${GUARD}</script>
${d3Tag}
</head><body><div id="root"></div>
<script type="application/json" id="studio-data">${dataJson(columns, rows)}</script>
<script>${scriptSafe(runner(code))}</script>
</body></html>`;
}

function absolute(url) {
  try {
    return new URL(url, window.location.href).href;
  } catch {
    return url;
  }
}

export default function ArtifactView({ visual, columns, rows, tall }) {
  const ref = useRef(null);
  const [failure, setFailure] = useState("");
  const nonce = useMemo(() => Math.random().toString(36).slice(2) + Date.now().toString(36),
                        [visual?.code, columns, rows]);
  const srcDoc = useMemo(() => buildArtifactDoc({
    code: visual?.code || "", columns, rows, nonce, title: visual?.title,
    d3Src: absolute(d3Url),
  }), [visual?.code, visual?.title, columns, rows, nonce]);

  useEffect(() => {
    setFailure("");
    function onMessage(event) {
      const frame = ref.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const m = event.data;
      if (!m || typeof m !== "object" || m.studioVisual !== nonce) return;
      if (m.kind === "error") setFailure(String(m.detail || "it stopped with an error").slice(0, 300));
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [nonce]);

  return (
    <>
      {failure && (
        <div className="chart-note">
          The custom visual failed: {failure}. Switch to Chart, or ask the agent to fix it.
        </div>
      )}
      <iframe
        ref={ref}
        title={visual?.title || "Custom visual"}
        className={"artifact-frame echart" + (tall ? " echart-tall" : "")}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        srcDoc={srcDoc}
      />
    </>
  );
}

/** Save the visual as one self-contained HTML file: d3 inlined, the data
 *  embedded, the same no-network policy — it opens anywhere, offline. */
export async function downloadArtifact({ visual, columns, rows }) {
  const res = await fetch(d3Url);
  if (!res.ok) throw new Error(`could not load d3 (${res.status})`);
  const html = buildArtifactDoc({
    code: visual.code, columns, rows, title: visual.title,
    nonce: "standalone", inlineD3: await res.text(),
  });
  const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${String(visual.title || "visual").replace(/[^\w-]+/g, "-").replace(/^-|-$/g, "")
    .slice(0, 60) || "visual"}.html`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
