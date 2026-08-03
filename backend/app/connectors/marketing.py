"""Marketing-stack connectors: GA4, Braze, PowerBI/SAP, Dynamic Yield,
Qualtrics, Google Ads, Microsoft Ads, Sprinklr, Algolia.

Design: each tool is a Studio *source* whose "tables" are its standard
reports. When the agent runs SQL, the connector fetches the referenced
report(s) from the vendor API into an in-memory SQLite database and executes
the SQL there — so the whole agent/queryguard/chart pipeline works unchanged
over API data.

Each connector is enabled by its env keys (see .env.example). Unconfigured
tools show as "not configured" in the source picker, exactly like Snowflake.
Tools whose auth needs a full OAuth app (GA4, Google/Microsoft Ads, PowerBI,
Sprinklr) raise a clear setup message rather than pretending to work.

The long-term ingestion path from the JD still applies: land these tools in
Snowflake (Fivetran/Airbyte/custom pipelines) and chat over the `snowflake`
source; these live connectors cover ad-hoc pulls before pipelines exist.
"""
import os
import re
import sqlite3

import requests

from .base import Connector

TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", re.IGNORECASE)


class ApiReportConnector(Connector):
    """Base: vendor API reports exposed as SQL-queryable tables."""

    dialect = "sqlite"
    #: {table_name: [(col, sqltype), ...]}
    REPORTS = {}

    def list_tables(self):
        return sorted(self.REPORTS.keys())

    def get_schema(self, table):
        if table not in self.REPORTS:
            raise ValueError(f"Unknown report {table}")
        return [{"name": c, "type": t} for c, t in self.REPORTS[table]]

    def fetch_report(self, table):
        """Return (columns, rows) for one report from the vendor API."""
        raise NotImplementedError

    def run_query(self, sql):
        # Load every report the SQL references, then run the SQL locally.
        wanted = {m.lower() for m in TABLE_REF.findall(sql)} & set(self.REPORTS)
        if not wanted:
            wanted = set(list(self.REPORTS)[:1])
        c = sqlite3.connect(":memory:")
        for t in wanted:
            columns, rows = self.fetch_report(t)
            cols_sql = ", ".join(f'"{col}"' for col in columns)
            c.execute(f'CREATE TABLE {t} ({cols_sql})')
            if rows:
                ph = ",".join("?" * len(columns))
                c.executemany(f"INSERT INTO {t} VALUES ({ph})", rows)
        cur = c.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000")))]
        c.close()
        return columns, rows

    @staticmethod
    def _get(url, **kw):
        r = requests.get(url, timeout=60, **kw)
        r.raise_for_status()
        return r.json()


class _OAuthPendingConnector(ApiReportConnector):
    """Tools that need a registered OAuth app / service account. They appear
    in the catalog; querying explains exactly what to configure."""

    SETUP = "See .env.example"

    def fetch_report(self, table):
        raise RuntimeError(f"{self.name}: {self.SETUP}")


# ── Simple API-key tools (live fetch implemented) ───────────────────────

class AlgoliaConnector(ApiReportConnector):
    name = "algolia"
    REPORTS = {
        "algolia_top_searches": [("search", "TEXT"), ("count", "INTEGER"),
                                 ("nb_hits", "INTEGER"), ("click_through_rate", "REAL")],
        "algolia_no_results": [("search", "TEXT"), ("count", "INTEGER"),
                               ("with_filter_count", "INTEGER")],
    }

    def configured(self):
        return bool(os.getenv("ALGOLIA_APP_ID") and os.getenv("ALGOLIA_API_KEY")
                    and os.getenv("ALGOLIA_INDEX"))

    def _headers(self):
        return {"X-Algolia-Application-Id": os.getenv("ALGOLIA_APP_ID"),
                "X-Algolia-API-Key": os.getenv("ALGOLIA_API_KEY")}

    def fetch_report(self, table):
        region = os.getenv("ALGOLIA_ANALYTICS_REGION", "us")
        index = os.getenv("ALGOLIA_INDEX")
        base = f"https://analytics.{region}.algolia.com/2"
        if table == "algolia_top_searches":
            data = self._get(f"{base}/searches?index={index}&limit=500&clickAnalytics=true",
                             headers=self._headers())
            rows = [[s.get("search"), s.get("count"), s.get("nbHits"),
                     s.get("clickThroughRate")] for s in data.get("searches", [])]
        else:
            data = self._get(f"{base}/searches/noResults?index={index}&limit=500",
                             headers=self._headers())
            rows = [[s.get("search"), s.get("count"), s.get("withFilterCount")]
                    for s in data.get("searches", [])]
        return [c for c, _ in self.REPORTS[table]], rows


class BrazeConnector(ApiReportConnector):
    name = "braze"
    REPORTS = {
        "braze_campaigns": [("campaign_id", "TEXT"), ("campaign_name", "TEXT"),
                            ("tags", "TEXT"), ("last_edited", "TEXT")],
        "braze_campaign_stats": [("campaign_id", "TEXT"), ("day", "TEXT"),
                                 ("sends", "INTEGER"), ("opens", "INTEGER"),
                                 ("clicks", "INTEGER"), ("conversions", "INTEGER"),
                                 ("unsubscribes", "INTEGER")],
    }

    def configured(self):
        return bool(os.getenv("BRAZE_API_KEY") and os.getenv("BRAZE_REST_ENDPOINT"))

    def _headers(self):
        return {"Authorization": f"Bearer {os.getenv('BRAZE_API_KEY')}"}

    def fetch_report(self, table):
        base = os.getenv("BRAZE_REST_ENDPOINT").rstrip("/")
        camps = self._get(f"{base}/campaigns/list?page=0", headers=self._headers()).get("campaigns", [])
        if table == "braze_campaigns":
            rows = [[c.get("id"), c.get("name"), ",".join(c.get("tags", [])),
                     c.get("last_edited")] for c in camps]
            return [c for c, _ in self.REPORTS[table]], rows
        rows = []
        for c in camps[:25]:  # data series per campaign, last 30 days
            try:
                series = self._get(
                    f"{base}/campaigns/data_series?campaign_id={c['id']}&length=30",
                    headers=self._headers()).get("data", [])
            except Exception:
                continue
            for d in series:
                m = (d.get("messages", {}).get("email", [{}]) or [{}])[0]
                rows.append([c["id"], d.get("time"), m.get("sent", 0), m.get("opens", 0),
                             m.get("clicks", 0), d.get("conversions", 0),
                             m.get("unsubscribes", 0)])
        return [c for c, _ in self.REPORTS[table]], rows


class QualtricsConnector(ApiReportConnector):
    name = "qualtrics"
    REPORTS = {
        "qualtrics_surveys": [("survey_id", "TEXT"), ("survey_name", "TEXT"),
                              ("is_active", "INTEGER"), ("creation_date", "TEXT")],
        "qualtrics_response_counts": [("survey_id", "TEXT"), ("survey_name", "TEXT"),
                                      ("auditable", "INTEGER"), ("generated", "INTEGER"),
                                      ("deleted", "INTEGER")],
    }

    def configured(self):
        return bool(os.getenv("QUALTRICS_API_TOKEN") and os.getenv("QUALTRICS_DATACENTER"))

    def fetch_report(self, table):
        dc = os.getenv("QUALTRICS_DATACENTER")
        headers = {"X-API-TOKEN": os.getenv("QUALTRICS_API_TOKEN")}
        surveys = self._get(f"https://{dc}.qualtrics.com/API/v3/surveys",
                            headers=headers).get("result", {}).get("elements", [])
        if table == "qualtrics_surveys":
            rows = [[s.get("id"), s.get("name"), int(bool(s.get("isActive"))),
                     s.get("creationDate")] for s in surveys]
        else:
            rows = []
            for s in surveys[:25]:
                try:
                    counts = self._get(
                        f"https://{dc}.qualtrics.com/API/v3/surveys/{s['id']}",
                        headers=headers).get("result", {}).get("responseCounts", {})
                except Exception:
                    counts = {}
                rows.append([s.get("id"), s.get("name"), counts.get("auditable", 0),
                             counts.get("generated", 0), counts.get("deleted", 0)])
        return [c for c, _ in self.REPORTS[table]], rows


class DynamicYieldConnector(ApiReportConnector):
    name = "dynamic_yield"
    REPORTS = {
        "dy_campaign_metrics": [("campaign", "TEXT"), ("day", "TEXT"),
                                ("direct_revenue", "REAL"), ("assisted_revenue", "REAL"),
                                ("ctr", "REAL"), ("purchases", "INTEGER")],
    }
    SETUP = ("set DYNAMIC_YIELD_API_KEY; the reports export API varies by DY plan — "
             "wire the account's report endpoint in connectors/marketing.py::DynamicYieldConnector")

    def configured(self):
        return bool(os.getenv("DYNAMIC_YIELD_API_KEY"))

    def fetch_report(self, table):
        # DY's reporting endpoints differ per section/plan; without a known
        # account shape we fail with guidance instead of guessing.
        raise RuntimeError(f"dynamic_yield: {self.SETUP}")


# ── OAuth-app tools (catalog presence + precise setup guidance) ─────────

class GA4Connector(_OAuthPendingConnector):
    name = "ga4"
    REPORTS = {
        "ga4_sessions": [("date", "TEXT"), ("property", "TEXT"), ("sessions", "INTEGER"),
                         ("events", "INTEGER"), ("conversions", "INTEGER"), ("revenue", "REAL")],
        "ga4_funnel": [("date", "TEXT"), ("property", "TEXT"), ("funnel_stage", "TEXT"),
                       ("users", "INTEGER")],
    }
    SETUP = ("needs a Google Cloud service account with the Analytics Data API: "
             "pip install google-analytics-data, set GA4_PROPERTY_ID and "
             "GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json, then "
             "implement fetch_report with BetaAnalyticsDataClient.run_report")

    def configured(self):
        return bool(os.getenv("GA4_PROPERTY_ID") and os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))


class GoogleAdsConnector(_OAuthPendingConnector):
    name = "google_ads"
    REPORTS = {
        "google_ads_performance": [("date", "TEXT"), ("campaign", "TEXT"),
                                   ("impressions", "INTEGER"), ("clicks", "INTEGER"),
                                   ("cpc", "REAL"), ("conversions", "REAL"),
                                   ("cost", "REAL"), ("roas", "REAL")],
    }
    SETUP = ("needs a Google Ads developer token + OAuth refresh token: "
             "pip install google-ads, configure google-ads.yaml, set GOOGLE_ADS_CUSTOMER_ID")

    def configured(self):
        return bool(os.getenv("GOOGLE_ADS_CUSTOMER_ID"))


class MicrosoftAdsConnector(_OAuthPendingConnector):
    name = "microsoft_ads"
    REPORTS = {
        "microsoft_ads_performance": [("date", "TEXT"), ("campaign", "TEXT"),
                                      ("impressions", "INTEGER"), ("clicks", "INTEGER"),
                                      ("cpc", "REAL"), ("conversions", "REAL"),
                                      ("cost", "REAL"), ("roas", "REAL")],
    }
    SETUP = ("needs a Microsoft Advertising developer token + Azure AD app: "
             "pip install bingads, set MSADS_DEVELOPER_TOKEN / MSADS_CLIENT_ID / MSADS_CUSTOMER_ID")

    def configured(self):
        return bool(os.getenv("MSADS_DEVELOPER_TOKEN"))


class SprinklrConnector(_OAuthPendingConnector):
    name = "sprinklr"
    REPORTS = {
        "sprinklr_social": [("date", "TEXT"), ("brand", "TEXT"), ("network", "TEXT"),
                            ("reach", "INTEGER"), ("engagements", "INTEGER"),
                            ("followers", "INTEGER"), ("reactions", "INTEGER")],
    }
    SETUP = ("needs a Sprinklr API app (enterprise): set SPRINKLR_API_KEY / "
             "SPRINKLR_ACCESS_TOKEN and wire the reporting widget export")

    def configured(self):
        return bool(os.getenv("SPRINKLR_ACCESS_TOKEN"))


class PowerBISAPConnector(_OAuthPendingConnector):
    name = "powerbi_sap"
    REPORTS = {
        "ecommerce_revenue": [("date", "TEXT"), ("channel", "TEXT"), ("orders", "INTEGER"),
                              ("revenue", "REAL"), ("aov", "REAL")],
        "sap_invoices": [("date", "TEXT"), ("invoice_id", "TEXT"), ("customer", "TEXT"),
                         ("amount", "REAL"), ("status", "TEXT")],
    }
    SETUP = ("needs an Azure AD app with Power BI REST permissions: set "
             "POWERBI_CLIENT_ID / POWERBI_TENANT_ID / POWERBI_CLIENT_SECRET and a "
             "dataset ID; SAP data is usually better landed in Snowflake via pipeline")

    def configured(self):
        return bool(os.getenv("POWERBI_CLIENT_ID"))


MARKETING_CONNECTORS = [
    GA4Connector(), BrazeConnector(), PowerBISAPConnector(), DynamicYieldConnector(),
    QualtricsConnector(), GoogleAdsConnector(), MicrosoftAdsConnector(),
    SprinklrConnector(), AlgoliaConnector(),
]
