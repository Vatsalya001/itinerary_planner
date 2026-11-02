# Rebuilding the curated Bangalore snapshot

The snapshot (`backend/data/snapshot/bangalore.json`) is built from one OpenStreetMap Overpass download
(ODbL; see `backend/data/SOURCES.md` for date, licence and mapping rules).

```bash
# 1. Download (one polite request; generic User-Agent, no personal data)
curl -sS -A "SaturdayPlanner/0.1 (snapshot build)" --data-urlencode data@query.overpassql \
  https://overpass-api.de/api/interpreter -o /tmp/overpass.json
# 2. Curate into the snapshot (the CURATED table in build_snapshot.py selects and maps places)
uv run python scripts/snapshot/build_snapshot.py /tmp/overpass.json data/snapshot/bangalore.json
```

Run from `backend/`. Re-check `SOURCES.md` and the fixture-integrity tests after a rebuild.
