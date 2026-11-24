#!/usr/bin/env python3
"""
Junta los carriles del IGN (espana-carriles.csv.gz: way_id,lanes) con los de las fotos del PNOA
(partes fotos-*.csv: way_id,lanes,highway). En los enlaces manda el IGN (coincide con OSM ≥80 %);
en las calzadas de autopista y autovía, las fotos (98 % de acierto medido).

Uso: python3 unir.py ign.csv.gz salida.csv.gz resumen.json fotos1.csv [fotos2.csv …]
"""
import csv, gzip, io, json, sys


def main(ign, salida, resumen, *fotos):
    res, origen = {}, {}
    with gzip.open(ign, "rt") as f:
        for r in csv.DictReader(f):
            res[r["way_id"]] = r["lanes"]
            origen[r["way_id"]] = "ign"
    puestas = cambiadas = 0
    for p in fotos:
        with open(p) as f:
            for r in csv.DictReader(f):
                w = r["way_id"]
                if w in res and r["highway"].endswith("_link"):
                    continue
                if w in res and res[w] != r["lanes"]:
                    cambiadas += 1
                elif w not in res:
                    puestas += 1
                res[w] = r["lanes"]
                origen[w] = "fotos"
    with gzip.open(salida, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["way_id", "lanes"])
        for k in sorted(res, key=int):
            w.writerow([k, res[k]])
    r = {"vias": len(res), "del_ign": sum(1 for v in origen.values() if v == "ign"),
         "de_las_fotos": sum(1 for v in origen.values() if v == "fotos"),
         "fotos_nuevas": puestas, "fotos_distintas_del_ign": cambiadas,
         "fuentes": "IGN IGR-RT y PNOA © IGN (CC BY 4.0) · © OpenStreetMap"}
    json.dump(r, open(resumen, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(r, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:])
