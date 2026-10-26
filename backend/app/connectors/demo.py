"""Demo warehouse — a seeded SQLite database so the full flow works with
zero external credentials. Mirrors the Connector interface used by the
Snowflake and Databricks connectors.
"""
import datetime as dt
import math
import os
import random
import sqlite3

from .base import Connector

WAREHOUSE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "demo_warehouse.db")

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = [
    ("Laptop Pro", "Electronics", 1200),
    ("Laptop Air", "Electronics", 900),
    ("Phone X", "Electronics", 800),
    ("Desk Chair", "Furniture", 220),
    ("Standing Desk", "Furniture", 480),
    ("Monitor 27", "Electronics", 310),
    ("Coffee Maker", "Appliances", 90),
    ("Blender", "Appliances", 60),
]
SEGMENTS = ["Consumer", "SMB", "Enterprise"]
PAGES = ["/home", "/pricing", "/product", "/blog", "/signup"]


def seed():
    if os.path.exists(WAREHOUSE_PATH):
        seed_manufacturing()
        seed_marketing()
        seed_logistics()
        return
    rng = random.Random(42)
    c = sqlite3.connect(WAREHOUSE_PATH)
    c.executescript(
        """
        CREATE TABLE sales (
            order_id INTEGER PRIMARY KEY,
            order_date TEXT NOT NULL,
            region TEXT NOT NULL,
            product TEXT NOT NULL,
            category TEXT NOT NULL,
            units INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            revenue REAL NOT NULL
        );
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT NOT NULL,
            segment TEXT NOT NULL,
            signup_date TEXT NOT NULL,
            lifetime_value REAL NOT NULL
        );
        CREATE TABLE web_traffic (
            day TEXT NOT NULL,
            page TEXT NOT NULL,
            visits INTEGER NOT NULL,
            bounce_rate REAL NOT NULL
        );
        """
    )
    # 3 years so year-over-year, running totals, and moving averages have real
    # prior-period values to compare against. Growth + Q4 seasonality make the
    # trend readable instead of noise.
    SALES_DAYS = 365 * 3
    start = dt.date.today() - dt.timedelta(days=SALES_DAYS)
    order_id = 1000
    for d in range(SALES_DAYS):
        day = start + dt.timedelta(days=d)
        growth = 1.0 + 0.18 * (d / 365.0)
        season = 1.0 + 0.35 * math.sin((day.timetuple().tm_yday / 365.0) * 2 * math.pi - 1.2)
        for _ in range(rng.randint(1, 4)):
            product, category, base_price = rng.choice(PRODUCTS)
            units = max(1, round(rng.randint(1, 8) * season))
            price = round(base_price * rng.uniform(0.9, 1.1) * growth, 2)
            c.execute(
                "INSERT INTO sales VALUES (?,?,?,?,?,?,?,?)",
                (order_id, day.isoformat(), rng.choice(REGIONS), product, category,
                 units, price, round(units * price, 2)),
            )
            order_id += 1
    cities = ["Austin", "Berlin", "Hyderabad", "London", "Sydney", "Toronto"]
    for i in range(120):
        signup = start + dt.timedelta(days=rng.randint(0, SALES_DAYS - 1))
        c.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?,?)",
            (i + 1, f"Customer {i + 1}", rng.choice(cities), rng.choice(SEGMENTS),
             signup.isoformat(), round(rng.uniform(100, 20000), 2)),
        )
    for d in range(90):
        day = (dt.date.today() - dt.timedelta(days=90 - d)).isoformat()
        for page in PAGES:
            c.execute(
                "INSERT INTO web_traffic VALUES (?,?,?,?)",
                (day, page, rng.randint(50, 2500), round(rng.uniform(0.15, 0.75), 3)),
            )
    c.commit()
    c.close()
    seed_manufacturing()
    seed_marketing()
    seed_logistics()


PLANTS = ["Pune", "Chennai", "Frankfurt"]
LINES = ["L1", "L2", "L3"]
MFG_PRODUCTS = ["Gearbox", "Axle", "Brake Pad", "Piston"]
SHIFTS = ["A", "B", "C"]
DOWNTIME_REASONS = ["maintenance", "changeover", "breakdown", "material shortage", "quality hold"]
DEFECT_TYPES = ["dimensional", "surface finish", "porosity", "assembly", "coating"]
MATERIALS = ["steel billet", "aluminium ingot", "friction compound", "bearing set", "seal kit"]


def seed_manufacturing():
    """Manufacturing tables: production, downtime_events, quality_checks,
    inventory. Idempotent — skipped when already present."""
    c = sqlite3.connect(WAREHOUSE_PATH)
    exists = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='production'"
    ).fetchone()
    if exists:
        c.close()
        return
    rng = random.Random(7)
    c.executescript(
        """
        CREATE TABLE production (
            run_id INTEGER PRIMARY KEY,
            run_date TEXT NOT NULL,
            plant TEXT NOT NULL,
            line TEXT NOT NULL,
            product TEXT NOT NULL,
            shift TEXT NOT NULL,
            units_produced INTEGER NOT NULL,
            units_scrap INTEGER NOT NULL,
            cycle_time_sec REAL NOT NULL
        );
        CREATE TABLE downtime_events (
            event_id INTEGER PRIMARY KEY,
            event_date TEXT NOT NULL,
            plant TEXT NOT NULL,
            line TEXT NOT NULL,
            machine TEXT NOT NULL,
            reason TEXT NOT NULL,
            minutes INTEGER NOT NULL
        );
        CREATE TABLE quality_checks (
            check_id INTEGER PRIMARY KEY,
            check_date TEXT NOT NULL,
            plant TEXT NOT NULL,
            product TEXT NOT NULL,
            defect_type TEXT NOT NULL,
            defects_found INTEGER NOT NULL,
            batch_size INTEGER NOT NULL
        );
        CREATE TABLE inventory (
            snapshot_date TEXT NOT NULL,
            plant TEXT NOT NULL,
            material TEXT NOT NULL,
            on_hand_units INTEGER NOT NULL,
            reorder_level INTEGER NOT NULL
        );
        """
    )
    start = dt.date.today() - dt.timedelta(days=365)
    run_id, event_id, check_id = 1, 1, 1
    for d in range(365):
        day = (start + dt.timedelta(days=d)).isoformat()
        for plant in PLANTS:
            for line in LINES:
                for shift in rng.sample(SHIFTS, rng.randint(2, 3)):
                    product = rng.choice(MFG_PRODUCTS)
                    produced = rng.randint(180, 950)
                    scrap = max(0, int(produced * rng.uniform(0.005, 0.06)))
                    c.execute(
                        "INSERT INTO production VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, day, plant, line, product, shift, produced, scrap,
                         round(rng.uniform(24, 95), 1)),
                    )
                    run_id += 1
            # ~1 downtime event per plant-day on average
            if rng.random() < 0.8:
                c.execute(
                    "INSERT INTO downtime_events VALUES (?,?,?,?,?,?,?)",
                    (event_id, day, plant, rng.choice(LINES),
                     f"M-{rng.randint(101, 130)}", rng.choice(DOWNTIME_REASONS),
                     rng.randint(10, 240)),
                )
                event_id += 1
        # quality checks: a few batches per day
        for _ in range(rng.randint(2, 5)):
            batch = rng.randint(200, 1200)
            c.execute(
                "INSERT INTO quality_checks VALUES (?,?,?,?,?,?,?)",
                (check_id, day, rng.choice(PLANTS), rng.choice(MFG_PRODUCTS),
                 rng.choice(DEFECT_TYPES), max(0, int(batch * rng.uniform(0, 0.03))), batch),
            )
            check_id += 1
    # weekly inventory snapshots
    for w in range(52):
        day = (start + dt.timedelta(weeks=w)).isoformat()
        for plant in PLANTS:
            for material in MATERIALS:
                c.execute(
                    "INSERT INTO inventory VALUES (?,?,?,?,?)",
                    (day, plant, material, rng.randint(500, 20000), rng.randint(1500, 4000)),
                )
    c.commit()
    c.close()




CHANNELS = ["organic", "paid_search", "email", "social", "direct"]
PROGRAMS = ["Welcome Series", "Cart Abandon", "Winback", "Newsletter", "Promo Blast"]
FUNNEL = ["view_item", "add_to_cart", "begin_checkout", "purchase"]
AD_PLATFORMS = ["Google Ads", "Microsoft Ads"]
NETWORKS = ["Instagram", "Facebook", "LinkedIn", "X"]
BRANDS = ["OBE Glass", "OBE Doors", "OBE Systems"]
DY_CAMPAIGNS = ["Homepage Hero", "PDP Recs", "Exit Intent", "Category Sort"]
SEARCH_TERMS = ["shower door", "storefront glass", "curtain wall", "glass railing",
                "mirror", "entrance system", "hurricane glass", "spandrel"]
DIFFICULTY = ["checkout", "search", "product info", "shipping", "account"]


def seed_marketing():
    """Marketing analytics tables mirroring the priority source tools
    (GA4, Braze, PowerBI/SAP ecommerce, Dynamic Yield, Qualtrics,
    Google/Microsoft Ads, Sprinklr, Algolia). Idempotent."""
    c = sqlite3.connect(WAREHOUSE_PATH)
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ga4_sessions'").fetchone():
        c.close()
        return
    rng = random.Random(11)
    c.executescript(
        """
        CREATE TABLE ga4_sessions (
            date TEXT, property TEXT, sessions INTEGER, events INTEGER,
            conversions INTEGER, revenue REAL, funnel_stage TEXT
        );
        CREATE TABLE braze_email (
            date TEXT, program TEXT, sends INTEGER, opens INTEGER,
            clicks INTEGER, conversions INTEGER, unsubscribes INTEGER
        );
        CREATE TABLE ecommerce_orders (
            date TEXT, channel TEXT, orders INTEGER, revenue REAL, aov REAL
        );
        CREATE TABLE dynamic_yield_campaigns (
            date TEXT, campaign TEXT, direct_revenue REAL, assisted_revenue REAL,
            ctr REAL, purchases INTEGER
        );
        CREATE TABLE qualtrics_nps (
            date TEXT, survey TEXT, nps REAL, ces REAL, responses INTEGER,
            difficulty_driver TEXT
        );
        CREATE TABLE ads_performance (
            date TEXT, platform TEXT, campaign TEXT, impressions INTEGER,
            clicks INTEGER, cpc REAL, conversions INTEGER, cost REAL, roas REAL
        );
        CREATE TABLE sprinklr_social (
            date TEXT, brand TEXT, network TEXT, reach INTEGER,
            engagements INTEGER, followers INTEGER, reactions INTEGER
        );
        CREATE TABLE algolia_search (
            date TEXT, search_term TEXT, searches INTEGER, result_rate REAL,
            zero_results INTEGER
        );
        """
    )
    start = dt.date.today() - dt.timedelta(days=180)
    props = ["obe.com", "shop.obe.com"]
    for d in range(180):
        day = (start + dt.timedelta(days=d)).isoformat()
        for prop in props:
            for stage in FUNNEL:
                base = {"view_item": rng.randint(2000, 6000), "add_to_cart": rng.randint(400, 1500),
                        "begin_checkout": rng.randint(150, 600), "purchase": rng.randint(40, 260)}[stage]
                c.execute("INSERT INTO ga4_sessions VALUES (?,?,?,?,?,?,?)",
                          (day, prop, base, base * rng.randint(2, 5),
                           int(base * rng.uniform(0.02, 0.2)),
                           round(base * rng.uniform(1.5, 9.0), 2) if stage == "purchase" else 0.0,
                           stage))
        for program in PROGRAMS:
            sends = rng.randint(800, 12000)
            opens = int(sends * rng.uniform(0.18, 0.45))
            clicks = int(opens * rng.uniform(0.08, 0.3))
            c.execute("INSERT INTO braze_email VALUES (?,?,?,?,?,?,?)",
                      (day, program, sends, opens, clicks,
                       int(clicks * rng.uniform(0.05, 0.3)), int(sends * rng.uniform(0.0005, 0.004))))
        for channel in CHANNELS:
            orders = rng.randint(15, 220)
            aov = round(rng.uniform(180, 900), 2)
            c.execute("INSERT INTO ecommerce_orders VALUES (?,?,?,?,?)",
                      (day, channel, orders, round(orders * aov, 2), aov))
        for camp in DY_CAMPAIGNS:
            direct = round(rng.uniform(500, 9000), 2)
            c.execute("INSERT INTO dynamic_yield_campaigns VALUES (?,?,?,?,?,?)",
                      (day, camp, direct, round(direct * rng.uniform(0.3, 1.4), 2),
                       round(rng.uniform(0.5, 6.5), 2), rng.randint(5, 140)))
        for platform in AD_PLATFORMS:
            for camp in ["Brand", "Non-Brand", "Retargeting"]:
                imps = rng.randint(5000, 90000)
                clicks = int(imps * rng.uniform(0.01, 0.06))
                cpc = round(rng.uniform(0.8, 6.5), 2)
                cost = round(clicks * cpc, 2)
                conv = int(clicks * rng.uniform(0.02, 0.11))
                c.execute("INSERT INTO ads_performance VALUES (?,?,?,?,?,?,?,?,?)",
                          (day, platform, camp, imps, clicks, cpc, conv, cost,
                           round(conv * rng.uniform(150, 700) / max(cost, 1), 2)))
        for brand in BRANDS:
            for network in NETWORKS:
                c.execute("INSERT INTO sprinklr_social VALUES (?,?,?,?,?,?,?)",
                          (day, brand, network, rng.randint(2000, 80000),
                           rng.randint(50, 4000), rng.randint(5000, 90000),
                           rng.randint(20, 2500)))
        for term in rng.sample(SEARCH_TERMS, 5):
            searches = rng.randint(20, 900)
            rate = round(rng.uniform(0.55, 0.99), 3)
            c.execute("INSERT INTO algolia_search VALUES (?,?,?,?,?)",
                      (day, term, searches, rate, int(searches * (1 - rate))))
    # weekly NPS/CES surveys
    for w in range(26):
        day = (start + dt.timedelta(weeks=w)).isoformat()
        for survey in ["Post-Purchase", "Site Feedback"]:
            c.execute("INSERT INTO qualtrics_nps VALUES (?,?,?,?,?,?)",
                      (day, survey, round(rng.uniform(18, 62), 1), round(rng.uniform(2.1, 4.6), 2),
                       rng.randint(40, 400), rng.choice(DIFFICULTY)))
    c.commit()
    c.close()


# Places with real coordinates, so the maps have something to draw: Chicago
# 'L' stations by line (route maps of rides), countries that trade (route maps
# and shaded countries), and US stores (dots, and shaded states).
L_LINES = {
    "Blue": [("O'Hare", 41.9773, -87.9045), ("Clark/Lake", 41.8857, -87.6309),
             ("Forest Park", 41.8740, -87.8171)],
    "Red": [("Howard", 42.0190, -87.6728), ("Clark/Lake", 41.8857, -87.6309),
            ("95th/Dan Ryan", 41.7222, -87.6244)],
    "Orange": [("Midway", 41.7868, -87.7375), ("Clark/Lake", 41.8857, -87.6309)],
    "Brown": [("Kimball", 41.9678, -87.7132), ("Clark/Lake", 41.8857, -87.6309)],
    "Green": [("Harlem/Lake", 41.8869, -87.8034), ("Clark/Lake", 41.8857, -87.6309),
              ("Ashland/63rd", 41.7781, -87.6638)],
    "Pink": [("54th/Cermak", 41.8517, -87.7567), ("Clark/Lake", 41.8857, -87.6309)],
    "Purple": [("Linden", 42.0732, -87.6909), ("Howard", 42.0190, -87.6728)],
}
TRADE_LANES = [
    ("China", "United States", "Ocean"), ("United States", "Canada", "Road"),
    ("Mexico", "United States", "Road"), ("Germany", "United States", "Air"),
    ("United States", "United Kingdom", "Air"), ("Japan", "United States", "Ocean"),
    ("South Korea", "Germany", "Ocean"), ("India", "United Kingdom", "Air"),
    ("Netherlands", "Germany", "Road"), ("France", "Spain", "Road"),
    ("Brazil", "United States", "Ocean"), ("Australia", "Japan", "Ocean"),
    ("China", "Australia", "Ocean"), ("Canada", "United Kingdom", "Air"),
    ("United States", "Brazil", "Ocean"), ("Germany", "France", "Road"),
]
STORES = [
    ("New York", "NY", 40.7128, -74.0060), ("Los Angeles", "CA", 34.0522, -118.2437),
    ("Chicago", "IL", 41.8781, -87.6298), ("Houston", "TX", 29.7604, -95.3698),
    ("Phoenix", "AZ", 33.4484, -112.0740), ("Philadelphia", "PA", 39.9526, -75.1652),
    ("San Diego", "CA", 32.7157, -117.1611), ("Dallas", "TX", 32.7767, -96.7970),
    ("Austin", "TX", 30.2672, -97.7431), ("Seattle", "WA", 47.6062, -122.3321),
    ("Denver", "CO", 39.7392, -104.9903), ("Boston", "MA", 42.3601, -71.0589),
    ("Miami", "FL", 25.7617, -80.1918), ("Atlanta", "GA", 33.7490, -84.3880),
    ("Minneapolis", "MN", 44.9778, -93.2650), ("Portland", "OR", 45.5152, -122.6784),
    ("Las Vegas", "NV", 36.1699, -115.1398), ("Detroit", "MI", 42.3314, -83.0458),
    ("Nashville", "TN", 36.1627, -86.7816), ("Charlotte", "NC", 35.2271, -80.8431),
    ("Columbus", "OH", 39.9612, -82.9988), ("Salt Lake City", "UT", 40.7608, -111.8910),
    ("Kansas City", "MO", 39.0997, -94.5786), ("Raleigh", "NC", 35.7796, -78.6382),
    ("Honolulu", "HI", 21.3069, -157.8583), ("Anchorage", "AK", 61.2181, -149.9003),
]


def seed_logistics():
    """Tables with places in them, for the maps: transit_trips (rides between
    Chicago 'L' stations, with each end's latitude/longitude), shipments
    (goods between countries) and stores (US stores with coordinates and
    state). Idempotent — skipped when already present."""
    c = sqlite3.connect(WAREHOUSE_PATH)
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='transit_trips'").fetchone():
        c.close()
        return
    rng = random.Random(23)
    c.executescript(
        """
        CREATE TABLE transit_trips (
            trip_date TEXT, line TEXT, from_stop TEXT, to_stop TEXT,
            from_lat REAL, from_lon REAL, to_lat REAL, to_lon REAL, riders INTEGER
        );
        CREATE TABLE shipments (
            ship_date TEXT, origin_country TEXT, destination_country TEXT, mode TEXT,
            units INTEGER, revenue REAL
        );
        CREATE TABLE stores (
            store_id INTEGER, store TEXT, city TEXT, state TEXT, latitude REAL, longitude REAL,
            revenue REAL, orders INTEGER
        );
        """
    )
    start = dt.date.today() - dt.timedelta(days=89)
    trips, ships = [], []
    for d in range(90):
        day = start + dt.timedelta(days=d)
        weekend = day.weekday() >= 5
        for line, stops in L_LINES.items():
            for a, b in zip(stops, stops[1:]):
                for (fa, la, lo), (fb, lb, ob) in ((a, b), (b, a)):
                    base = 2400 if "Clark/Lake" in (fa, fb) else 1100
                    riders = int(base * (0.55 if weekend else 1.0) * rng.uniform(0.8, 1.2))
                    trips.append((day.isoformat(), line, fa, fb, la, lo, lb, ob, riders))
        for origin, dest, mode in TRADE_LANES:
            if rng.random() < 0.6:
                units = rng.randint(200, 5000) * (3 if mode == "Ocean" else 1)
                ships.append((day.isoformat(), origin, dest, mode, units,
                              round(units * rng.uniform(18, 60), 2)))
    c.executemany("INSERT INTO transit_trips VALUES (?,?,?,?,?,?,?,?,?)", trips)
    c.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?)", ships)
    c.executemany("INSERT INTO stores VALUES (?,?,?,?,?,?,?,?)", [
        (i + 1, f"Studio {city}", city, state, lat, lon,
         round(rng.uniform(0.4, 4.5) * 1e6, 2), rng.randint(2000, 30000))
        for i, (city, state, lat, lon) in enumerate(STORES)])
    c.commit()
    c.close()


class DemoConnector(Connector):
    name = "demo"
    dialect = "sqlite"
    # No qualifiers(): one SQLite file has no schema/catalog namespace, so the
    # base class's empty set is right — the guard then refuses any qualified
    # reference (`main.sales`, `other.sales`) instead of reducing it to a bare
    # allowed name.

    def configured(self):
        return True

    def _conn(self):
        seed()
        return sqlite3.connect(WAREHOUSE_PATH)

    def list_tables(self):
        c = self._conn()
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        c.close()
        return [r[0] for r in rows]

    def get_schema(self, table):
        c = self._conn()
        # PRAGMA can't be parameterized; table names come from our own catalog.
        if table not in self.list_tables():
            raise ValueError(f"Unknown table {table}")
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
        c.close()
        return [{"name": r[1], "type": r[2]} for r in rows]

    def describe_constraints(self):
        """Primary keys and foreign keys SQLite records for each table."""
        c = self._conn()
        try:
            out = {"primary_keys": {}, "foreign_keys": []}
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                quoted = name.replace('"', '""')
                pk = sorted((r[5], r[1]) for r in c.execute(f'PRAGMA table_info("{quoted}")')
                            if r[5])
                if pk:
                    out["primary_keys"][name] = [col for _, col in pk]
                fks = {}
                for r in c.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                    fks.setdefault(r[0], {"name": f"{name}_fk{r[0]}", "from": name,
                                          "from_columns": [], "to": r[2], "to_columns": []})
                    fks[r[0]]["from_columns"].append(r[3])
                    fks[r[0]]["to_columns"].append(r[4])
                out["foreign_keys"] += [fk for fk in fks.values() if all(fk["to_columns"])]
            return out
        finally:
            c.close()

    def view_definitions(self):
        c = self._conn()
        try:
            return {name: sql for name, sql in c.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='view'") if sql}
        finally:
            c.close()

    def table_signals(self):
        """{relation: hash of its kind and schema text} for every table and
        view — the same relations change_signal() covers."""
        import hashlib
        c = self._conn()
        try:
            rows = c.execute("SELECT name, type, sql FROM sqlite_master "
                             "WHERE type IN ('table', 'view')").fetchall()
        finally:
            c.close()
        return {name: hashlib.sha256(f"{kind}|{sql or ''}".encode()).hexdigest()[:16]
                for name, kind, sql in rows}

    def change_signal(self):
        """A hash of the schema text SQLite keeps for every table and view."""
        import hashlib
        c = self._conn()
        try:
            rows = c.execute("SELECT type, name, sql FROM sqlite_master "
                             "WHERE type IN ('table', 'view') ORDER BY name").fetchall()
        finally:
            c.close()
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    def describe_relation(self, name):
        """Kind and columns of one table or view in the demo warehouse."""
        c = self._conn()
        try:
            row = c.execute("SELECT type FROM sqlite_master WHERE name = ? "
                            "AND type IN ('table', 'view')", (name,)).fetchone()
            if row is None:
                return None
            quoted = name.replace('"', '""')
            cols = c.execute(f'PRAGMA table_info("{quoted}")').fetchall()
            return {"kind": row[0], "namespace": None,
                    "columns": [{"name": r[1], "type": r[2]} for r in cols]}
        finally:
            c.close()

    def describe_relations(self):
        """Kind and columns for every table and view in the demo warehouse."""
        c = self._conn()
        try:
            rows = c.execute("SELECT name, type FROM sqlite_master "
                             "WHERE type IN ('table', 'view') ORDER BY name").fetchall()
            out = {}
            for name, kind in rows:
                # PRAGMA can't be parameterized; names come from sqlite_master.
                quoted = name.replace('"', '""')
                cols = c.execute(f'PRAGMA table_info("{quoted}")').fetchall()
                out[name] = {"kind": kind, "namespace": None,
                             "columns": [{"name": r[1], "type": r[2]} for r in cols]}
            return out
        finally:
            c.close()

    def run_query(self, sql):
        c = self._conn()
        cur = c.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000")))]
        c.close()
        return columns, rows

    def run_script(self, sql):
        # The demo warehouse is a read-only sandbox — writes are refused even
        # after approval, which is what a locked-down environment should do.
        raise PermissionError(
            "demo is a read-only sandbox — writes are blocked. Point the job at "
            "a configured Snowflake or Databricks environment.")
