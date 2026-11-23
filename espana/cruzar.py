#!/usr/bin/env python3
"""
Cruza los carriles del IGN con las vías de OpenStreetMap de España y saca qué vías de OSM no tienen «lanes» y cuántos
tienen según el IGN. Nunca se cambia un dato que ya esté en OpenStreetMap: solo se rellenan huecos.

Cómo casa: cada vía de OSM (autopistas, autovías, nacionales, autonómicas, secundarias y sus enlaces) se recorre con
un punto cada 15 m; cada punto busca el tramo del IGN más cercano a menos de 8 m y con el mismo rumbo (±25°; en vías de
doble sentido también el contrario). La vía recibe el valor si al menos el 70 % de sus puntos casan y el 90 % de los
que casan dicen el mismo número de carriles.

Uso: python3 cruzar.py ign.jsonl.gz spain.osm.pbf salida.csv resumen.json
Salida CSV: way_id,lanes (lo que el servidor de rutas pone en su copia de OpenStreetMap antes de construir la red).
"""
import collections, csv, gzip, json, math, sys

import osmium

CLASES = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
          "secondary", "secondary_link", "tertiary", "tertiary_link"}
CELDA = 0.002          # ~200 m
PASO = 15.0            # m entre puntos de la vía
DIST = 8.0             # m
RUMBO = 25.0           # grados
MIN_CASAN = 0.7
MIN_ACUERDO = 0.9
MIN_PUNTOS = 3


def xy(lon, lat, lat0):
    return lon * 111320.0 * math.cos(math.radians(lat0)), lat * 110540.0


def rumbo(ax, ay, bx, by):
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360


def dif(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def cargar_ign(ruta):
    segs = []       # (ax, ay, bx, by, rumbo, lanes)  en metros con lat0 = 40
    rejilla = collections.defaultdict(list)
    n = 0
    with gzip.open(ruta, "rt", encoding="utf-8") as f:
        for linea in f:
            t = json.loads(linea)
            c = t["c"]
            n += 1
            for (lo1, la1), (lo2, la2) in zip(c, c[1:]):
                ax, ay = xy(lo1, la1, 40.0)
                bx, by = xy(lo2, la2, 40.0)
                if ax == bx and ay == by:
                    continue
                i = len(segs)
                segs.append((ax, ay, bx, by, rumbo(ax, ay, bx, by), t["lanes"]))
                for gx in range(int(min(lo1, lo2) // CELDA), int(max(lo1, lo2) // CELDA) + 1):
                    for gy in range(int(min(la1, la2) // CELDA), int(max(la1, la2) // CELDA) + 1):
                        rejilla[(gx, gy)].append(i)
    print(f"IGN: {n} tramos, {len(segs)} segmentos", flush=True)
    return segs, rejilla


def cercano(segs, rejilla, lon, lat, rumbo_via, dos_sentidos):
    px, py = xy(lon, lat, 40.0)
    gx, gy = int(lon // CELDA), int(lat // CELDA)
    mejor = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for i in rejilla.get((gx + dx, gy + dy), ()):
                ax, ay, bx, by, r, lanes = segs[i]
                d = dif(r, rumbo_via)
                if d > RUMBO and not (dos_sentidos and d > 180 - RUMBO):
                    continue
                vx, vy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
                dist = math.hypot(ax + t * vx - px, ay + t * vy - py)
                if dist <= DIST and (mejor is None or dist < mejor[0]):
                    mejor = (dist, lanes)
    return mejor


class Vias(osmium.SimpleHandler):
    def __init__(self, segs, rejilla):
        super().__init__()
        self.segs, self.rejilla = segs, rejilla
        self.res = {}
        self.est = collections.Counter()
        self.desacuerdo = collections.Counter()

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in CLASES:
            return
        self.est["vias"] += 1
        try:
            pts = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2:
            return
        # Lo normal es que cerca no haya nada del IGN: se descarta sin recorrerla.
        if not any((int(lo // CELDA) + dx, int(la // CELDA) + dy) in self.rejilla for lo, la in pts for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            return
        dos = w.tags.get("oneway") not in ("yes", "1", "-1") and not hw.startswith("motorway") and w.tags.get("junction") != "roundabout"
        votos = collections.Counter()
        total = 0
        for (lo1, la1), (lo2, la2) in zip(pts, pts[1:]):
            ax, ay = xy(lo1, la1, 40.0)
            bx, by = xy(lo2, la2, 40.0)
            largo = math.hypot(bx - ax, by - ay)
            if largo == 0:
                continue
            r = rumbo(ax, ay, bx, by)
            k = max(1, int(largo // PASO))
            for j in range(k):
                t = (j + 0.5) / k
                total += 1
                m = cercano(self.segs, self.rejilla, lo1 + (lo2 - lo1) * t, la1 + (la2 - la1) * t, r, dos)
                if m:
                    votos[m[1]] += 1
        casan = sum(votos.values())
        if total < MIN_PUNTOS or casan < MIN_CASAN * total:
            return
        lanes, n = votos.most_common(1)[0]
        if n < MIN_ACUERDO * casan:
            self.est["dudosas"] += 1
            return
        osm = w.tags.get("lanes")
        if osm:
            self.est["ya_tenian"] += 1
            if osm.strip() != str(lanes):
                self.desacuerdo[f"{hw}: OSM {osm} / IGN {lanes}"] += 1
            return
        self.res[w.id] = lanes
        self.est[f"nuevas_{hw}"] += 1


def main(ign, pbf, salida, resumen):
    segs, rejilla = cargar_ign(ign)
    h = Vias(segs, rejilla)
    h.apply_file(pbf, locations=True, idx="sparse_file_array,/tmp/nodos.idx")
    with open(salida, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["way_id", "lanes"])
        for wid, lanes in sorted(h.res.items()):
            w.writerow([wid, lanes])
    r = {"vias_miradas": h.est["vias"], "nuevas": len(h.res), "ya_tenian_lanes": h.est["ya_tenian"],
         "dudosas": h.est["dudosas"], "por_clase": {k[7:]: v for k, v in h.est.items() if k.startswith("nuevas_")},
         "desacuerdos_con_osm": dict(h.desacuerdo.most_common(30)),
         "fuente": "IGN · IGR Redes de Transporte (CC BY 4.0) · © OpenStreetMap"}
    json.dump(r, open(resumen, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
