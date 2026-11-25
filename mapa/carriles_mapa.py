"""Carriles para pintarlos en el mapa de ClusterRide.

Lee el OpenStreetMap (España, Portugal y Andorra) y los carriles del IGN cruzados (espana-carriles.csv.gz: way_id,lanes,
solo para vías de OSM que no los tienen) y escribe un GeoJSON por líneas con las calzadas cuyo número de carriles cambia
el dibujo: sentido único con 2 o más, y doble sentido con 4 o más o con lanes:forward/lanes:backward.

Cada línea va en el sentido de la marcha (las oneway=-1 se dan la vuelta) y lleva:
  n  carriles de la calzada                   c  clase como la de OpenMapTiles (motorway, trunk, primary, secondary,
  b  carriles del sentido contrario, a la        tertiary, minor)
     izquierda (0 en sentido único)           br bridge | tunnel | "" ;  r = 1 si es enlace
No se inventa nada: un doble sentido con un número impar de carriles y sin lanes:forward/backward no se sabe cómo se
reparte y se deja fuera (el mapa lo sigue pintando como siempre).

Uso: python3 carriles_mapa.py <osm.pbf> [espana-carriles.csv.gz] > carriles.geojsonl
"""
import csv, gzip, json, sys
import osmium

CLASE = {"motorway": "motorway", "motorway_link": "motorway", "trunk": "trunk", "trunk_link": "trunk",
         "primary": "primary", "primary_link": "primary", "secondary": "secondary", "secondary_link": "secondary",
         "tertiary": "tertiary", "tertiary_link": "tertiary", "unclassified": "minor", "residential": "minor"}

def entero(v):
    try:
        n = int(str(v).split(";")[0].strip())
        return n if 0 < n < 13 else None
    except Exception:
        return None

ign = {}
if len(sys.argv) > 2:
    for r in csv.reader(gzip.open(sys.argv[2], "rt")):
        if r and r[0].isdigit(): ign[int(r[0])] = entero(r[1])

class H(osmium.SimpleHandler):
    def __init__(s):
        super().__init__(); s.n = 0; s.de_ign = 0
    def way(s, w):
        t = w.tags; hw = t.get("highway")
        c = CLASE.get(hw)
        if not c or t.get("area") == "yes": return
        n = entero(t.get("lanes")) if "lanes" in t else None
        fuente_ign = False
        if n is None and w.id in ign: n = ign[w.id]; fuente_ign = True
        ow = t.get("oneway", "")
        uno = ow in ("yes", "1", "true", "-1") or t.get("junction") in ("roundabout", "circular") or \
              (hw in ("motorway", "motorway_link") and ow not in ("no", "0", "false"))
        rev = ow == "-1"
        if uno:
            if n is None or n < 2: return
            b = 0
        else:
            f, k = entero(t.get("lanes:forward")), entero(t.get("lanes:backward"))
            if f and k and f + k >= 3: n, b = f + k, k
            elif n and n >= 4 and n % 2 == 0: b = n // 2
            else: return
        try:
            pts = [(round(nd.lon, 6), round(nd.lat, 6)) for nd in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2: return
        if rev: pts.reverse()
        br = "bridge" if t.get("bridge") not in (None, "no") else ("tunnel" if t.get("tunnel") not in (None, "no") else "")
        props = {"n": n, "b": b, "c": c, "br": br, "r": 1 if hw.endswith("_link") else 0}
        sys.stdout.write(json.dumps({"type": "Feature", "properties": props,
                                     "geometry": {"type": "LineString", "coordinates": pts}}, separators=(",", ":")) + "\n")
        s.n += 1; s.de_ign += fuente_ign

h = H()
h.apply_file(sys.argv[1], locations=True, idx="flex_mem")
print(f"calzadas: {h.n} (con carriles del IGN: {h.de_ign})", file=sys.stderr)
