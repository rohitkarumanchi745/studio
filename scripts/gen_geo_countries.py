"""Regenerate frontend/src/components/renderers/geo-countries.json.

Each row is [ISO numeric, alpha-2, alpha-3, English names…, world-atlas's own
spelling], so a map can match a country written as "US", "USA", "840",
"United States" or "United States of America". Built from the data files of
i18n-iso-countries (MIT) and world-atlas (ISC); neither's code is run.

    npm pack i18n-iso-countries@7.14.0 && tar -xzf i18n-iso-countries-7.14.0.tgz
    python scripts/gen_geo_countries.py package \\
        frontend/node_modules/world-atlas/countries-110m.json \\
        frontend/src/components/renderers/geo-countries.json
"""
import json
import sys


def main(pkg, atlas, out):
    codes = json.load(open(f"{pkg}/codes.json"))
    en = json.load(open(f"{pkg}/langs/en.json"))["countries"]
    topo = json.load(open(atlas))
    atlas_names = {g["id"]: g["properties"]["name"]
                   for g in topo["objects"]["countries"]["geometries"] if "id" in g}
    rows = []
    for a2, a3, num, _ in codes:
        names = en.get(a2)
        names = [names] if isinstance(names, str) else list(names or [])
        if num in atlas_names and atlas_names[num] not in names:
            names.append(atlas_names[num])
        rows.append([num, a2, a3, *names])
    json.dump(rows, open(out, "w"), ensure_ascii=False, separators=(",", ":"))
    print(len(rows), "countries")


if __name__ == "__main__":
    main(*sys.argv[1:4])
