"""Señales de velocidad de Mapillary para ClusterRide (26/11/2026).

Baja de las teselas de Mapillary las señales detectadas en sus fotos (R-301 «velocidad máxima» y «fin de velocidad
máxima»), les pone el sentido en que se ven (aligned_direction, de su API) y junta las detecciones de la misma señal.
Sale un fichero compacto que el servidor de rutas (rutas.py) usa para el límite de cada punto de la carretera, por encima
de la DGT y de OpenStreetMap.

Datos de Mapillary, licencia CC BY-SA 4.0 («© Mapillary»). La clave (Client Token) va en la variable MAPILLARY_TOKEN.

Uso: MAPILLARY_TOKEN=… python3 mapillary_velocidad.py <oeste,sur,este,norte>[;<otro recuadro>…] salida.json.gz
Cada línea de la salida: [lon, lat, valor (negativo = fin de ese límite), sentido (grados), visto por última vez (día
desde 1970), veces vista].
"""
import gzip, json, math, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import mapbox_vector_tile

T = os.environ["MAPILLARY_TOKEN"].strip()
TILES = "https://tiles.mapillary.com/maps/vtp/{layer}/2/{z}/{x}/{y}?access_token=" + T
WORKERS = int(os.environ.get("HILOS", "24"))


def get(url, tries=5):
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return b""
            time.sleep(1.5 * (k + 1) ** 2)
        except Exception:
            time.sleep(1.5 * (k + 1) ** 2)
    return None


def tile_of(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def lonlat(z, x, y, px, py, extent=4096):
    n = 2 ** z
    gx = (x + px / extent) / n
    gy = (y + 1 - py / extent) / n          # las teselas vienen con el eje y hacia arriba
    lon = gx * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy))))
    return lon, lat


def covered_z14(box):
    """Teselas z14 por donde pasa alguna secuencia de fotos (mirando las teselas z12 de cobertura)."""
    w, s, e, n = box
    x0, y1 = tile_of(w, s, 12); x1, y0 = tile_of(e, n, 12)
    jobs = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    out = set()

    def one(xy):
        x, y = xy
        d = get(TILES.format(layer="mly1_public", z=12, x=x, y=y))
        if not d:
            return []
        try:
            t = mapbox_vector_tile.decode(d)
        except Exception:
            return []
        lay = t.get("sequence")
        if not lay:
            return []
        ext = lay.get("extent", 4096)
        cells = set()
        for f in lay["features"]:
            g = f["geometry"]
            lines = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
            for ln in lines:
                for px, py in ln:
                    cx = min(3, max(0, int(px / ext * 4)))
                    cy = min(3, max(0, int((ext - py) / ext * 4)))
                    cells.add((x * 4 + cx, y * 4 + cy))
        return list(cells)

    with ThreadPoolExecutor(WORKERS) as ex:
        for i, r in enumerate(ex.map(one, jobs)):
            out.update(r)
    print(f"  cobertura: {len(jobs)} teselas z12 → {len(out)} teselas z14 con fotos", file=sys.stderr)
    return sorted(out)


def speed_value(v):
    # regulatory--maximum-speed-limit-70--g1 / regulatory--end-of-maximum-speed-limit-70--g1
    if not v or "maximum-speed-limit-" not in v or "advisory" in v:
        return None
    try:
        num = int(v.split("maximum-speed-limit-")[1].split("--")[0])
    except Exception:
        return None
    if not 5 <= num <= 130 or num % 10 not in (0,) and num not in (5, 15, 25):
        return None
    return -num if "end-of-" in v else num


def signs_in(tiles):
    found = {}

    def one(xy):
        x, y = xy
        d = get(TILES.format(layer="mly_map_feature_traffic_sign", z=14, x=x, y=y))
        if not d:
            return []
        try:
            t = mapbox_vector_tile.decode(d)
        except Exception:
            return []
        lay = t.get("traffic_sign")
        if not lay:
            return []
        ext = lay.get("extent", 4096)
        res = []
        for f in lay["features"]:
            p = f["properties"]
            v = speed_value(p.get("value"))
            if v is None:
                continue
            px, py = f["geometry"]["coordinates"]
            lon, lat = lonlat(14, x, y, px, py, ext)
            res.append((str(p["id"]), lon, lat, v, int(p.get("last_seen_at") or 0) // 86400000,
                        int(p.get("first_seen_at") or 0) // 86400000))
        return res

    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, r in enumerate(ex.map(one, tiles)):
            for s in r:
                found[s[0]] = s
            if i and i % 5000 == 0:
                print(f"  {i}/{len(tiles)} teselas · {len(found)} señales · {time.time() - t0:.0f} s", file=sys.stderr)
    return found


def directions(ids):
    out = {}
    chunks = [ids[i:i + 50] for i in range(0, len(ids), 50)]

    def one(ch):
        d = get("https://graph.mapillary.com/?ids=" + ",".join(ch) + "&fields=id,aligned_direction&access_token=" + T)
        try:
            j = json.loads(d or b"{}")
        except Exception:
            return {}
        return {k: v.get("aligned_direction") for k, v in j.items() if isinstance(v, dict)}

    with ThreadPoolExecutor(min(WORKERS, 12)) as ex:
        for r in ex.map(one, chunks):
            out.update(r)
    return out


def angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def cluster(signs):
    """Junta las detecciones de la misma señal (mismo valor, a menos de 20 m, mismo sentido) y quita las sustituidas."""
    kx = 111320 * math.cos(math.radians(40))
    grid = {}
    for s in signs:
        grid.setdefault((int(s["lon"] * kx // 30), int(s["lat"] * 110574 // 30)), []).append(s)
    used = set()
    groups = []
    for s in sorted(signs, key=lambda s: -s["seen"]):
        if s["id"] in used:
            continue
        gx, gy = int(s["lon"] * kx // 30), int(s["lat"] * 110574 // 30)
        g = [s]
        used.add(s["id"])
        for i in (gx - 1, gx, gx + 1):
            for j in (gy - 1, gy, gy + 1):
                for o in grid.get((i, j), []):
                    if o["id"] in used or o["v"] != s["v"]:
                        continue
                    if math.hypot((o["lon"] - s["lon"]) * kx, (o["lat"] - s["lat"]) * 110574) > 20:
                        continue
                    if s["dir"] is not None and o["dir"] is not None and angdiff(s["dir"], o["dir"]) > 45:
                        continue
                    g.append(o)
                    used.add(o["id"])
        dirs = [x["dir"] for x in g if x["dir"] is not None]
        if dirs:
            cx = sum(math.cos(math.radians(d)) for d in dirs); cy = sum(math.sin(math.radians(d)) for d in dirs)
            dmean = math.degrees(math.atan2(cy, cx)) % 360
        else:
            dmean = None
        groups.append(dict(lon=sum(x["lon"] for x in g) / len(g), lat=sum(x["lat"] for x in g) / len(g), v=s["v"],
                           dir=dmean, seen=max(x["seen"] for x in g), first=min(x["first"] for x in g), n=len(g)))
    # Una señal sustituida: otra de distinto valor en el mismo sitio y sentido, vista después y la vieja ya no
    keep = []
    g2 = {}
    for g in groups:
        g2.setdefault((int(g["lon"] * kx // 40), int(g["lat"] * 110574 // 40)), []).append(g)
    for g in groups:
        gx, gy = int(g["lon"] * kx // 40), int(g["lat"] * 110574 // 40)
        old = False
        for i in (gx - 1, gx, gx + 1):
            for j in (gy - 1, gy, gy + 1):
                for o in g2.get((i, j), []):
                    if o is g or o["v"] == g["v"] or (o["v"] < 0) != (g["v"] < 0):
                        continue
                    if math.hypot((o["lon"] - g["lon"]) * kx, (o["lat"] - g["lat"]) * 110574) > 30:
                        continue
                    if g["dir"] is not None and o["dir"] is not None and angdiff(g["dir"], o["dir"]) > 45:
                        continue
                    if o["first"] > g["seen"] + 30:      # la otra apareció cuando esta ya no se veía
                        old = True
        if not old:
            keep.append(g)
    return keep


def main():
    boxes = [tuple(float(v) for v in b.split(",")) for b in sys.argv[1].split(";")]
    t0 = time.time()
    raw = {}
    for b in boxes:
        tiles = covered_z14(b)
        raw.update(signs_in(tiles))
        print(f"recuadro {b}: {len(raw)} detecciones · {time.time() - t0:.0f} s", file=sys.stderr)
    dirs = directions(list(raw))
    signs = [dict(id=k, lon=s[1], lat=s[2], v=s[3], seen=s[4], first=s[5],
                  dir=(dirs.get(k) if isinstance(dirs.get(k), (int, float)) else None)) for k, s in raw.items()]
    groups = cluster(signs)
    with gzip.open(sys.argv[2], "wt") as fh:
        for g in groups:
            fh.write(json.dumps([round(g["lon"], 6), round(g["lat"], 6), g["v"],
                                 None if g["dir"] is None else round(g["dir"]), g["seen"], g["n"]],
                                separators=(",", ":")) + "\n")
    print(f"señales: {len(groups)} (de {len(signs)} detecciones; {sum(1 for s in signs if s['dir'] is None)} sin sentido)"
          f" · {time.time() - t0:.0f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
