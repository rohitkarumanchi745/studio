"""Downloads: an answer's data as CSV or Excel, and a report as one HTML page.

Through the real API, signed in: the files carry exactly the rows sent (no
query runs), a value a spreadsheet would read as a formula stays text in both
formats, the report is static escaped HTML whose only images are checked PNG
or SVG data URLs, sizes are bounded, and every download is an audit row.

Run from the backend directory:
    python -m pytest tests/test_exports.py -q
"""
import base64
import csv
import io
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-export-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import db, exports

ANALYST = ("analyst@studio.local", "analyst123")
COLUMNS = ["region", "revenue", "note"]
ROWS = [["EU", 120.5, "=HYPERLINK(\"http://evil.example\",\"click\")"],
        ["US", -80, "+1 555 0100"], ["APAC", None, "@SUM(A1:A9)"],
        ["Zürich", 3, "plain, with comma"], ["-5", 0, "-12.5"]]
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
SVG = "data:image/svg+xml;base64," + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()


@pytest.fixture(scope="module")
def client():
    import app.main as main
    mp = pytest.MonkeyPatch()
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM"):
        mp.delenv(k, raising=False)
    mp.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    with TestClient(main.app) as c:
        yield c
    mp.undo()


@pytest.fixture(scope="module")
def auth(client):
    tok = client.post("/api/auth/login", json={"email": ANALYST[0], "password": ANALYST[1]}
                      ).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _exports():
    """Export audit rows, oldest first (the log lists newest first)."""
    return [r for r in db.list_activity() if r["action"] == "export"][::-1]


def test_csv_is_the_rows_sent_with_formulas_kept_as_text(client, auth):
    r = client.post("/api/export/csv", headers=auth, json={
        "title": "Revenue by region / Q3", "columns": COLUMNS, "rows": ROWS,
        "sql": "SELECT region, revenue FROM sales"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="Revenue-by-region-Q3.csv"'
    assert r.content.startswith("﻿".encode())                  # Excel reads it as UTF-8
    got = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert got[0] == COLUMNS
    assert got[1] == ["EU", "120.5", "'=HYPERLINK(\"http://evil.example\",\"click\")"]
    assert got[2] == ["US", "-80", "'+1 555 0100"]
    assert got[3] == ["APAC", "", "'@SUM(A1:A9)"]
    assert got[4] == ["Zürich", "3", "plain, with comma"]
    assert got[5] == ["-5", "0", "-12.5"]                           # numbers stay numbers
    row = _exports()[-1]
    assert row["mode"] == "csv" and row["row_count"] == 5 and row["email"] == ANALYST[0]


def test_excel_has_the_data_as_text_never_formulas_and_says_what_it_is(client, auth):
    r = client.post("/api/export/xlsx", headers=auth, json={
        "title": "Revenue", "columns": COLUMNS, "rows": ROWS, "sql": "SELECT 1",
        "text": "EU leads.", "source": "demo"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == exports.XLSX
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    data = wb["Data"]
    assert [c.value for c in data[1]] == COLUMNS and data["A1"].font.bold
    assert data["C2"].value == "=HYPERLINK(\"http://evil.example\",\"click\")"
    assert data["C2"].data_type == "s"                              # text, not a formula
    assert data["B2"].value == 120.5 and data["B3"].value == -80 and data["B4"].value is None
    assert data.freeze_panes == "A2"
    about = {row[0].value: row[1].value for row in wb["About"].iter_rows()}
    assert about["SQL"] == "SELECT 1" and about["Source"] == "demo" and about["Rows"] == 5
    assert about["Exported by"] == ANALYST[0]
    assert _exports()[-1]["mode"] == "xlsx"


def test_the_report_is_static_escaped_html(client, auth):
    r = client.post("/api/export/report", headers=auth, json={
        "title": "Weekly <review>", "subtitle": "shop",
        "sections": [
            {"title": "Revenue <b>by</b> region", "text": "EU leads.\n<script>alert(1)</script>",
             "image": PNG, "columns": COLUMNS, "rows": ROWS, "sql": "SELECT * FROM t WHERE a<1",
             "source": "shop"},
            {"title": "Trend", "image": SVG}]})
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == 'attachment; filename="Weekly-review.html"'
    page = r.text
    assert "<script" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Revenue &lt;b&gt;by&lt;/b&gt; region" in page and "a&lt;1" in page
    assert "default-src &#x27;none&#x27;" in page or "default-src 'none'" in page
    assert page.count("<img ") == 2 and 'src="data:image/svg+xml;base64,' in page
    assert "<td>Zürich</td>" in page and "Exported by analyst@studio.local" in page
    assert _exports()[-1]["mode"] == "report"


def test_a_report_table_is_capped_and_says_so(client, auth):
    rows = [[i] for i in range(exports.REPORT_ROWS + 5)]
    page = client.post("/api/export/report", headers=auth, json={
        "title": "Big", "sections": [{"title": "All", "columns": ["n"], "rows": rows}]}).text
    assert page.count("<tr>") == exports.REPORT_ROWS + 1                # header + capped rows
    assert "The first 1,000 of 1,005 rows" in page


@pytest.mark.parametrize("image", [
    "javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=",
    "data:image/png;base64,not base64!", "https://evil.example/x.png"])
def test_a_report_embeds_only_png_or_svg_data_images(client, auth, image):
    r = client.post("/api/export/report", headers=auth, json={
        "title": "x", "sections": [{"title": "s", "image": image}]})
    assert r.status_code == 400


def test_sizes_are_bounded_and_sign_in_is_required(client, auth, monkeypatch):
    monkeypatch.setattr(exports, "MAX_ROWS", 3)
    r = client.post("/api/export/csv", headers=auth, json={"columns": ["a"], "rows": [[1]] * 4})
    assert r.status_code == 413
    monkeypatch.setattr(exports, "MAX_IMAGE_BYTES", 4)
    r = client.post("/api/export/report", headers=auth, json={
        "title": "x", "sections": [{"image": PNG}]})
    assert r.status_code == 413
    assert client.post("/api/export/report", headers=auth,
                       json={"title": "x", "sections": []}).status_code == 400
    assert client.post("/api/export/csv", json={"columns": ["a"], "rows": [[1]]}).status_code == 401


def test_file_names_are_safe():
    assert exports.filename("../../etc/passwd", "csv") == "etc-passwd.csv"
    assert exports.filename("", "xlsx") == "studio-export.xlsx"
    assert exports.filename('a"b\r\nc', "html") == "a-b-c.html"
