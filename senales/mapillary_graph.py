"""Señales y marcas viales de Mapillary por su API de entidades (26/11/2026).

La API de teselas de Mapillary deja 50.000 al día; la de entidades (graph.mapillary.com/map_features) tiene su propio
límite, mucho más alto. Se pregunta tesela z14 a tesela z14 de la cobertura del estado (las teselas con fotos), solo por
lo que usa ClusterRide, con su sentido (aligned_direction), y se guarda en el estado (clave «g»), para seguir donde se
quedó si la pasada se corta.

Salidas:
  senales-velocidad.json.gz  [lon, lat, valor (negativo = fin), sentido, último día visto, veces]  (como mapillary_velocidad.py)
  senales-ruta.json.gz       [lon, lat, tipo, sentido, último día visto, veces]  (peligros, adelantar, cruces, flechas)

Uso: MAPILLARY_TOKEN=… python3 mapillary_graph.py estado.json.gz senales-velocidad.json.gz senales-ruta.json.gz
Datos de Mapillary, CC BY-SA 4.0 («© Mapillary»).
"""
import gzip, json, math, os, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapillary_velocidad as M

T = os.environ["MAPILLARY_TOKEN"].strip()
WORKERS = int(os.environ.get("HILOS", "96"))
MAX_SECONDS = float(os.environ.get("TIEMPO_MAX", str(5 * 3600)))
G = (1, 2, 3, 4, 5, 6)

# Tipos de ClusterRide → valores de Mapillary (sin el «--gN» del final).
TYPES = {
    # peligros
    "curva-izq": ["warning--curve-left"], "curva-der": ["warning--curve-right"],
    "curvas-izq": ["warning--double-curve-first-left", "warning--winding-road-first-left"],
    "curvas-der": ["warning--double-curve-first-right", "warning--winding-road-first-right"],
    "subida": ["warning--steep-ascent"], "bajada": ["warning--steep-descent", "warning--descent"],
    "deslizante": ["warning--slippery-road-surface", "warning--slippery-motorcycles"],
    "hielo": ["warning--icy-road", "warning--snow"],
    "desprendimientos": ["warning--falling-rocks-or-debris-right", "warning--falling-rocks-or-debris-left"],
    "animales": ["warning--domestic-animals", "warning--wild-animals", "warning--horse-riders"],
    "estrechamiento": ["warning--road-narrows", "warning--road-narrows-left", "warning--road-narrows-right",
                       "warning--narrow-bridge"],
    "obras": ["warning--roadworks"],
    "firme": ["warning--uneven-road", "warning--dip", "warning--road-bump", "warning--loose-road-surface",
              "warning--uneven-roadway"],
    "peligro": ["warning--other-danger"],
    "cruce": ["warning--crossroads", "warning--junction-with-a-side-road-perpendicular-left",
              "warning--junction-with-a-side-road-perpendicular-right"],
    "rotonda-aviso": ["warning--roundabout"],
    "semaforo-aviso": ["warning--traffic-signals"],
    "peatones": ["warning--pedestrians-crossing", "warning--children"],
    "ciclistas": ["warning--bicycles-crossing"],
    "doble-sentido": ["warning--two-way-traffic"],
    # adelantar
    "no-adelantar": ["regulatory--no-overtaking"],
    "fin-no-adelantar": ["regulatory--end-of-no-overtaking"],
    # cruces
    "stop": ["regulatory--stop"], "ceda": ["regulatory--yield"],
}
POINTS = {   # sin «--gN»
    "semaforo": ["object--traffic-light--general-upright-front", "object--traffic-light--general-upright",
                 "object--traffic-light--general-single-front", "object--traffic-light--general-horizontal-front"],
    "paso": ["marking--discrete--crosswalk-zebra"],
    "linea-stop": ["marking--discrete--stop-line", "marking--discrete--text--stop"],
    "linea-ceda": ["marking--discrete--give-way-single", "marking--discrete--give-way-row"],
    "flecha-recto": ["marking--discrete--arrow--straight"],
    "flecha-izq": ["marking--discrete--arrow--left"],
    "flecha-der": ["marking--discrete--arrow--right"],
    "flecha-recto-izq": ["marking--discrete--arrow--split-left-or-straight"],
    "flecha-recto-der": ["marking--discrete--arrow--split-right-or-straight"],
    "flecha-izq-der": ["marking--discrete--arrow--split-left-or-right"],
}
VALUE_TYPE = {}
for t, vs in TYPES.items():
    for v in vs:
        for g in G:
            VALUE_TYPE[f"{v}--g{g}"] = t
for t, vs in POINTS.items():
    for v in vs:
        VALUE_TYPE[v] = t
SPEED = [f"regulatory--{e}maximum-speed-limit-{v}--g{g}" for e in ("", "end-of-")
         for v in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120) for g in (1, 2, 3)]
# Tres consultas por tesela (la dirección no puede ser muy larga)
GROUPS = [SPEED, [v for v in VALUE_TYPE if v.startswith(("warning", "regulatory"))],
          [v for v in VALUE_TYPE if not v.startswith(("warning", "regulatory"))]]
FIELDS = "id,object_value,geometry,aligned_direction,last_seen_at,first_seen_at"


def day(v):
    """Día desde 1970 de una fecha de la API (texto ISO o milisegundos)."""
    if isinstance(v, (int, float)):
        return int(v) // 86400000
    if isinstance(v, str) and len(v) >= 10:
        try:
            import datetime
            return (datetime.date.fromisoformat(v[:10]) - datetime.date(1970, 1, 1)).days
        except Exception:
            return 0
    return 0


def tile_box(x, y, z=14):
    n = 2 ** z
    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def ask(vals, w, s, e, n, depth=0):
    """Lo de esos valores en el recuadro; si la API no puede con él, se parte en cuatro. None si no se pudo."""
    url = (f"https://graph.mapillary.com/map_features?bbox={w:.6f},{s:.6f},{e:.6f},{n:.6f}&object_values={','.join(vals)}"
           f"&fields={FIELDS}&limit=2000&access_token={T}")
    split = False
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=M.UA), timeout=120) as r:
                data = json.load(r).get("data", [])
            if len(data) >= 2000 and depth < 3:
                split = True
                break
            return data
        except urllib.error.HTTPError as ex:
            if ex.code == 500 and depth < 3:
                split = True
                break
            time.sleep(4 * (k + 1))
        except Exception:
            time.sleep(4 * (k + 1))
    if not split:
        return None
    mx, my = (w + e) / 2, (s + n) / 2
    out = []
    for b in ((w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)):
        r = ask(vals, *b, depth=depth + 1)
        if r is None:
            return None
        out += r
    return out


def cluster_points(rows):
    """Junta las detecciones del mismo tipo a menos de 15 m (y mismo sentido si lo tienen)."""
    kx = 111320 * math.cos(math.radians(40))
    grid = {}
    for r in rows:
        grid.setdefault((r["t"], int(r["lon"] * kx // 20), int(r["lat"] * 110574 // 20)), []).append(r)
    used, out = set(), []
    for r in sorted(rows, key=lambda r: -r["seen"]):
        if r["id"] in used:
            continue
        gx, gy = int(r["lon"] * kx // 20), int(r["lat"] * 110574 // 20)
        g = [r]; used.add(r["id"])
        for i in (gx - 1, gx, gx + 1):
            for j in (gy - 1, gy, gy + 1):
                for o in grid.get((r["t"], i, j), []):
                    if o["id"] in used:
                        continue
                    if math.hypot((o["lon"] - r["lon"]) * kx, (o["lat"] - r["lat"]) * 110574) > 15:
                        continue
                    if r["dir"] is not None and o["dir"] is not None and M.angdiff(r["dir"], o["dir"]) > 45:
                        continue
                    g.append(o); used.add(o["id"])
        dirs = [x["dir"] for x in g if x["dir"] is not None]
        if dirs:
            cx = sum(math.cos(math.radians(d)) for d in dirs); cy = sum(math.sin(math.radians(d)) for d in dirs)
            dm = round(math.degrees(math.atan2(cy, cx)) % 360)
        else:
            dm = None
        out.append([round(sum(x["lon"] for x in g) / len(g), 6), round(sum(x["lat"] for x in g) / len(g), 6), r["t"], dm,
                    max(x["seen"] for x in g), len(g)])
    return out


def main():
    state, out_speed, out_route = sys.argv[1], sys.argv[2], sys.argv[3]
    st = M.load_state(state)
    st.setdefault("g", {})
    today = int(time.time() // 86400)
    keys = [k for k in st["cov"] if k not in st["g"]]
    keys.sort(key=lambda k: -st["cov"][k])
    print(f"teselas: {len(st['cov'])} · hechas {len(st['g'])} · faltan {len(keys)}", file=sys.stderr, flush=True)
    t0 = time.time(); bad = [0]
    lock = threading.Lock()

    def one(k):
        if time.time() - t0 > MAX_SECONDS:
            return k, None
        x, y = (int(v) for v in k.split(","))
        box = tile_box(x, y)
        res = []
        for vals in GROUPS:
            r = ask(vals, *box)
            if r is None:
                return k, None
            res += r
        return k, res

    def save():
        with gzip.open(state + ".tmp", "wt") as fh:
            json.dump(st, fh, separators=(",", ":"))
        os.replace(state + ".tmp", state)

    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (k, r) in enumerate(ex.map(one, keys)):
            if r is None:
                bad[0] += 1
                continue
            det = []
            for f in r:
                ov = f.get("object_value") or ""
                g = f.get("geometry") or {}
                if g.get("type") != "Point":
                    continue
                v = M.speed_value(ov)
                t = v if v is not None else VALUE_TYPE.get(ov)
                if t is None:
                    continue
                lon, lat = g["coordinates"]
                ad = f.get("aligned_direction")
                det.append([str(f["id"]), round(lon, 7), round(lat, 7), t, day(f.get("last_seen_at")), day(f.get("first_seen_at")),
                            round(ad) if isinstance(ad, (int, float)) else None])
            st["g"][k] = [today, det]
            if i and i % 2000 == 0:
                save()
                print(f"  {i}/{len(keys)} · fallidas {bad[0]} · {time.time() - t0:.0f} s", file=sys.stderr, flush=True)
    save()
    # Velocidad: lo de la API de entidades y, en las teselas que aún no tiene, lo de las teselas de Mapillary
    raw = {}
    for k, (_, r) in st["tiles"].items():
        if k not in st["g"]:
            for d in r:
                raw[d[0]] = d[:6] + [st["dirs"].get(d[0])]
    other = []
    for _, r in st["g"].values():
        for d in r:
            if isinstance(d[3], int):
                raw[d[0]] = d
            else:
                other.append(dict(id=d[0], lon=d[1], lat=d[2], t=d[3], seen=d[4], dir=d[6]))
    signs = [dict(id=k, lon=d[1], lat=d[2], v=d[3], seen=d[4], first=d[5], dir=d[6]) for k, d in raw.items()]
    groups = M.cluster(signs)
    with gzip.open(out_speed, "wt") as fh:
        for g in groups:
            fh.write(json.dumps([round(g["lon"], 6), round(g["lat"], 6), g["v"],
                                 None if g["dir"] is None else round(g["dir"]), g["seen"], g["n"]],
                                separators=(",", ":")) + "\n")
    pts = cluster_points(other)
    with gzip.open(out_route, "wt") as fh:
        for p in pts:
            fh.write(json.dumps(p, separators=(",", ":"), ensure_ascii=False) + "\n")
    from collections import Counter
    c = Counter(p[2] for p in pts)
    hechas = sum(1 for k in st["cov"] if k in st["g"])
    with open(os.environ.get("GITHUB_ENV", "/dev/null"), "a") as fh:
        fh.write(f"N={len(groups)}\nNR={len(pts)}\nHECHAS={hechas}\nTOTAL={len(st['cov'])}\n")
    print(f"velocidad: {len(groups)} · ruta: {len(pts)} {dict(c.most_common())} · teselas {hechas}/{len(st['cov'])} · "
          f"fallidas {bad[0]} · {time.time() - t0:.0f} s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
