#!/usr/bin/env python3
"""Cuánta red de España tiene carriles (y límite de velocidad) en OSM y cuánta más con nuestros datos.
Uso: python3 cobertura.py spain.osm.pbf espana-carriles.csv.gz salida.json"""
import collections, csv, gzip, json, math, sys
import osmium

CLASES = ["motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
          "primary_link", "secondary_link", "tertiary_link", "unclassified", "residential"]


class H(osmium.SimpleHandler):
    def __init__(self, nuestros):
        super().__init__()
        self.n = nuestros
        self.km = collections.defaultdict(lambda: collections.Counter())

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in CLASES:
            return
        try:
            p = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if not (-9.5 < p[0][0] < 4.5 and 35.8 < p[0][1] < 43.9):
            return  # solo península y Baleares
        k = sum(math.hypot((b[0] - a[0]) * 85000, (b[1] - a[1]) * 110540) for a, b in zip(p, p[1:])) / 1000
        c = self.km[hw]
        c["total"] += k
        t = w.tags
        if t.get("lanes"):
            c["carriles_osm"] += k
        elif w.id in self.n:
            c["carriles_nuestros"] += k
        if t.get("turn:lanes") or t.get("turn:lanes:forward") or t.get("turn:lanes:backward"):
            c["flechas_osm"] += k
        if t.get("maxspeed") or t.get("maxspeed:forward") or t.get("maxspeed:backward"):
            c["limite_osm"] += k


def main(pbf, csvgz, salida):
    with gzip.open(csvgz, "rt") as f:
        nuestros = {int(r["way_id"]) for r in csv.DictReader(f)}
    h = H(nuestros)
    h.apply_file(pbf, locations=True, idx="sparse_file_array,/tmp/nodos3.idx")
    r = {}
    for hw in CLASES:
        c = h.km[hw]
        tot = c["total"] or 1
        r[hw] = {"km": round(c["total"]),
                 "carriles_osm_%": round(100 * c["carriles_osm"] / tot, 1),
                 "carriles_con_nuestros_%": round(100 * (c["carriles_osm"] + c["carriles_nuestros"]) / tot, 1),
                 "flechas_de_carril_osm_%": round(100 * c["flechas_osm"] / tot, 1),
                 "limite_osm_%": round(100 * c["limite_osm"] / tot, 1)}
    json.dump(r, open(salida, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(r, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:4])
