#!/usr/bin/env python3
"""
ClusterRide · el motor de rutas de Europa por zonas.

Valhalla no cabe entero en el Plesk, así que Europa va en zonas (zonas.sh) que se solapan en las fronteras, cada una en
su contenedor. Este programa escucha donde escuchaba Valhalla (127.0.0.1:17891, el /navegacion/ del Plesk) y manda cada
petición a la primera zona cuyo recuadro contiene todos sus puntos y que la resuelve; si una zona no la resuelve (no hay
carretera cerca, no hay camino), prueba la siguiente. /height y lo demás sin puntos van a la principal (la que tiene las
alturas). Solo biblioteca estándar de Python.
"""
import bisect, json, math, os, re, threading, time, urllib.error, urllib.parse, urllib.request
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# «principal=http://127.0.0.1:17897|-31.5,27.4,19.0,55.2;noroeste=http://127.0.0.1:17893|-11,48,9.5,61;…» en orden.
ZONES = []
for item in os.environ.get("ZONES", "").split(";"):
    if "=" not in item:
        continue
    name, rest = item.split("=", 1)
    url, _, box = rest.partition("|")
    # Una zona puede tener varios recuadros separados por «/» (p. ej. las islas).
    b = [tuple(float(x) for x in bx.split(",")) for bx in box.split("/")] if box else [(-180, -90, 180, 90)]
    ZONES.append((name.strip(), url.strip().rstrip("/"), b))
TIMEOUT = float(os.environ.get("TIMEOUT", "60"))
PORT = int(os.environ.get("PORT", "17891"))
POINTS_PATHS = ("/route", "/optimized_route", "/trace_route", "/trace_attributes", "/sources_to_targets", "/isochrone", "/locate", "/expansion")


def decode6(s):
    """Polilínea codificada con precisión 6 (la de Valhalla) → [(lat, lon)]."""
    out, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63; i += 1
                result |= (b & 0x1F) << shift; shift += 5
                if b < 0x20: break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0: lat += d
            else: lon += d
        out.append((lat / 1e6, lon / 1e6))
    return out


def points(body):
    pts = []
    for k in ("locations", "sources", "targets", "shape"):
        for p in body.get(k) or []:
            if isinstance(p, dict) and "lat" in p and "lon" in p:
                pts.append((float(p["lat"]), float(p["lon"])))
    if isinstance(body.get("encoded_polyline"), str):
        pts += decode6(body["encoded_polyline"])
    return pts


def inside(boxes, pts):
    if boxes and isinstance(boxes[0], (int, float)):
        boxes = [boxes]
    return any(all(w <= lon <= e and s <= lat <= n for lat, lon in pts) for w, s, e, n in boxes)


def forward(base, path, query, data, ctype):
    url = f"{base}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.headers.get("Content-Type", "application/json"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e.read()
    except Exception as e:
        return 502, "application/json", json.dumps({"error": f"zona sin respuesta: {e}"}).encode()


# ---- Rutas entre zonas -------------------------------------------------------------------------------------------
# Si ninguna zona tiene los dos extremos de un tramo, el tramo se parte en una ciudad de paso que está en las dos zonas
# (las zonas se solapan ahí) y se pide cada trozo a su zona; luego se cosen en un solo tramo, como si lo hubiera
# calculado una sola zona (una sola llegada, indicaciones seguidas). Puede hacer falta pasar por dos zonas (noroeste →
# norte → este).
GATES = {
    frozenset(("principal", "este")): [(48.137, 11.575), (48.208, 16.373), (51.050, 13.738), (52.520, 13.405), (45.650, 13.777), (45.438, 12.327), (47.809, 13.055)],
    frozenset(("principal", "noroeste")): [(48.857, 2.352), (50.629, 3.057), (48.573, 7.752), (50.938, 6.960), (49.233, 6.996), (49.611, 6.130)],
    frozenset(("principal", "norte")): [(50.110, 8.682), (53.551, 9.993), (52.520, 13.405), (48.137, 11.575), (52.375, 9.732)],
    frozenset(("noroeste", "norte")): [(50.938, 6.960), (51.514, 7.465), (51.227, 6.773), (53.219, 6.567)],
    frozenset(("norte", "este")): [(52.520, 13.405), (51.050, 13.738), (48.137, 11.575), (54.352, 18.646)],
    frozenset(("este", "sureste")): [(44.426, 26.102), (46.770, 23.590), (47.158, 27.587), (45.760, 21.226)],
}
ZBASE = {z[0]: z[1] for z in ZONES}


def has_road(zone, lat, lon):
    """¿Tiene esa zona carretera cerca del punto? (/locate de Valhalla)."""
    body = json.dumps({"locations": [{"lat": lat, "lon": lon}], "costing": "motorcycle", "verbose": False}).encode()
    code, _, out = forward(ZBASE[zone], "/locate", "", body, "application/json")
    if code != 200:
        return False
    try:
        r = json.loads(out)
        return bool(r and (r[0].get("edges") or r[0].get("nodes")))
    except Exception:
        return False


def zones_for(lat, lon):
    """Las zonas que tienen carretera en ese punto (primero las que lo tienen en su recuadro)."""
    boxed = [z[0] for z in ZONES if inside(z[2], [(lat, lon)])]
    rest = [z[0] for z in ZONES if z[0] not in boxed]
    return [z for z in boxed + rest if has_road(z, lat, lon)]


def dist(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    return 6371 * 2 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))


def zone_path(za, zb):
    """El camino más corto de zonas (por las que se solapan) de za a zb."""
    prev, todo = {za: None}, [za]
    while todo:
        z = todo.pop(0)
        if z == zb:
            break
        for pair in GATES:
            if z in pair:
                (o,) = pair - {z}
                if o in ZBASE and o not in prev:
                    prev[o] = z; todo.append(o)
    if zb not in prev:
        return None
    out, z = [], zb
    while z:
        out.append(z); z = prev[z]
    return out[::-1]


def encode6(pts):
    out, plat, plon = [], 0, 0
    for lat, lon in pts:
        for v, pv in ((round(lat * 1e6), plat), (round(lon * 1e6), plon)):
            d = v - pv
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63)); d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * 1e6), round(lon * 1e6)
    return "".join(out)


def sew(a, b):
    """Une dos tramos (legs) de Valhalla seguidos en uno: forma, indicaciones y resumen."""
    pa, pb = decode6(a["shape"]), decode6(b["shape"])
    off = len(pa) - 1
    ma, mb = list(a.get("maneuvers") or []), list(b.get("maneuvers") or [])
    if ma and ma[-1].get("type") in (4, 5, 6):
        ma.pop()                                              # la «llegada» a la ciudad de paso
    if mb and ma:
        first = mb.pop(0)                                     # la «salida» desde la ciudad de paso
        last = dict(ma[-1])
        last["end_shape_index"] = off + first.get("end_shape_index", 0)
        last["length"] = round(last.get("length", 0) + first.get("length", 0), 3)
        last["time"] = round(last.get("time", 0) + first.get("time", 0), 3)
        ma[-1] = last
    for m in mb:
        m = dict(m)
        for k in ("begin_shape_index", "end_shape_index"):
            if k in m: m[k] += off
        ma.append(m)
    sa, sb = a.get("summary", {}), b.get("summary", {})
    summ = dict(sa)
    for k in ("length", "time", "cost"):
        if k in sa or k in sb: summ[k] = round(sa.get(k, 0) + sb.get(k, 0), 3)
    for k, f in (("min_lat", min), ("min_lon", min), ("max_lat", max), ("max_lon", max)):
        if k in sa and k in sb: summ[k] = f(sa[k], sb[k])
    leg = dict(a); leg["shape"] = encode6(pa + pb[1:]); leg["maneuvers"] = ma; leg["summary"] = summ
    return leg


def route_one(zone, body, locs):
    req = dict(body); req["locations"] = locs
    code, _, out = forward(ZBASE[zone], "/route", "", json.dumps(req).encode(), "application/json")
    if code != 200:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def cross_route(body):
    """Ruta cuyos puntos no están todos en una zona: tramo a tramo, partiendo por ciudades de paso donde haga falta."""
    locs = body.get("locations") or []
    if len(locs) < 2:
        return None
    legs, first_trip = [], None
    for a, b in zip(locs, locs[1:]):
        pa, pb = (float(a["lat"]), float(a["lon"])), (float(b["lat"]), float(b["lon"]))
        za_list, zb_list = zones_for(*pa), zones_for(*pb)
        common = [z for z in za_list if z in zb_list]
        r = None
        for z in common:
            r = route_one(z, body, [a, b])
            if r: break
        if not r:
            best = None
            for za in za_list:
                for zb in zb_list:
                    path = zone_path(za, zb)
                    if path and (best is None or len(path) < len(best)):
                        best = path
            if not best:
                return None
            pts, cur = [a], pa
            for z1, z2 in zip(best, best[1:]):
                gates = sorted(GATES.get(frozenset((z1, z2)), []), key=lambda g: dist(cur, g) + dist(g, pb))
                g = next((g for g in gates if has_road(z1, *g) and has_road(z2, *g)), None)
                if not g:
                    return None
                pts.append({"lat": g[0], "lon": g[1], "type": "break"}); cur = g
            pts.append(b)
            parts = []
            for z, p, q in zip(best, pts, pts[1:]):
                rr = route_one(z, body, [p, q])
                if not rr: return None
                parts.append(rr)
            leg = parts[0]["trip"]["legs"][0]
            for rr in parts[1:]:
                leg = sew(leg, rr["trip"]["legs"][0])
            r = dict(parts[0]); r["trip"] = dict(parts[0]["trip"]); r["trip"]["legs"] = [leg]
        first_trip = first_trip or r
        legs += r["trip"]["legs"]
    trip = dict(first_trip["trip"]); trip["legs"] = legs; trip["locations"] = locs
    summ = {}
    for k in ("length", "time", "cost"):
        summ[k] = round(sum(l.get("summary", {}).get(k, 0) for l in legs), 3)
    for k, f in (("min_lat", min), ("min_lon", min), ("max_lat", max), ("max_lon", max)):
        vals = [l["summary"][k] for l in legs if k in l.get("summary", {})]
        if vals: summ[k] = f(vals)
    for k in ("has_toll", "has_highway", "has_ferry", "has_time_restrictions"):
        summ[k] = any(l.get("summary", {}).get(k) for l in legs)
    trip["summary"] = summ
    out = dict(first_trip); out["trip"] = trip
    out.pop("alternates", None)
    return out


# ---- Límite de velocidad en todas las vías ------------------------------------------------------------------------
# Las apps sacan el límite de /trace_attributes (edge.speed_limit = maxspeed de OpenStreetMap). Donde OpenStreetMap no lo
# tiene apuntado (en España, casi ninguna secundaria) se pone el genérico del país según el tipo de vía y si es poblado,
# con `speed_limit_generic: true`. El país sale de las fronteras de Natural Earth (fronteras.json, junto a este programa o
# bajado de FRONTERAS_URL al arrancar); sin fronteras, solo España, Portugal y Andorra por un recuadro.
GENERIC_LIMITS = os.environ.get("GENERIC_LIMITS", "1") != "0"
FRONTERAS_URL = os.environ.get("FRONTERAS_URL", "https://clusterride.clusterapps.es/apps/clusterride/servidor/fronteras.json")
LIMIT_ATTRS = ("edge.speed_limit", "edge.road_class", "edge.speed", "edge.lane_count", "edge.names", "edge.density",
               "matched.point", "matched.edge_index", "edge.use", "edge.roundabout", "shape", "edge.begin_shape_index",
               "edge.end_shape_index")

# Límites genéricos (km/h): poblado, carretera, vía rápida (dos carriles por sentido, no autopista), autopista.
# None = sin límite general (autobahn). España: en poblado 30 con un carril por sentido y 50 con dos (RGC, 2021).
DEFAULTS = {
    "ES": (30, 90, 120, 120), "PT": (50, 90, 100, 120), "AD": (50, 90, 90, 90), "FR": (50, 80, 110, 130),
    "MC": (50, 50, 50, 50), "IT": (50, 90, 110, 130), "SM": (50, 70, 70, 70), "VA": (30, 30, 30, 30),
    "DE": (50, 100, 100, None), "AT": (50, 100, 100, 130), "CH": (50, 80, 100, 120), "LI": (50, 80, 80, 80),
    "BE": (50, 70, 120, 120), "NL": (50, 80, 100, 100), "LU": (50, 90, 110, 130), "GB": (48, 97, 113, 113),
    "IE": (50, 80, 100, 120), "DK": (50, 80, 90, 130), "SE": (50, 70, 100, 110), "NO": (50, 80, 90, 110),
    "FI": (50, 80, 100, 120), "IS": (50, 90, 90, 90), "PL": (50, 90, 120, 140), "CZ": (50, 90, 110, 130),
    "SK": (50, 90, 90, 130), "SI": (50, 90, 110, 130), "HR": (50, 90, 110, 130), "HU": (50, 90, 110, 130),
    "RO": (50, 90, 100, 130), "BG": (50, 90, 120, 140), "GR": (50, 90, 110, 130), "EE": (50, 90, 90, 110),
    "LV": (50, 90, 100, 110), "LT": (50, 90, 110, 130), "RS": (50, 80, 100, 130), "BA": (50, 80, 100, 130),
    "ME": (50, 80, 100, 100), "MK": (50, 80, 100, 130), "AL": (40, 80, 90, 110), "XK": (50, 80, 100, 130),
    "UA": (50, 90, 110, 130), "BY": (60, 90, 90, 110), "MD": (50, 90, 90, 110), "MT": (50, 80, 80, 80),
    "CY": (50, 80, 100, 100), "TR": (50, 90, 110, 120),
}
WALLONIA_LAT = 50.75  # Bélgica: al sur (Valonia) la carretera es a 90; al norte (Flandes) a 70

# Nombres de calle en los idiomas de las zonas: es poblado aunque Valhalla vea pocas calles.
STREET = re.compile(r"^(calle|carrer|avenida|avinguda|avda\.?|av\.|plaza|pla[çc]a|paseo|passeig|ronda|r[uú]a|travesía|"
                    r"travessera|glorieta|rotonda|kalea|etorbidea|largo|praça|alameda|bulevar|boulevard|via|vía|rue|"
                    r"avenue|place|piazza|corso|viale|vicolo|ulica|ul\.|aleja|plac|náměstí|ulice|trg|odos|leoforos)\b|"
                    r"(stra(ß|ss)e|gasse|platz|allee|straat|plein|laan|gade|gatan|gata|katu|utca|iela|gatve| street)$", re.I)

_BORDERS = []  # [(iso, (w, s, e, n), [ring, …])]


def load_borders():
    data = None
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fronteras.json")
    for src in (here, "/tmp/fronteras.json"):
        try:
            with open(src) as fh:
                data = json.load(fh)
            break
        except Exception:
            pass
    if data is None and FRONTERAS_URL:
        try:
            with urllib.request.urlopen(FRONTERAS_URL, timeout=30) as r:
                raw = r.read()
            data = json.loads(raw)
            with open("/tmp/fronteras.json", "wb") as fh:
                fh.write(raw)
        except Exception as ex:
            print("fronteras:", ex, flush=True)
    if not data:
        return
    small = []
    for iso, rings in data.items():
        for ring in rings:
            xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
            small.append((iso, (min(xs), min(ys), max(xs), max(ys)), ring))
    # Los países pequeños primero (Mónaco, San Marino, Vaticano, Andorra, Liechtenstein…): están dentro del recuadro de otros.
    small.sort(key=lambda t: (t[1][2] - t[1][0]) * (t[1][3] - t[1][1]))
    _BORDERS.extend(small)
    print("fronteras:", len(data), "países", flush=True)


def _inside(ring, x, y):
    c = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]; xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            c = not c
        j = i
    return c


def in_iberia(lat, lon):
    if 27.5 <= lat <= 29.5 and -18.3 <= lon <= -13.3:  # Canarias
        return True
    if not (35.9 <= lat <= 43.9 and -9.6 <= lon <= 4.4):
        return False
    if lon >= -1.8:  # al norte de los Pirineos es Francia: frontera de Hendaya (-1.79, 43.39) a Cerbère (3.17, 42.43)
        return lat <= 43.39 - 0.1935 * (lon + 1.79)
    return True


def _country_at(lat, lon):
    for iso, (w, s, e, n), ring in _BORDERS:
        if w <= lon <= e and s <= lat <= n and _inside(ring, lon, lat):
            return iso
    return None


def country(lat, lon):
    if not _BORDERS:
        return "ES" if in_iberia(lat, lon) else None
    iso = _country_at(lat, lon)
    if iso:
        return iso
    # En la costa y en países diminutos las fronteras simplificadas se quedan cortas: el país más cerca (hasta ~10 km).
    for d in (0.02, 0.05, 0.1):
        for dy, dx in ((0, d), (0, -d), (d, 0), (-d, 0), (d, d), (d, -d), (-d, d), (-d, -d)):
            iso = _country_at(lat + dy, lon + dx)
            if iso:
                return iso
    return None


def generic_limit(e, iso="ES", lat=None):
    d = DEFAULTS.get(iso)
    if not d:
        return None
    urban_l, rural_l, fast_l, motorway_l = d
    if iso == "BE" and lat is not None and lat < WALLONIA_LAT:
        rural_l = 90
    rc = e.get("road_class")
    est = float(e.get("speed") or 0)
    lanes = int(e.get("lane_count") or 1)
    dens = int(e.get("density") or 0)
    street = any(isinstance(n, str) and STREET.search(n) for n in e.get("names") or [])
    urban = dens >= 7 or (dens >= 5 and est < 40) or street
    town = (50 if lanes >= 2 else 30) if iso == "ES" else urban_l
    if rc == "motorway":
        return motorway_l
    if rc in ("service", "service_other", "living_street"):
        return 20
    if urban or rc == "residential":
        return town
    if rc == "trunk" and lanes >= 2:
        return fast_l
    return rural_l


# ---- Límites oficiales de la DGT (España) --------------------------------------------------------------------------
# La capa «Velocidad» del Mapa de Movilidad de la DGT: unos 190.000 tramos de todas las titularidades (Estado, comunidades,
# diputaciones, cabildos…) con su límite por sentido. Se baja al arrancar y cada semana, se guarda compacta en DGT_CACHE y
# manda sobre OpenStreetMap y sobre el genérico en los tramos que casan (a menos de 35 m, en el mismo sentido y, si la vía
# tiene matrícula, con la misma). Para apagarlo, DGT_LIMITS=0.
DGT_LIMITS = os.environ.get("DGT_LIMITS", "0") == "1"   # apagado desde el 28/11/2026: solo señales de Mapillary
DGT_URL = os.environ.get("DGT_URL", "https://services-eu1.arcgis.com/xMJSZjeskIpAzCcP/arcgis/rest/services/"
                                    "Velocidad_Spain/FeatureServer/0/query")
DGT_CACHE = os.environ.get("DGT_CACHE", "/tmp/dgt-velocidad.bin")
DGT_DAYS = float(os.environ.get("DGT_DAYS", "7"))
_KX, _KY = 85280.0, 110540.0   # metros por grado a unos 40° N (basta para 25 m y para el rumbo)
_CELL = 500                    # m (casillas del índice)
_PIECE = 200                   # m como mucho por trozo
_NEAR = 35.0                   # m (la DGT dibuja cada sentido a unos 20 m del eje)
_NEAR_NOREF = 28.0             # m, si la vía no tiene matrícula
_ANGLE = math.cos(math.radians(40))
_REF = re.compile(r"^[A-Z]{1,5}-?\d")


def _norm_ref(r):
    return re.sub(r"[\s\-‐–]", "", (r or "").upper())


def _simplify(pts, tol=2.0):
    """Douglas-Peucker (en metros): quita los vértices que no cambian la línea más de `tol`."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        ax, ay = pts[i]; bx, by = pts[j]
        dx, dy = bx - ax, by - ay; L = math.hypot(dx, dy) or 1
        far, fk = 0.0, -1
        for k in range(i + 1, j):
            d = abs((pts[k][0] - ax) * dy - (pts[k][1] - ay) * dx) / L
            if d > far:
                far, fk = d, k
        if far > tol:
            keep[fk] = True; stack.append((i, fk)); stack.append((fk, j))
    return [p for p, k in zip(pts, keep) if k]


class DgtLimits:
    """Trozos de como mucho 200 m en el sentido de la marcha, ordenados por casilla de 500 m (índice compacto)."""
    FIELDS = ("x1", "y1", "x2", "y2", "v", "r")

    def __init__(self):
        self.x1 = array("i"); self.y1 = array("i"); self.x2 = array("i"); self.y2 = array("i")
        self.v = array("B"); self.r = array("H"); self.refs = []
        self.keys = array("q"); self.start = array("I")   # casillas ordenadas y dónde empiezan sus trozos

    @staticmethod
    def _cell(x1, y1, x2, y2):
        return ((x1 + x2) // 2 // _CELL) * 1000000 + ((y1 + y2) // 2 // _CELL)

    def lookup(self, lat, lon, heading, refs):
        """Límite en (lat, lon) yendo hacia `heading` (grados, 0 = norte). refs: matrículas de la vía (normalizadas)."""
        x, y = lon * _KX, lat * _KY
        hx, hy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
        cx, cy = int(x) // _CELL, int(y) // _CELL
        near = _NEAR if refs else _NEAR_NOREF
        best, bd = None, near
        for i in range(cx - 1, cx + 2):
            for j in range(cy - 1, cy + 2):
                key = i * 1000000 + j
                p = bisect.bisect_left(self.keys, key)
                if p >= len(self.keys) or self.keys[p] != key:
                    continue
                for k in range(self.start[p], self.start[p + 1]):
                    ax, ay, bx, by = self.x1[k], self.y1[k], self.x2[k], self.y2[k]
                    dx, dy = bx - ax, by - ay
                    L = math.hypot(dx, dy) or 1
                    if (dx * hx + dy * hy) / L < _ANGLE:
                        continue
                    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (L * L)))
                    d = math.hypot(x - (ax + t * dx), y - (ay + t * dy))
                    if d < bd and (not refs or self.refs[self.r[k]] in refs):
                        best, bd = self.v[k], d
        return best

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            head = json.dumps({"n": len(self.v), "cells": len(self.keys), "refs": self.refs}).encode()
            fh.write(len(head).to_bytes(4, "little")); fh.write(head)
            for f in self.FIELDS + ("keys", "start"):
                getattr(self, f).tofile(fh)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        o = cls()
        with open(path, "rb") as fh:
            head = json.loads(fh.read(int.from_bytes(fh.read(4), "little")))
            n, c = head["n"], head["cells"]; o.refs = head["refs"]
            for f in cls.FIELDS:
                getattr(o, f).fromfile(fh, n)
            o.keys.fromfile(fh, c); o.start.fromfile(fh, c + 1)
        return o

    @classmethod
    def download(cls):
        """Baja la capa por páginas y la ordena por casillas sin tener todo en memoria dos veces."""
        raw = cls(); ids, off = {}, 0
        cell = array("q")
        while True:
            q = urllib.parse.urlencode(dict(where="1=1", outFields="MATRICULA,SENTIDO,VELOCIDAD", outSR=4326,
                                            geometryPrecision=6, orderByFields="OBJECTID", resultOffset=off,
                                            resultRecordCount=2000, f="json"))
            d = None
            for t in range(5):
                try:
                    with urllib.request.urlopen(DGT_URL + "?" + q, timeout=120) as r:
                        d = json.loads(r.read())
                    break
                except Exception as ex:
                    print("dgt:", ex, flush=True); time.sleep(10 * (t + 1))
            if d is None or "features" not in d:
                raise RuntimeError("la DGT no responde")
            fs = d["features"]
            for f in fs:
                a = f.get("attributes") or {}
                for p in (f.get("geometry") or {}).get("paths") or []:
                    raw._add(a.get("MATRICULA"), a.get("SENTIDO"), a.get("VELOCIDAD"), p, ids, cell)
            off += len(fs)
            more = len(fs) == 2000 or d.get("exceededTransferLimit")
            del d, fs
            if off % 20000 < 2000:
                print("dgt: bajando", off, flush=True)
            if not more:
                break
            time.sleep(0.3)
        if off < 1000:
            raise RuntimeError(f"la DGT solo ha dado {off} tramos")
        return raw._sorted(cell)

    def _add(self, ref, sense, speed, path, ids, cell):
        if not path or len(path) < 2 or not speed or not 5 <= speed <= 150:
            return
        pts = _simplify([(lo * _KX, la * _KY) for lo, la in path])
        if sense == 2:          # la geometría va siempre en el sentido de los PK: el sentido 2 circula al revés
            pts.reverse()
        k = _norm_ref(ref)
        if k not in ids:
            ids[k] = len(self.refs); self.refs.append(k)
        rid = ids[k]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            d = math.hypot(bx - ax, by - ay)
            if d < 1:
                continue
            n = max(1, int(math.ceil(d / _PIECE)))
            for s in range(n):
                p1 = (int(ax + (bx - ax) * s / n), int(ay + (by - ay) * s / n))
                p2 = (int(ax + (bx - ax) * (s + 1) / n), int(ay + (by - ay) * (s + 1) / n))
                self.x1.append(p1[0]); self.y1.append(p1[1]); self.x2.append(p2[0]); self.y2.append(p2[1])
                self.v.append(int(speed)); self.r.append(rid)
                cell.append(self._cell(p1[0], p1[1], p2[0], p2[1]))

    def _sorted(self, cell):
        # Cuenta por casilla, sitio de cada una y se colocan los trozos (sin ordenar millones de objetos de Python).
        count = {}
        for c in cell:
            count[c] = count.get(c, 0) + 1
        o = DgtLimits(); o.refs = self.refs
        o.keys = array("q", sorted(count))
        pos, acc = {}, 0
        for c in o.keys:
            pos[c] = acc; o.start.append(acc); acc += count[c]
        o.start.append(acc); del count
        n = len(cell)
        for f in self.FIELDS:
            setattr(o, f, array(getattr(self, f).typecode, bytes(getattr(self, f).itemsize * n)))
        for i in range(n):
            j = pos[cell[i]]; pos[cell[i]] = j + 1
            o.x1[j] = self.x1[i]; o.y1[j] = self.y1[i]; o.x2[j] = self.x2[i]; o.y2[j] = self.y2[i]
            o.v[j] = self.v[i]; o.r[j] = self.r[i]
        return o


_DGT = None


def dgt_keep_fresh():
    global _DGT
    try:
        if os.path.exists(DGT_CACHE):
            _DGT = DgtLimits.load(DGT_CACHE)
            print("dgt: límites cargados", len(_DGT.v), flush=True)
    except Exception as ex:
        print("dgt: no se lee la copia:", ex, flush=True)
    while True:
        try:
            age = time.time() - os.path.getmtime(DGT_CACHE) if os.path.exists(DGT_CACHE) else 1e12
            if _DGT is None or age > DGT_DAYS * 86400:
                o = DgtLimits.download()
                o.save(DGT_CACHE)
                _DGT = o
                print("dgt: límites bajados", len(o.v), flush=True)
        except Exception as ex:
            print("dgt:", ex, flush=True)
        time.sleep(6 * 3600)


def _heading(a, b):
    return math.degrees(math.atan2((b[1] - a[1]) * _KX, (b[0] - a[0]) * _KY)) % 360


def dgt_fill(o):
    """Pone en cada tramo (edge) el límite de la DGT que más sale en sus puntos casados."""
    dg = _DGT
    if dg is None:
        return
    edges = o.get("edges") or []
    mp = [m for m in o.get("matched_points") or [] if "lat" in m and "lon" in m and isinstance(m.get("edge_index"), int)]
    votes = {}
    for i, m in enumerate(mp):
        a = (m["lat"], m["lon"])
        b = None
        for n in mp[i + 1:i + 4]:
            if abs(n["lat"] - a[0]) * _KY + abs(n["lon"] - a[1]) * _KX > 8:
                b = (n["lat"], n["lon"]); break
        if b is None:
            continue
        k = m["edge_index"]
        if not 0 <= k < len(edges):
            continue
        e = edges[k]
        if e.get("roundabout"):
            continue  # 25/11/2026: la DGT ponía en las rotondas el límite de la carretera que pasa (70 en una rotonda de la M-863)
        refs = {_norm_ref(n) for n in e.get("names") or [] if isinstance(n, str) and _REF.match(_norm_ref(n))}
        if not refs and e.get("use") in ("ramp", "turn_channel"):
            continue  # un enlace sin matrícula junto a la autovía no se queda con el límite de la autovía
        v = dg.lookup(a[0], a[1], _heading(a, b), refs)
        if v:
            votes.setdefault(k, {}); votes[k][v] = votes[k].get(v, 0) + 1
    for k, c in votes.items():
        v = max(c, key=c.get)
        e = edges[k]
        e["speed_limit"] = v
        e.pop("speed_limit_generic", None)
        e["speed_limit_dgt"] = True


# --- Señales de velocidad de Mapillary (26/11/2026) -------------------------------------------------------------------
# Las detecta Mapillary en sus fotos (flujo «Señales de velocidad» de ised-sp/clusterride-mapas-datos, cada mes): cada
# señal con su sitio, su valor (negativo = fin de ese límite), el sentido en que se ve y cuándo se vio por última vez.
# Mandan por encima de la DGT y de OpenStreetMap desde el punto exacto de la señal. © Mapillary, CC BY-SA 4.0.
SIGNS = os.environ.get("MAPILLARY_SIGNS", "1") != "0"
SIGNS_URL = os.environ.get("SIGNS_URL", "https://github.com/ised-sp/clusterride-mapas-datos/releases/download/"
                                        "senales-velocidad/senales-velocidad.json.gz")
SIGNS_CACHE = os.environ.get("SIGNS_CACHE", "/tmp/senales-velocidad.json.gz")
SIGNS_DAYS = float(os.environ.get("SIGNS_DAYS", "0.25"))   # se mira cada hora; se baja si tiene más de 6 h
SIGN_NEAR = 18.0          # m del eje de la vía casada (las señales van en el arcén o en la acera)
SIGN_ANGLE = 50.0         # grados entre el sentido en que se ve la señal y el de la marcha
SIGN_KEEP = {"motorway": 6000, "trunk": 5000}   # m que dura una señal si no hay otra (el resto, 2500)
_SCELL = 200


class Signs:
    """Puntos con sitio, valor y sentido, en celdas de 200 m. Se carga línea a línea (poca memoria):
    filas [lon, lat, valor, sentido, …]; `conv` pasa el valor a entero (p. ej. el tipo de señal a su número)."""

    def __init__(self, path, conv=int, need_dir=True):
        import gzip
        xs, ys, vs, ds = array("i"), array("i"), array("h"), array("h")
        with gzip.open(path, "rt") as fh:
            for l in fh:
                try:
                    r = json.loads(l)
                    v = conv(r[2])
                except Exception:
                    continue
                if v is None or (need_dir and r[3] is None):
                    continue
                xs.append(int(r[0] * _KX)); ys.append(int(r[1] * _KY)); vs.append(v)
                # Mapillary da hacia dónde mira la cara de la señal (aligned_direction); quien la lee va en el sentido
                # contrario: se gira 180° para guardar el sentido de la marcha (28/11/2026; antes se daban las del
                # otro sentido). Vale igual para las marcas del suelo (mismo criterio de Mapillary).
                ds.append(-1 if r[3] is None else (int(r[3]) + 180) % 360)
        cell = lambda i: (xs[i] // _SCELL) * 1000000 + ys[i] // _SCELL
        order = sorted(range(len(xs)), key=cell)
        self.x = array("i", (xs[i] for i in order)); self.y = array("i", (ys[i] for i in order))
        self.v = array("h", (vs[i] for i in order)); self.d = array("h", (ds[i] for i in order))
        del xs, ys, vs, ds
        self.keys = array("q"); self.start = array("I")
        last = None
        for i in range(len(self.x)):
            k = (self.x[i] // _SCELL) * 1000000 + self.y[i] // _SCELL
            if k != last:
                self.keys.append(k); self.start.append(i); last = k
        self.start.append(len(self.x))

    def near(self, x, y, r):
        out = []
        cx, cy = int(x) // _SCELL, int(y) // _SCELL
        n = int(r // _SCELL) + 1
        for i in range(cx - n, cx + n + 1):
            for j in range(cy - n, cy + n + 1):
                key = i * 1000000 + j
                p = bisect.bisect_left(self.keys, key)
                if p < len(self.keys) and self.keys[p] == key:
                    out.extend(range(self.start[p], self.start[p + 1]))
        return out


# Señales y marcas de la ruta (senales-ruta.json.gz, del mismo flujo): peligros, prohibido adelantar, cruces y flechas.
# El número de cada tipo es su posición en esta lista (no cambiar el orden; añadir al final).
ROUTE_TYPES = ["curva-izq", "curva-der", "curvas-izq", "curvas-der", "subida", "bajada", "deslizante", "hielo",
               "desprendimientos", "animales", "estrechamiento", "obras", "firme", "peligro", "cruce", "rotonda-aviso",
               "semaforo-aviso", "peatones", "ciclistas", "doble-sentido", "no-adelantar", "fin-no-adelantar", "stop",
               "ceda", "semaforo", "paso", "linea-stop", "linea-ceda", "flecha-recto", "flecha-izq", "flecha-der",
               "flecha-recto-izq", "flecha-recto-der", "flecha-izq-der"]
_RT = {t: i for i, t in enumerate(ROUTE_TYPES)}
_MARKS = {_RT[t] for t in ("paso", "linea-stop", "linea-ceda", "flecha-recto", "flecha-izq", "flecha-der",
                           "flecha-recto-izq", "flecha-recto-der", "flecha-izq-der")}
_NODIR = {_RT[t] for t in ("paso", "linea-stop", "linea-ceda")}    # marcas cuyo sentido no se fía
ROUTE_SIGNS_URL = os.environ.get("ROUTE_SIGNS_URL", SIGNS_URL.replace("senales-velocidad.json.gz", "senales-ruta.json.gz"))
ROUTE_SIGNS_CACHE = os.environ.get("ROUTE_SIGNS_CACHE", "/tmp/senales-ruta.json.gz")
_RSIGNS = None
NO_OVERTAKE_KEEP = 5000      # m que dura un «prohibido adelantar» sin su fin

_SIGNS = None


def _fetch_if_old(url, cache):
    age = time.time() - os.path.getmtime(cache) if os.path.exists(cache) else 1e12
    if age <= SIGNS_DAYS * 86400:
        return False
    tmp = cache + ".tmp"
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as fh:
        fh.write(r.read())
    os.replace(tmp, cache)
    return True


def signs_keep_fresh():
    global _SIGNS, _RSIGNS
    while True:
        try:
            if _fetch_if_old(SIGNS_URL, SIGNS_CACHE) or (_SIGNS is None and os.path.exists(SIGNS_CACHE)):
                _SIGNS = Signs(SIGNS_CACHE)
                print("señales: cargadas", len(_SIGNS.v), flush=True)
        except Exception as ex:
            print("señales:", ex, flush=True)
        try:
            if _fetch_if_old(ROUTE_SIGNS_URL, ROUTE_SIGNS_CACHE) or (_RSIGNS is None and os.path.exists(ROUTE_SIGNS_CACHE)):
                _RSIGNS = Signs(ROUTE_SIGNS_CACHE, conv=lambda t: _RT.get(t), need_dir=False)
                print("señales de la ruta: cargadas", len(_RSIGNS.v), flush=True)
        except Exception as ex:
            print("señales de la ruta:", ex, flush=True)
        time.sleep(3600)


def _road_key(e):
    """Lo que identifica la vía de un tramo: sus nombres y matrículas (para no llevar un límite a otra carretera)."""
    return {n for n in (e.get("names") or []) if isinstance(n, str)}


URBAN_GENERIC = os.environ.get("URBAN_GENERIC", "1") != "0"


def _urban_street(e):
    """Calle o travesía en poblado (no autopista, autovía ni ramal): ahí, sin señal, manda el genérico urbano del país
    (España: 30 con un carril por sentido, 50 con dos), no la DGT ni el maxspeed de OpenStreetMap, que en los pueblos
    suelen estar viejos (la DGT da 50 en travesías que hoy están a 30)."""
    if e.get("road_class") in ("motorway", "trunk") or e.get("use") in ("ramp", "turn_channel"):
        return False
    if e.get("road_class") in ("residential", "living_street"):
        return True
    dens = int(e.get("density") or 0)
    est = float(e.get("speed") or 0)
    street = any(isinstance(n, str) and STREET.search(n) for n in e.get("names") or [])
    return dens >= 7 or (dens >= 5 and est < 40) or street


def point_fill(o):
    """Límite a lo largo de la vía casada, punto a punto: señal de Mapillary > (en poblado) genérico urbano > DGT > lo que
    ya tenga el tramo.
    Pone en cada tramo el límite con que empieza y, si cambia dentro, «speed_limit_parts» = [[metros desde su
    principio, límite], …]. Devuelve False si no puede (sin el trazado casado)."""
    shape = o.get("shape")
    edges = o.get("edges") or []
    if not isinstance(shape, str) or not edges:
        return False
    P = decode6(shape)
    if len(P) < 2:
        return False
    xs = [p[1] * _KX for p in P]; ys = [p[0] * _KY for p in P]
    seg_edge = [None] * (len(P) - 1)
    for k, e in enumerate(edges):
        b, en = e.get("begin_shape_index"), e.get("end_shape_index")
        if not (isinstance(b, int) and isinstance(en, int)):
            return False
        for i in range(max(0, b), min(en, len(P) - 1)):
            seg_edge[i] = k
    gaps = sum(1 for v in seg_edge if v is None)
    if gaps > max(3, len(seg_edge) // 20):
        return False
    # los saltos del casado (un trozo sin tramo) no cuentan como metros recorridos
    S = [0.0]
    for i in range(1, len(P)):
        S.append(S[-1] + (math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) if seg_edge[i - 1] is not None else 0.0))
    for i in range(len(seg_edge)):
        if seg_edge[i] is None:
            seg_edge[i] = seg_edge[i - 1] if i else next((v for v in seg_edge if v is not None), 0)
    # 1) señales en la vía, en su sentido
    events = []
    sg = _SIGNS if SIGNS else None
    if sg is not None:
        seen = set()
        for i in range(len(P) - 1):
            e = edges[seg_edge[i]]
            if e.get("roundabout"):
                continue
            ax, ay, bx, by = xs[i], ys[i], xs[i + 1], ys[i + 1]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            if L < 0.5:
                continue
            hd = math.degrees(math.atan2(dx, dy)) % 360
            fast = e.get("road_class") in ("motorway", "trunk")
            for k in sg.near((ax + bx) / 2, (ay + by) / 2, L / 2 + SIGN_NEAR):
                if k in seen:
                    continue
                t = ((sg.x[k] - ax) * dx + (sg.y[k] - ay) * dy) / (L * L)
                if t < 0 or t > 1:
                    continue
                d = abs((sg.x[k] - ax) * dy - (sg.y[k] - ay) * dx) / L
                if d > SIGN_NEAR:
                    continue
                if abs((sg.d[k] - hd + 180) % 360 - 180) > SIGN_ANGLE:
                    continue
                v = sg.v[k]
                if fast and 0 < v < 60:
                    continue   # la de un ramal o una vía de servicio pegada a la autovía
                if fast and 0 < v < 80:
                    continue   # 60-70 en autovía: la vía de servicio o los ramales de al lado (A-6 en Villalba, 28/11/2026)
                if 0 < v < 30 and e.get("road_class") in ("motorway", "trunk", "primary", "secondary", "tertiary"):
                    continue   # 20 o 10 en una carretera: casi siempre la de una calle o un aparcamiento de al lado
                seen.add(k)
                events.append((S[i] + t * L, v, seg_edge[i]))
        events.sort()
    # 2) la DGT cada 20 m
    dg = _DGT if DGT_LIMITS else None
    samples = []          # (s, límite DGT o None)
    if dg is not None:
        nxt = 0.0
        for i in range(len(P) - 1):
            e = edges[seg_edge[i]]
            L = S[i + 1] - S[i]
            if L < 0.1:
                continue
            while nxt <= S[i + 1]:
                if not e.get("roundabout") and not ((e.get("use") in ("ramp", "turn_channel") or e.get("road_class") in (
                        "service_other", "service", "residential", "living_street")) and not _road_refs(e)):
                    t = (nxt - S[i]) / L
                    lat = P[i][0] + (P[i + 1][0] - P[i][0]) * t; lon = P[i][1] + (P[i + 1][1] - P[i][1]) * t
                    samples.append((nxt, dg.lookup(lat, lon, _heading(P[i], P[i + 1]), _road_refs(e))))
                else:
                    samples.append((nxt, None))
                nxt += 20.0
        # sin saltos cortos: un valor que dura menos de 3 muestras (60 m) se queda con el de antes
        j = 1
        while j < len(samples):
            if samples[j][1] != samples[j - 1][1]:
                e = j
                while e < len(samples) and samples[e][1] == samples[j][1]:
                    e += 1
                if e - j < 3 and e < len(samples) and samples[j - 1][1] is not None:
                    for q in range(j, e):
                        samples[q] = (samples[q][0], samples[j - 1][1])
                j = e
            else:
                j += 1
    # 3) límite en cada punto
    def sign_at(s, k_edge):
        """Valor de la última señal antes de s en esta misma vía y a menos de lo que dura una señal."""
        best = None
        for (se, v, ke) in events:
            if se > s:
                break
            best = (se, v, ke)
        if not best:
            return None
        se, v, ke = best
        if v < 0:
            return None    # fin de límite
        e0, e1 = edges[ke], edges[k_edge]
        if ke != k_edge and (not _road_key(e0) or _road_key(e0).isdisjoint(_road_key(e1)) or e1.get("roundabout")):
            # ¿seguimos por la misma vía desde la señal hasta aquí?
            for kk in range(ke, k_edge + 1):
                ek = edges[kk]
                if ek.get("roundabout") or (_road_key(e0) and _road_key(ek).isdisjoint(_road_key(e0))):
                    return None
        keep = SIGN_KEEP.get(edges[k_edge].get("road_class"), 2500)
        return v if s - se <= keep else None

    def dgt_at(s):
        if not samples:
            return None
        j = bisect.bisect_right([x[0] for x in samples], s) - 1 if len(samples) < 50 else _bis(samples, s)
        return samples[max(0, j)][1]

    iso = None
    if URBAN_GENERIC:
        try:
            iso = country(P[0][0], P[0][1])
        except Exception:
            iso = None
    any_change = False
    for k, e in enumerate(edges):
        b, en = e["begin_shape_index"], e["end_shape_index"]
        town = generic_limit(e, iso, P[0][0]) if iso and _urban_street(e) else None
        s0, s1 = S[b], S[min(en, len(S) - 1)]
        own = e.get("speed_limit") if isinstance(e.get("speed_limit"), (int, float)) and 5 <= e.get("speed_limit") <= 150 else None
        parts, src = [], None
        pos = s0
        while True:
            v = sign_at(pos + 0.01, k); sv = "sign"
            if v is None and town:
                v, sv = town, "gen"
            if v is None:
                # La DGT solo en carreteras: nunca en rotondas ni en viales de servicio sin matrícula (la entrada a una
                # gasolinera o a un aparcamiento se quedaba con el límite de la carretera de al lado).
                side = (e.get("use") in ("ramp", "turn_channel") or e.get("road_class") in (
                    "service_other", "service", "residential", "living_street")) and not _road_refs(e)
                v = dgt_at(pos + 0.01) if not e.get("roundabout") and not side else None; sv = "dgt"
            if v is None:
                v, sv = own, "own"
            if v is not None and (not parts or parts[-1][1] != v):
                parts.append([round(pos - s0), v])
                src = src or sv
            # siguiente punto de cambio posible: la próxima señal o muestra de la DGT dentro del tramo
            cand = [se for (se, _, _) in events if pos + 0.01 < se < s1]
            cand += [x[0] for x in samples if pos + 0.01 < x[0] < s1]
            if not cand:
                break
            pos = min(cand)
        if not parts:
            continue
        # quitar trocitos de menos de 25 m (salvo el primero)
        clean = [parts[0]]
        for p_ in parts[1:]:
            if p_[0] - clean[-1][0] < 25 and len(clean) > 1:
                clean[-1] = [clean[-1][0], p_[1]]
            elif clean[-1][1] != p_[1]:
                clean.append(p_)
        e["speed_limit"] = clean[0][1]
        if len(clean) > 1:
            e["speed_limit_parts"] = clean
            any_change = True
        else:
            e.pop("speed_limit_parts", None)
        if src == "sign":
            e["speed_limit_sign"] = True; e.pop("speed_limit_generic", None); e.pop("speed_limit_dgt", None)
        elif src == "dgt":
            e["speed_limit_dgt"] = True; e.pop("speed_limit_generic", None)
        elif src == "gen":
            e["speed_limit_generic"] = True; e.pop("speed_limit_dgt", None)
    route_signs(edges, P, xs, ys, S, seg_edge)
    return True


def route_signs(edges, P, xs, ys, S, seg_edge):
    """Señales y marcas de Mapillary en la vía casada y en su sentido: cada tramo lleva «ride_signs»
    [[metros desde su principio, tipo, metros a la derecha del eje], …] y, si hay «prohibido adelantar», «no_overtaking_parts» [[metros, 1|0], …]."""
    rs = _RSIGNS
    if rs is None:
        return
    ev = []
    seen = set()
    for i in range(len(P) - 1):
        ax, ay, bx, by = xs[i], ys[i], xs[i + 1], ys[i + 1]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        if L < 0.5:
            continue
        hd = math.degrees(math.atan2(dx, dy)) % 360
        for k in rs.near((ax + bx) / 2, (ay + by) / 2, L / 2 + 16):
            if k in seen:
                continue
            t = rs.v[k]
            near = 8.0 if t in _MARKS else 15.0
            u = ((rs.x[k] - ax) * dx + (rs.y[k] - ay) * dy) / (L * L)
            if u < 0 or u > 1:
                continue
            lat_ = ((rs.x[k] - ax) * dy - (rs.y[k] - ay) * dx) / L      # > 0: a la derecha de la marcha
            if abs(lat_) > near:
                continue
            d = rs.d[k]
            if t not in _NODIR and (d < 0 or abs((d - hd + 180) % 360 - 180) > 50):
                continue
            seen.add(k)
            ev.append((S[i] + u * L, t, seg_edge[i], round(lat_, 1)))
    ev.sort()
    # quitar repetidos del mismo tipo a menos de 40 m (la misma señal a los dos lados, o vista dos veces)
    clean = []
    for e in ev:
        # (las flechas no: cada carril tiene la suya)
        if e[1] not in _MARKS and any(c[1] == e[1] and e[0] - c[0] < 40 for c in clean[-6:]):
            continue
        clean.append(e)
    starts = [S[e["begin_shape_index"]] for e in edges]
    for (s_, t, k, lt) in clean:
        if t in (_RT["no-adelantar"], _RT["fin-no-adelantar"]):
            continue
        # [metros desde el principio del tramo, tipo, metros a la derecha (−: a la izquierda) del eje]
        edges[k].setdefault("ride_signs", []).append([round(s_ - starts[k]), ROUTE_TYPES[t], lt])
    # prohibido adelantar: desde su señal hasta su fin, otra vía, una rotonda o NO_OVERTAKE_KEEP metros
    on = None     # (s de inicio, tramo de inicio)
    spans = []
    marks = [(s_, t, k) for (s_, t, k, _) in clean if t in (_RT["no-adelantar"], _RT["fin-no-adelantar"])]
    for (s_, t, k) in marks:
        if t == _RT["no-adelantar"]:
            if on is None:
                on = (s_, k)
        elif on is not None:
            spans.append((on[0], s_)); on = None
    if on is not None:
        spans.append((on[0], min(S[-1], on[0] + NO_OVERTAKE_KEEP)))
    for (a0, a1) in spans:
        # se corta en una rotonda o al cambiar de vía
        k0 = next(i for i, st in enumerate(starts) if i + 1 >= len(starts) or starts[i + 1] > a0)
        key = _road_key(edges[k0])
        for i in range(k0 + 1, len(edges)):
            if starts[i] >= a1:
                break
            if edges[i].get("roundabout") or (key and _road_key(edges[i]).isdisjoint(key)):
                a1 = starts[i]
                break
        for i, e in enumerate(edges):
            e0 = starts[i]; e1 = S[min(e["end_shape_index"], len(S) - 1)]
            if e1 <= a0 or e0 >= a1:
                continue
            parts = e.setdefault("no_overtaking_parts", [])
            parts.append([max(0, round(a0 - e0)), 1])
            if a1 < e1:
                parts.append([round(a1 - e0), 0])


def _bis(samples, s):
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid][0] <= s:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _road_refs(e):
    return {_norm_ref(n) for n in e.get("names") or [] if isinstance(n, str) and _REF.match(_norm_ref(n))}


def limits_request(body, pts):
    """Si es un /trace_attributes que pide edge.speed_limit: pide también lo que hace falta para el genérico."""
    if not GENERIC_LIMITS or not pts:
        return None
    f = body.get("filters")
    if not isinstance(f, dict):
        return body  # sin filtro Valhalla ya da todo
    attrs = f.get("attributes") or []
    if f.get("action") != "include" or "edge.speed_limit" not in attrs:
        return None
    nb = dict(body)
    nb["filters"] = dict(f, attributes=list(attrs) + [a for a in LIMIT_ATTRS if a not in attrs])
    return nb


def fill_limits(out, pts, drop_shape=False):
    try:
        o = json.loads(out)
        done = False
        try:
            done = point_fill(o)
        except Exception as ex:
            print("límite punto a punto:", ex, flush=True)
        if DGT_LIMITS and not done:
            try:
                dgt_fill(o)
            except Exception as ex:
                print("dgt:", ex, flush=True)
        edges = o.get("edges") or []
        # El país de cada tramo: el de su punto casado (o el del anterior); sin puntos, el primero de la petición.
        where = {}
        for m in o.get("matched_points") or []:
            i = m.get("edge_index")
            if isinstance(i, int) and "lat" in m and "lon" in m and i not in where:
                where[i] = (m["lat"], m["lon"])
        last = pts[0] if pts else None
        cache = {}
        prev = None
        for i, e in enumerate(edges):
            if i in where:
                last = where[i]
            v = e.get("speed_limit")
            if isinstance(v, (int, float)) and 5 <= v <= 150:
                prev = v
                continue
            # Una rotonda sin límite propio se queda con el de la vía por la que se entra (no el genérico de la carretera).
            if e.get("roundabout") and prev:
                e["speed_limit"] = prev
                e["speed_limit_generic"] = True
                continue
            if last is None:
                continue
            key = (round(last[0], 2), round(last[1], 2))
            if key not in cache:
                cache[key] = country(*last)
            lim = generic_limit(e, cache[key], last[0])
            if lim:
                e["speed_limit"] = lim
                e["speed_limit_generic"] = True
                prev = lim
        if drop_shape:
            o.pop("shape", None)
        return json.dumps(o, separators=(",", ":")).encode()
    except Exception as ex:
        print("límites:", ex, flush=True)
        return out


# ---- Salidas de las rotondas -------------------------------------------------------------------------------------
# Valhalla cuenta mal algunas rotondas (28/11/2026: la de la M-601 con la Avenida de Buenos Aires en Collado Mediano,
# que es la primera salida, la daba como la segunda). Se recuentan con el trazado: 1 + las salidas por las que se pasa
# dentro de la rotonda (vías que salen de ella, sin contar los accesos de servicio: gasolineras, aparcamientos…).
RB_FIX = os.environ.get("ROTONDAS", "1") == "1"
ORD_ES = ["primera", "segunda", "tercera", "cuarta", "quinta", "sexta", "séptima", "octava", "novena", "décima"]


def _rb_text(s, old, new):
    if not isinstance(s, str) or old == new:
        return s
    s = re.sub(r"salida %d\.º" % old, "salida %d.º" % new, s)
    if 1 <= old <= 10 and 1 <= new <= 10:
        s = re.sub(r"\b%s salida\b" % ORD_ES[old - 1], "%s salida" % ORD_ES[new - 1], s)
    return s


def fix_roundabouts(base, body, out):
    try:
        o = json.loads(out)
        legs = (o.get("trip") or {}).get("legs") or []
        if not any(m.get("type") == 26 for lg in legs for m in lg.get("maneuvers", [])):
            return out
        costing = body.get("costing", "motorcycle") if isinstance(body, dict) else "motorcycle"
        changed = False
        for lg in legs:
            ms = lg.get("maneuvers", [])
            if not any(m.get("type") == 26 for m in ms) or not lg.get("shape"):
                continue
            req = {"encoded_polyline": lg["shape"], "costing": costing, "shape_match": "edge_walk",
                   "filters": {"attributes": ["edge.roundabout", "edge.begin_shape_index", "edge.end_shape_index",
                                              "node.intersecting_edge.driveability", "node.intersecting_edge.road_class"],
                               "action": "include"}}
            code, _, t = forward(base, "/trace_attributes", "", json.dumps(req).encode(), "application/json")
            if code != 200:
                continue
            E = json.loads(t).get("edges") or []
            for i, m in enumerate(ms):
                if m.get("type") != 26 or i + 1 >= len(ms):
                    continue
                b, e = m.get("begin_shape_index", 0), ms[i + 1].get("begin_shape_index", 0)
                rb = [x for x in E if x.get("roundabout") and x.get("begin_shape_index", -1) >= b and x.get("end_shape_index", 1 << 30) <= e]
                if not rb:
                    continue
                n = 1
                for x in rb[:-1]:
                    for y in (x.get("end_node") or {}).get("intersecting_edges", []):
                        if y.get("driveability") in ("forward", "both") and y.get("road_class") != "service_other":
                            n += 1
                old = m.get("roundabout_exit_count")
                if isinstance(old, int) and old != n:
                    m["roundabout_exit_count"] = n
                    for k in ("instruction", "verbal_transition_alert_instruction", "verbal_pre_transition_instruction",
                              "verbal_succinct_transition_instruction"):
                        if k in m:
                            m[k] = _rb_text(m[k], old, n)
                    changed = True
        return json.dumps(o).encode() if changed else out
    except Exception as e:
        print("rotondas:", e, flush=True)
        return out


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one(self, data):
        u = urllib.parse.urlsplit(self.path)
        body = {}
        try:
            if data:
                body = json.loads(data.decode("utf-8"))
            else:
                q = urllib.parse.parse_qs(u.query).get("json")
                if q: body = json.loads(q[0])
        except Exception:
            body = {}
        pts = points(body) if isinstance(body, dict) and u.path.rstrip("/") in POINTS_PATHS else []
        limits = limits_request(body, pts) if u.path.rstrip("/") == "/trace_attributes" and isinstance(body, dict) else None
        if limits is not None and limits is not body:
            data = json.dumps(limits).encode()
        cands = [z for z in ZONES if pts and inside(z[2], pts)] if pts else ZONES[:1]
        if not cands:
            cands = ZONES[:1]
        res = first = None
        used = None
        for _, base, _ in cands:
            res = forward(base, u.path, u.query, data, self.headers.get("Content-Type"))
            if res[0] == 200:
                used = base
                break
            if first is None and res[0] != 502:
                first = res  # el error de la primera zona que responde (p. ej. «no hay camino»), no el de una apagada
        code, ctype, out = res if res[0] == 200 else (first or res)
        if code == 200 and limits is not None:
            asked = set(((body.get("filters") or {}).get("attributes") or [])) if isinstance(body, dict) else set()
            out = fill_limits(out, pts, drop_shape=isinstance(body.get("filters"), dict) and "shape" not in asked)
        if code == 200 and used and RB_FIX and u.path.rstrip("/") in ("/route", "/optimized_route"):
            out = fix_roundabouts(used, body, out)
        # Ninguna zona la ha resuelto entera: si es una ruta, se calcula tramo a tramo entre zonas.
        if code != 200 and u.path.rstrip("/") == "/route" and isinstance(body, dict):
            try:
                cr = cross_route(body)
            except Exception as e:
                print("ruta entre zonas:", e, flush=True); cr = None
            if cr:
                code, ctype, out = 200, "application/json", json.dumps(cr).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path.rstrip("/") == "/senales":
            return self.nearby_signs(urllib.parse.parse_qs(u.query))
        self.handle_one(None)

    def nearby_signs(self, q):
        """Sin ruta: las señales de Mapillary a menos de r metros (como mucho 3 km), para avisar de lo que viene por el
        rumbo. [[lon, lat, tipo, sentido], …]; las de velocidad con tipo «v<límite>» (o «v-<límite>», fin)."""
        try:
            lat = float(q["lat"][0]); lon = float(q["lon"][0]); r = min(3000.0, float(q.get("r", ["2000"])[0]))
        except Exception:
            lat = lon = None
        out = []
        if lat is not None:
            x, y = lon * _KX, lat * _KY
            for idx, name in ((_RSIGNS, None), (_SIGNS if SIGNS else None, "v")):
                if idx is None:
                    continue
                for k in idx.near(x, y, r):
                    if math.hypot(idx.x[k] - x, idx.y[k] - y) > r:
                        continue
                    t = ROUTE_TYPES[idx.v[k]] if name is None else f"v{idx.v[k]}"
                    out.append([round(idx.x[k] / _KX, 6), round(idx.y[k] / _KY, 6), t, None if idx.d[k] < 0 else idx.d[k]])
        data = json.dumps(out, separators=(",", ":")).encode()
        self.send_response(200 if lat is not None else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.handle_one(self.rfile.read(n) if n else None)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    print("Zonas:", [(z[0], z[1]) for z in ZONES], flush=True)
    if GENERIC_LIMITS:
        load_borders()
        if DGT_LIMITS:
            threading.Thread(target=dgt_keep_fresh, daemon=True).start()
        if SIGNS:
            threading.Thread(target=signs_keep_fresh, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
