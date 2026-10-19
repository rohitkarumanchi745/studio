"""Which columns of a result are places — for picking a map.

The browser draws the maps (frontend renderers/geo.js holds the full rules
and the country / US-state gazetteer); this is the same column-naming rule,
so the keyless fallback and the canvas can choose a map without a model:

  * a latitude/longitude pair, matched by name (``stop_lat``/``stop_lon``,
    ``pickup_latitude``/``pickup_longitude``), whose values are plausible
    degrees;
  * a pair whose name says where a trip begins (from, origin, pickup,
    start…) and one where it ends (to, dest, dropoff, end…) — movement:
    transit, shipments, trips → a route map;
  * otherwise a pair → a point map; a column named country / state with a
    value → a choropleth.

Stdlib only; nothing here throws. Used by agent.py (its keyless chart pick
and canvas keyword edits).
"""
from __future__ import annotations

import re

MAP_TYPES = ("route_map", "point_map", "choropleth")

_LAT = {"lat", "latitude"}
_LON = {"lon", "lng", "long", "longitude"}
_ORIGIN = {"origin", "orig", "from", "src", "source", "start", "pickup", "departure",
           "depart", "dep", "o"}
_DEST = {"dest", "destination", "to", "dst", "target", "end", "dropoff", "arrival", "arr", "d"}


def tokens(name):
    """"PickupLatitude" / "pickup-latitude" → ["pickup", "latitude"]."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name or ""))
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def _degrees(rows, i, limit):
    seen = good = 0
    for r in rows:
        v = r[i] if i < len(r) else None
        if v is None or v == "":
            continue
        seen += 1
        try:
            if abs(float(v)) <= limit:
                good += 1
        except (TypeError, ValueError):
            pass
    return seen > 0 and good / seen >= 0.9


def coordinate_pairs(columns, rows):
    """[(key, role, lat_index, lon_index)], role "origin" | "dest" | "point"."""
    lats, lons = {}, {}
    for i, c in enumerate(columns):
        t = tokens(c)
        at = next((j for j, x in enumerate(t) if x in _LAT or x in _LON), None)
        if at is None:
            continue
        key = "_".join(x for j, x in enumerate(t) if j != at)
        (lats if t[at] in _LAT else lons)[key] = i
    out = []
    for key, lat in lats.items():
        lon = lons.get(key)
        if lon is None or not _degrees(rows, lat, 90) or not _degrees(rows, lon, 180):
            continue
        parts = set(key.split("_")) if key else set()
        role = "origin" if parts & _ORIGIN else "dest" if parts & _DEST else "point"
        out.append((key, role, lat, lon))
    return out


_PLACE = {"country", "countries", "state", "nation"}


def _role(name):
    t = set(tokens(name))
    return "origin" if t & _ORIGIN else "dest" if t & _DEST else None


def _place_ends(columns):
    """(origin, destination) columns naming countries / states by role —
    origin_country → destination_country — or (None, None)."""
    places = [c for c in columns if set(tokens(c)) & _PLACE]
    origin = next((c for c in places if _role(c) == "origin"), None)
    dest = next((c for c in places if _role(c) == "dest"), None)
    return (origin, dest) if origin and dest else (None, None)


def map_for_columns(columns):
    """The map a result's column NAMES call for (no rows to check): routes
    when trips have a start and an end, regions for a country / state column,
    else places as dots."""
    roles = set()
    lat_keys, lon_keys = set(), set()
    for c in columns:
        t = tokens(c)
        at = next((j for j, x in enumerate(t) if x in _LAT or x in _LON), None)
        if at is None:
            continue
        key = "_".join(x for j, x in enumerate(t) if j != at)
        (lat_keys if t[at] in _LAT else lon_keys).add(key)
    for key in lat_keys & lon_keys:
        parts = set(key.split("_")) if key else set()
        roles.add("origin" if parts & _ORIGIN else "dest" if parts & _DEST else "point")
    if {"origin", "dest"} <= roles or _place_ends(columns)[0]:
        return "route_map"
    if roles:
        return "point_map"
    if any(set(tokens(c)) & _PLACE for c in columns):
        return "choropleth"
    return "point_map"


def suggest_map(columns, rows):
    """A chart spec for a map the result plainly asks for, or None."""
    if not rows:
        return None
    pairs = coordinate_pairs(columns, rows)
    roles = {p[1] for p in pairs}
    first = rows[0]
    numeric = [c for c, v in zip(columns, first)
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    coord_cols = {columns[i] for p in pairs for i in p[2:]}
    measures = [c for c in numeric if c not in coord_cols]
    texty = [c for c, v in zip(columns, first) if isinstance(v, str)]
    if {"origin", "dest"} <= roles:
        return {"type": "route_map", "title": "Routes", "x": texty[0] if texty else columns[0],
                "y": measures[:1]}
    if pairs:
        return {"type": "point_map", "title": "Places", "x": texty[0] if texty else columns[0],
                "y": measures[:1]}
    origin, dest = _place_ends([c for c in texty])
    if origin:
        return {"type": "route_map", "title": f"{origin} → {dest}", "x": origin,
                "y": [dest, *measures[:1]]}
    place = next((c for c in texty if set(tokens(c)) & _PLACE), None)
    if place and measures:
        return {"type": "choropleth", "title": f"{measures[0]} by {place}", "x": place,
                "y": measures[:1]}
    return None
