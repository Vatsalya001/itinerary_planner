"""Build backend/data/snapshot/bangalore.json from one Overpass download (curation table below)."""
import json
import sys

src, out = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    data = json.load(fh)
by_key = {(e["type"], e["id"]): e for e in data["elements"]}

# (osm type, id) -> (category, popularity, curation note or None)
CURATED = [
    # food with OSM diet evidence
    (("node", 677286843), "restaurant", "medium", None),
    (("way", 370418975), "restaurant", "medium", None),
    (("node", 1722589278), "restaurant", "medium", None),
    (("node", 1577582017), "restaurant", "medium", None),
    (("node", 10761493314), "cafe", "medium", None),
    (("node", 6819880190), "restaurant", "medium", None),
    (("node", 8347895697), "cafe", "medium", None),
    (("node", 10178386010), "cafe", "medium", None),
    (("node", 9704972553), "restaurant", "medium", None),
    (("node", 9890165409), "cafe", "medium", None),
    (("node", 2198288187), "street_food", "medium", None),
    (("node", 5915195103), "restaurant", "medium", None),
    (("node", 1375978284), "restaurant", "medium", None),
    (("node", 4506805592), "cafe", "medium", None),
    (("node", 1617398428), "street_food", "medium", None),
    (("node", 4443365241), "restaurant", "medium", None),
    (("node", 3842746873), "restaurant", "medium", None),
    (("node", 428845624), "restaurant", "high", "Popularity set to high by curation (well-known, busy venue); a heuristic, not an OSM fact."),
    (("way", 380094757), "street_food", "medium", None),
    # culture
    (("way", 1124083558), "museum", "medium", None),
    (("way", 327551780), "museum", "medium", None),
    (("way", 52032467), "museum", "medium", None),
    (("way", 1156298790), "art_gallery", "medium", None),
    (("way", 121883110), "museum", "medium", None),
    (("node", 10015163287), "art_gallery", "medium", None),
    (("way", 327534040), "art_gallery", "medium", "OSM amenity=arts_centre mapped to art_gallery."),
    (("node", 7293721311), "museum", "medium", None),
    (("node", 9536777477), "museum", "medium", None),
    (("way", 35277205), "art_gallery", "medium", "OSM amenity=arts_centre (cultural centre) mapped to art_gallery."),
    (("node", 1138468595), "art_gallery", "medium", "OSM amenity=arts_centre (cultural centre) mapped to art_gallery."),
    (("way", 335169246), "live_music_venue", "medium", "OSM amenity=theatre; categorised as a live music venue because it is a concert hall (curation)."),
    (("node", 6594341985), "bookstore", "medium", "OSM amenity=cafe; categorised as a bookstore from its name 'Bookstore, Library & Cafe' (curation)."),
    # books
    (("node", 6400075545), "bookstore", "medium", None),
    (("relation", 20071915), "bookstore", "medium", None),
    (("node", 3701774141), "bookstore", "medium", None),
    (("node", 1328610696), "bookstore", "medium", None),
    # outdoors
    (("way", 22895320), "park", "medium", None),
    (("way", 15802464), "garden", "high", "OSM leisure=park; categorised as a garden (botanical garden). Popularity high by curation (heuristic)."),
    (("way", 36884560), "park", "medium", None),
    (("way", 299877333), "park", "medium", None),
    (("relation", 1857615), "lake", "medium", None),
]
KEEP = ["name", "amenity", "leisure", "tourism", "shop", "natural", "water", "cuisine", "diet:vegetarian",
        "diet:vegan", "diet:jain", "diet:plant_based", "wheelchair", "opening_hours", "fee", "addr:housenumber",
        "addr:street", "wikidata"]
places = []
for (otype, oid), cat, pop, note in CURATED:
    e = by_key[(otype, oid)]
    tags = e.get("tags", {})
    lat = e.get("lat", e.get("center", {}).get("lat"))
    lon = e.get("lon", e.get("center", {}).get("lon"))
    rec = {
        "id": f"snap:osm-{otype}-{oid}",
        "osm_type": otype,
        "osm_id": oid,
        "name": tags["name"],
        "category": cat,
        "lat": round(lat, 6),
        "lng": round(lon, 6),
        "popularity": pop,
        "osm_tags": {k: tags[k] for k in KEEP if k in tags},
    }
    if note:
        rec["curation_note"] = note
    places.append(rec)

doc = {
    "_about": "Curated Bengaluru snapshot built from OpenStreetMap data (one Overpass API query, see backend/data/SOURCES.md). Facts in osm_tags are OSM data (c) OpenStreetMap contributors, ODbL 1.0. category, popularity and curation_note are curation decisions, not OSM facts. Costs are NOT stored: providers use category defaults labelled 'category_estimate'. Neighbourhood centroids are approximate and hand-set (not OSM).",
    "version": 1,
    "city": {"name": "Bengaluru", "country_code": "IN", "timezone": "Asia/Kolkata", "currency": "INR", "lat": 12.9716, "lng": 77.5946},
    "source": {
        "provider": "OpenStreetMap via Overpass API",
        "endpoint": "https://overpass-api.de/api/interpreter",
        "osm_base_timestamp": data.get("osm3s", {}).get("timestamp_osm_base"),
        "retrieved": "2026-11-02",
        "licence": "ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/)",
        "attribution": "© OpenStreetMap contributors",
        "query_file": "backend/data/SOURCES.md",
    },
    "last_checked": "2026-11-02",
    "neighbourhoods": {
        "mg road": [12.9756, 77.6050], "m g road": [12.9756, 77.6050], "cubbon park": [12.9763, 77.5929],
        "church street": [12.9752, 77.6050], "lavelle road": [12.9710, 77.5980], "kasturba road": [12.9745, 77.5962],
        "st marks road": [12.9740, 77.6010], "indiranagar": [12.9750, 77.6405], "indira nagar": [12.9750, 77.6405],
        "koramangala": [12.9352, 77.6245], "basavanagudi": [12.9420, 77.5735], "lalbagh": [12.9507, 77.5848],
        "malleshwaram": [13.0035, 77.5700], "ulsoor": [12.9810, 77.6200], "halasuru": [12.9810, 77.6200],
        "vasanth nagar": [12.9890, 77.5930], "richmond town": [12.9650, 77.6030], "shanthinagar": [12.9580, 77.5990]
    },
    "places": places,
}
def dumps(v):
    return json.dumps(v, ensure_ascii=False)
lines = ["{"]
keys = list(doc)
for i, k in enumerate(keys):
    comma = "," if i < len(keys) - 1 else ""
    if k == "places":
        lines.append('  "places": [')
        for j, p in enumerate(doc["places"]):
            lines.append("    " + dumps(p) + ("," if j < len(doc["places"]) - 1 else ""))
        lines.append("  ]" + comma)
    elif isinstance(doc[k], dict):
        lines.append(f"  {dumps(k)}: " + dumps(doc[k]) + comma)
    else:
        lines.append(f"  {dumps(k)}: {dumps(doc[k])}{comma}")
lines.append("}")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(len(places), "places written")
