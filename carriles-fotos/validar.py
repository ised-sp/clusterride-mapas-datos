#!/usr/bin/env python3
"""
Mide cuánto acierta el detector de carriles de las fotos del PNOA: lo pasa por tramos de autopista/autovía de
OpenStreetMap que YA tienen «lanes» y compara. Uso: python3 validar.py spain.osm.pbf N salida.json casos.csv
"""
import collections, concurrent.futures as cf, csv, json, math, random, sys

import osmium

import corredor as C

import os
CLASES = set(os.environ.get("CLASES", "motorway,trunk").split(","))
VENTANA = 240          # filas de 0,25 m = 60 m
LARGO = float(os.environ.get("LARGO", "600"))   # m de cada vía que se miran (en el centro)


class Vias(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.vias = []

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        # Solo calzadas de un sentido (en las de doble sentido el eje va por la línea del centro)
        if hw not in CLASES or t.get("tunnel") or (t.get("oneway") != "yes" and not hw.startswith("motorway")):
            return
        lanes = t.get("lanes", "")
        if not lanes.isdigit() or not 1 <= int(lanes) <= 6 or t.get("lanes:psv") or t.get("lanes:bus"):
            return
        try:
            pts = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if not (-9.5 < pts[0][0] < 4.5 and 35.8 < pts[0][1] < 43.9):   # península y Baleares (el PNOA de Canarias va aparte)
            return
        largo = sum(math.hypot((b[0] - a[0]) * 85000, (b[1] - a[1]) * 110540) for a, b in zip(pts, pts[1:]))
        if largo >= LARGO + 100:
            self.vias.append((w.id, int(lanes), pts, largo))


def recorte(pts, largo, mirar=None):
    """Los LARGO metros centrales de la vía (los puntos de los segmentos que tocan ese tramo)."""
    mirar = mirar or LARGO
    ini, fin = (largo - mirar) / 2, (largo + mirar) / 2
    idx, acc = [], 0.0
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        d = math.hypot((b[0] - a[0]) * 85000, (b[1] - a[1]) * 110540)
        if acc + d >= ini and acc <= fin:
            idx += [i, i + 1]
        acc += d
    return [pts[i] for i in sorted(set(idx))] if idx else pts


def medir(via, mirar=None):
    wid, lanes, pts, largo = via
    try:
        img, off = C.enderezar(recorte(pts, largo, mirar))
    except Exception as e:
        return wid, lanes, None, f"error {e}"
    votos = collections.Counter()
    n = 0
    for k in range(0, img.shape[0] - VENTANA + 1, VENTANA):
        n += 1
        r, _ = C.analizar(img[k:k + VENTANA], off)
        if r:
            votos[r] += 1
    if not votos or sum(votos.values()) < 0.5 * n:
        return wid, lanes, None, dict(votos)
    v, c = votos.most_common(1)[0]
    return wid, lanes, (v if c >= 0.6 * sum(votos.values()) else None), dict(votos)


def main(pbf, n, salida, casos):
    h = Vias()
    h.apply_file(pbf, locations=True, idx="sparse_file_array,/tmp/nodos.idx")
    random.seed(7)
    muestra = random.sample(h.vias, min(int(n), len(h.vias)))
    print(f"{len(h.vias)} vías candidatas; se miran {len(muestra)}", flush=True)
    res = []
    with cf.ThreadPoolExecutor(6) as ex:
        for i, r in enumerate(ex.map(medir, muestra)):
            res.append(r)
            if i % 25 == 0:
                print(i, r[:3], flush=True)
    dio = [r for r in res if r[2]]
    ok = [r for r in dio if r[2] == r[1]]
    conf = collections.Counter(f"OSM {r[1]} / foto {r[2]}" for r in dio)
    out = {"miradas": len(res), "con_respuesta": len(dio), "aciertos": len(ok),
           "acierto": round(100 * len(ok) / max(1, len(dio)), 1),
           "cobertura": round(100 * len(dio) / max(1, len(res)), 1),
           "por_carriles": {k: f"{sum(1 for r in dio if r[1] == k and r[2] == k)}/{sum(1 for r in dio if r[1] == k)}" for k in range(1, 7)},
           "confusion": dict(conf.most_common())}
    json.dump(out, open(salida, "w"), indent=1, ensure_ascii=False)
    with open(casos, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["way_id", "osm", "foto", "votos"])
        for r in res:
            w.writerow([r[0], r[1], r[2] or "", json.dumps(r[3])])
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:5])
