"""Señales de velocidad de Mapillary para ClusterRide (26/11/2026).

Baja de las teselas de Mapillary las señales detectadas en sus fotos (R-301 «velocidad máxima» y «fin de velocidad
máxima»), les pone el sentido en que se ven (aligned_direction, de su API) y junta las detecciones de la misma señal.
Sale un fichero compacto que el servidor de rutas (rutas.py) usa para el límite de cada punto de la carretera, por encima
de la DGT y de OpenStreetMap.

Datos de Mapillary, licencia CC BY-SA 4.0 («© Mapillary»). La clave (Client Token) va en la variable MAPILLARY_TOKEN.

Mapillary deja pedir 50.000 teselas al día por aplicación, y solo España tiene cientos de miles de teselas z14 con
fotos: por eso se va por partes y se guarda lo bajado en un estado (estado.json.gz): cada pasada pide como mucho
PRESUPUESTO teselas (primero las que nunca se han pedido y después las más viejas) y la salida se hace con todo lo que
hay en el estado. La cobertura (qué teselas z14 tienen fotos) sale de las teselas z10, que son pocas, y se rehace cada
COBERTURA_DIAS días.

Uso: MAPILLARY_TOKEN=… python3 mapillary_velocidad.py <oeste,sur,este,norte>[;<otro recuadro>…] salida.json.gz estado.json.gz
Cada línea de la salida: [lon, lat, valor (negativo = fin de ese límite), sentido (grados), visto por última vez (día
desde 1970), veces vista].
"""
import gzip, json, math, os, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
import mapbox_vector_tile

T = os.environ["MAPILLARY_TOKEN"].strip()
TILES = "https://tiles.mapillary.com/maps/vtp/{layer}/2/{z}/{x}/{y}?access_token=" + T
WORKERS = int(os.environ.get("HILOS", "8"))
BUDGET = int(os.environ.get("PRESUPUESTO", "40000"))        # teselas de Mapillary por pasada (el tope es 50.000 al día)
COVER_DAYS = int(os.environ.get("COBERTURA_DIAS", "30"))
UA = {"User-Agent": "ClusterRide-senales/1.0 (+https://clusterride.clusterapps.es)"}
STOP = threading.Event()        # se agotó el cupo del día: no se pide nada más
FAILS = [0]


def get(url, tries=4):
    for k in range(tries):
        if STOP.is_set():
            return None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                d = r.read()
                if d[:1] == b"<":          # una página web en vez de la tesela («Update your browser»): no vale
                    raise ValueError("html")
                FAILS[0] = 0
                return d
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return b""
            if e.code == 429 or e.code == 403:
                FAILS[0] += 1
            time.sleep(2 * (k + 1))
        except Exception:
            FAILS[0] += 1
            time.sleep(2 * (k + 1))
        if FAILS[0] > 60:
            print("  demasiados fallos seguidos (¿cupo del día gastado?): se para aquí", file=sys.stderr)
            STOP.set()
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
    """Teselas z14 por donde pasa alguna secuencia de fotos (de las teselas z10 de cobertura, con los tramos rellenos
    cada ~150 m). Devuelve {(x, y): cuántos puntos de secuencia caen} o None si no se pudo."""
    w, s, e, n = box
    x0, y1 = tile_of(w, s, 10); x1, y0 = tile_of(e, n, 10)
    jobs = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    out = {}
    bad = [0]

    def one(xy):
        x, y = xy
        d = get(TILES.format(layer="mly1_public", z=10, x=x, y=y))
        if d is None:
            bad[0] += 1
            return {}
        if not d:
            return {}
        try:
            t = mapbox_vector_tile.decode(d)
        except Exception:
            return {}
        lay = t.get("sequence")
        if not lay:
            return {}
        ext = lay.get("extent", 4096)
        cells = {}
        step = ext / 16 / 2.5           # una tesela z14 es 1/16 de la z10: varios puntos por tesela

        def add(px, py):
            cx = min(15, max(0, int(px / ext * 16))); cy = min(15, max(0, int((ext - py) / ext * 16)))
            k = (x * 16 + cx, y * 16 + cy)
            cells[k] = cells.get(k, 0) + 1

        for f in lay["features"]:
            g = f["geometry"]
            lines = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
            for ln in lines:
                for (ax, ay), (bx, by) in zip(ln, ln[1:]):
                    m = max(1, int(math.hypot(bx - ax, by - ay) / step))
                    for i in range(m):
                        add(ax + (bx - ax) * i / m, ay + (by - ay) * i / m)
                if ln:
                    add(*ln[-1])
        return cells

    kx0, ky1 = tile_of(w, s, 14); kx1, ky0 = tile_of(e, n, 14)
    with ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(one, jobs):
            for k, v in r.items():
                if kx0 <= k[0] <= kx1 and ky0 <= k[1] <= ky1:
                    out[k] = out.get(k, 0) + v
    print(f"  cobertura: {len(jobs)} teselas z10 ({bad[0]} sin respuesta) → {len(out)} teselas z14 con fotos",
          file=sys.stderr)
    return None if bad[0] > len(jobs) // 20 else out


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
    """{(x, y): [[id, lon, lat, valor, visto, primera vez], …]} de las teselas pedidas (las que fallan no salen)."""
    got = {}

    def one(xy):
        x, y = xy
        d = get(TILES.format(layer="mly_map_feature_traffic_sign", z=14, x=x, y=y))
        if d is None:
            return xy, None
        if not d:
            return xy, []
        try:
            t = mapbox_vector_tile.decode(d)
        except Exception:
            return xy, []
        lay = t.get("traffic_sign")
        if not lay:
            return xy, []
        ext = lay.get("extent", 4096)
        res = []
        for f in lay["features"]:
            p = f["properties"]
            v = speed_value(p.get("value"))
            if v is None:
                continue
            px, py = f["geometry"]["coordinates"]
            lon, lat = lonlat(14, x, y, px, py, ext)
            res.append([str(p["id"]), round(lon, 7), round(lat, 7), v, int(p.get("last_seen_at") or 0) // 86400000,
                        int(p.get("first_seen_at") or 0) // 86400000])
        return xy, res

    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (xy, r) in enumerate(ex.map(one, tiles)):
            if r is not None:
                got[xy] = r
            if i and i % 5000 == 0:
                print(f"  {i}/{len(tiles)} teselas · {sum(len(v) for v in got.values())} detecciones · "
                      f"{time.time() - t0:.0f} s", file=sys.stderr)
    return got


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


def load_state(path):
    try:
        with gzip.open(path, "rt") as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    st.setdefault("cov_day", 0); st.setdefault("cov", {}); st.setdefault("tiles", {}); st.setdefault("dirs", {})
    return st


def main():
    boxes = [tuple(float(v) for v in b.split(",")) for b in sys.argv[1].split(";")]
    state_path = sys.argv[3]
    st = load_state(state_path)
    today = int(time.time() // 86400)
    t0 = time.time()
    # 1) cobertura (cada COBERTURA_DIAS días)
    if not st["cov"] or today - st["cov_day"] >= COVER_DAYS:
        cov = {}
        ok = True
        for b in boxes:
            c = covered_z14(b)
            if c is None:
                ok = False
                break
            for k, v in c.items():
                cov[f"{k[0]},{k[1]}"] = cov.get(f"{k[0]},{k[1]}", 0) + v
        if ok:
            st["cov"] = cov; st["cov_day"] = today
        print(f"cobertura {'nueva' if ok else 'fallida, sigue la de antes'}: {len(st['cov'])} teselas z14", file=sys.stderr)
    # 2) qué teselas tocan hoy: las nunca pedidas (las de más fotos primero) y luego las más viejas
    tiles = st["tiles"]
    never = sorted((k for k in st["cov"] if k not in tiles), key=lambda k: -st["cov"][k])
    old = sorted((k for k in st["cov"] if k in tiles), key=lambda k: tiles[k][0])
    todo = (never + old)[:max(0, BUDGET)]
    print(f"teselas: {len(st['cov'])} con fotos · {len(never)} sin pedir nunca · hoy {len(todo)}", file=sys.stderr)
    got = signs_in([tuple(int(v) for v in k.split(",")) for k in todo])
    for (x, y), r in got.items():
        tiles[f"{x},{y}"] = [today, r]
    for k in [k for k in tiles if k not in st["cov"]]:
        del tiles[k]                 # ya no tiene fotos
    print(f"pedidas bien: {len(got)} de {len(todo)} · {time.time() - t0:.0f} s", file=sys.stderr)
    # 3) sentido de las detecciones nuevas (API de entidades, aparte del cupo de teselas)
    raw = {d[0]: d for (_, r) in tiles.values() for d in r}
    STOP.clear(); FAILS[0] = 0
    need = [k for k in raw if k not in st["dirs"]]
    if need:
        dirs = directions(need)
        for k, v in dirs.items():
            if isinstance(v, (int, float)):
                st["dirs"][k] = round(v)
    for k in [k for k in st["dirs"] if k not in raw]:
        del st["dirs"][k]
    with gzip.open(state_path + ".tmp", "wt") as fh:
        json.dump(st, fh, separators=(",", ":"))
    os.replace(state_path + ".tmp", state_path)
    # 4) salida con todo lo que hay
    signs = [dict(id=k, lon=d[1], lat=d[2], v=d[3], seen=d[4], first=d[5], dir=st["dirs"].get(k)) for k, d in raw.items()]
    groups = cluster(signs)
    with gzip.open(sys.argv[2], "wt") as fh:
        for g in groups:
            fh.write(json.dumps([round(g["lon"], 6), round(g["lat"], 6), g["v"],
                                 None if g["dir"] is None else round(g["dir"]), g["seen"], g["n"]],
                                separators=(",", ":")) + "\n")
    done = sum(1 for k in st["cov"] if k in tiles)
    print(f"señales: {len(groups)} (de {len(signs)} detecciones; {sum(1 for s in signs if s['dir'] is None)} sin sentido)"
          f" · teselas hechas {done}/{len(st['cov'])} · {time.time() - t0:.0f} s", file=sys.stderr)
    with open(os.environ.get("GITHUB_ENV", "/dev/null"), "a") as fh:
        fh.write(f"N={len(groups)}\nHECHAS={done}\nTOTAL={len(st['cov'])}\n")


if __name__ == "__main__":
    main()
