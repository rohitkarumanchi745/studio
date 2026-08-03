"""Email delivery: real SMTP when configured, dev outbox otherwise.

Set SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM to send real
mail. Without them, messages are written as .eml files to backend/outbox/ so
every email flow is testable locally.
"""
import os
import smtplib
import time
from email.message import EmailMessage

OUTBOX = os.path.join(os.path.dirname(__file__), "..", "outbox")


def send(to, subject, html, text=None):
    """Send an email. Returns {"mode": "smtp"|"outbox", "to": ..., "path": ...?}."""
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM", "studio@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or "This message contains an HTML report.")
    msg.add_alternative(html, subtype="html")

    host = os.getenv("SMTP_HOST")
    if host:
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASSWORD")
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
        return {"mode": "smtp", "to": to}

    os.makedirs(OUTBOX, exist_ok=True)
    safe_subject = "".join(c if c.isalnum() else "_" for c in subject)[:40]
    path = os.path.join(OUTBOX, f"{int(time.time())}_{safe_subject}.eml")
    with open(path, "w") as f:
        f.write(msg.as_string())
    return {"mode": "outbox", "to": to, "path": path}


def report_html(title, body_text, sql, columns, rows, footer=""):
    """Simple HTML report: insight text, SQL, and a data table (first 100 rows)."""
    head_cells = "".join(f"<th style='text-align:left;padding:6px 10px'>{c}</th>" for c in columns)
    body_rows = "".join(
        "<tr>" + "".join(f"<td style='padding:5px 10px;border-top:1px solid #eee'>{v}</td>" for v in r) + "</tr>"
        for r in rows[:100]
    )
    sql_block = f"<pre style='background:#f6f6f4;padding:10px;border-radius:6px'>{sql}</pre>" if sql else ""
    more = f"<p style='color:#888'>… {len(rows) - 100} more rows not shown</p>" if len(rows) > 100 else ""
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px">
      <h2 style="margin-bottom:4px">◆ Studio — {title}</h2>
      <p>{body_text}</p>
      {sql_block}
      <table style="border-collapse:collapse;font-size:13px"><thead><tr>{head_cells}</tr></thead>
      <tbody>{body_rows}</tbody></table>
      {more}
      <p style="color:#888;font-size:12px">{footer}</p>
    </div>"""
