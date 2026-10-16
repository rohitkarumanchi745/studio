// The markdown an answer is written in — bold, italics, `code`, lists,
// headings, tables, code blocks and links — drawn as React elements. Answers
// used to show it raw (**order_id**, backticks). Nothing here ever becomes
// HTML: every piece of text is a text node, and a link is only made for
// http(s) and mailto addresses, so an answer can format itself but cannot
// inject markup or a javascript: URL.

const FENCE = /^\s*```/;
const HEADING = /^(#{1,4})\s+(.*)$/;
const OL = /^\s*(\d{1,3})[.)]\s+(.*)$/;
const UL = /^\s*[-*•]\s+(.*)$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$/;

const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

/** Text → blocks: {type: p|h|ul|ol|code|table|hr, …}. Pure, for tests. */
export function parseMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let para = [];
  const flush = () => {
    if (para.length) blocks.push({ type: "p", lines: para });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const body = [];
      for (i++; i < lines.length && !FENCE.test(lines[i]); i++) body.push(lines[i]);
      blocks.push({ type: "code", text: body.join("\n") });
      continue;
    }
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      flush();
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && TABLE_ROW.test(lines[i]); i++) rows.push(cells(lines[i]));
      i--;
      blocks.push({ type: "table", head, rows });
      continue;
    }
    let m;
    if ((m = HEADING.exec(line))) {
      flush();
      blocks.push({ type: "h", level: m[1].length, text: m[2] });
    } else if (RULE.test(line)) {
      flush();
      blocks.push({ type: "hr" });
    } else if ((m = OL.exec(line)) || (m = UL.exec(line))) {
      flush();
      const ordered = OL.test(line);
      const last = blocks[blocks.length - 1];
      const item = ordered ? m[2] : m[1];
      if (last && last.type === (ordered ? "ol" : "ul") && last.open) last.items.push(item);
      else blocks.push({ type: ordered ? "ol" : "ul", items: [item], open: true,
                         ...(ordered ? { start: Number(m[1]) } : {}) });
    } else if (!line.trim()) {
      flush();
      // A blank line between two items keeps one list: "1. …\n\n2. …".
      const last = blocks[blocks.length - 1];
      const next = lines.slice(i + 1).find((l) => l.trim());
      if (!(last?.open && next && (OL.test(next) || UL.test(next)))) {
        if (last) last.open = false;
      }
    } else {
      const last = blocks[blocks.length - 1];
      if (last?.open && /^\s{2,}\S/.test(line)) {
        last.items[last.items.length - 1] += " " + line.trim();   // a wrapped list item
      } else {
        if (last) last.open = false;
        para.push(line);
      }
    }
  }
  flush();
  return blocks.map(({ open, ...b }) => b);
}

// `code`, **bold** / __bold__, *italic* / _italic_ (not inside a word, so
// order_id stays as it is), [text](https://…).
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*|__[^_\n]+__)|((?<![\w*])\*[^*\s][^*\n]*\*(?!\w)|(?<!\w)_[^_\s][^_\n]*_(?!\w))|(\[[^\]\n]+\]\([^)\s]+\))/g;
const SAFE_URL = /^(?:https?:\/\/|mailto:)/i;

/** One line of text → React nodes. */
export function inline(text, keyPrefix = "i") {
  const out = [];
  let last = 0;
  let n = 0;
  for (const m of String(text).matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const key = `${keyPrefix}-${n++}`;
    const [whole, code, bold, em, link] = m;
    if (code) out.push(<code key={key}>{code.slice(1, -1)}</code>);
    else if (bold) out.push(<strong key={key}>{inline(bold.slice(2, -2), key)}</strong>);
    else if (em) out.push(<em key={key}>{inline(em.slice(1, -1), key)}</em>);
    else if (link) {
      const [, label, url] = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(link);
      out.push(SAFE_URL.test(url)
        ? <a key={key} href={url} target="_blank" rel="noopener noreferrer">{inline(label, key)}</a>
        : whole);
    }
    last = m.index + whole.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function Markdown({ text, className = "" }) {
  const blocks = parseMarkdown(text);
  return (
    <div className={`md ${className}`.trim()}>
      {blocks.map((b, i) => {
        const k = `b${i}`;
        switch (b.type) {
          case "h": {
            const H = `h${Math.min(6, b.level + 2)}`;     // answers sit inside a card
            return <H key={k}>{inline(b.text, k)}</H>;
          }
          case "ul":
            return <ul key={k}>{b.items.map((it, j) => <li key={j}>{inline(it, `${k}-${j}`)}</li>)}</ul>;
          case "ol":
            return (
              <ol key={k} start={b.start}>
                {b.items.map((it, j) => <li key={j}>{inline(it, `${k}-${j}`)}</li>)}
              </ol>
            );
          case "code":
            return <pre key={k}><code>{b.text}</code></pre>;
          case "hr":
            return <hr key={k} />;
          case "table":
            return (
              <div key={k} className="md-table">
                <table>
                  <thead><tr>{b.head.map((c, j) => <th key={j}>{inline(c, `${k}-h${j}`)}</th>)}</tr></thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j}>{r.map((c, x) => <td key={x}>{inline(c, `${k}-${j}-${x}`)}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          default:
            return (
              <p key={k}>
                {b.lines.map((l, j) => (
                  <span key={j}>{j > 0 && <br />}{inline(l, `${k}-${j}`)}</span>
                ))}
              </p>
            );
        }
      })}
    </div>
  );
}
