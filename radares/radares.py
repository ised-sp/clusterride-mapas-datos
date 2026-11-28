#!/usr/bin/env python3
"""
Todos los radares para ClusterRide en un solo fichero (radares.json), con su tipo.

Fuentes (todas abiertas):
  - DGT, Punto de Acceso Nacional: radares fijos y de tramo (DATEX II).
  - Servei Català de Trànsit: radares fijos y de tramo de Cataluña, y radares en remolque (TXT, UTM 31N ETRS89).
  - Ayuntamiento de Madrid: radares fijos y de tramo (CSV, CC BY 4.0).
  - OpenStreetMap (© colaboradores de OpenStreetMap, ODbL): highway=speed_camera y relaciones type=enforcement
    (maxspeed, average_speed = tramo, traffic_signals = semáforo), por países de Europa con Overpass (varios espejos).

Salida: {"generated", "counts", "cameras": [[id, kind, lon, lat, end_lon, end_lat, road, direction, max_speed, src], …]}
  kind: fijo | tramo | semaforo | remolque.   direction: rumbo en grados (texto) si se sabe, para avisar solo en ese sentido.
Lo oficial manda: un radar de OpenStreetMap a menos de 150 m de uno oficial del mismo tipo no se repite.

Uso: python3 radares.py salida.json [carpeta-de-entrada]   (con carpeta, no baja nada: lee dgt.xml, sct.txt, sct-remolc.txt,
madrid.csv y osm-*.json de ahí; para probar)
"""
import csv, io, json, math, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

UA = "ClusterRide/1.0 (https://clusterride.clusterapps.es; radares)"
DGT = "https://nap.dgt.es/datex2/dgt/PredefinedLocationsPublication/radares/content.xml"
SCT = "https://transit.gencat.cat/web/.content/documents/seguretat_viaria/radars.txt"
SCT_REMOLC = "https://transit.gencat.cat/web/.content/documents/seguretat_viaria/radars-remolc.txt"
MADRID = ("https://datos.madrid.es/dataset/300049-0-radares-fijos-moviles/resource/300049-1-radares-fijos-moviles-csv/"
          "download/300049-1-radares-fijos-moviles-csv.csv")
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
# Europa (lo que cubren los mapas y las zonas de rutas), España primero.
COUNTRIES = ("ES PT AD FR MC IT SM VA MT DE AT CH LI BE NL LU GB IE DK NO SE FI IS EE LV LT PL CZ SK HU SI HR BA RS ME "
             "MK AL XK GR BG RO MD CY").split()


def get(url, data=None, tries=3, timeout=300):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            print(f"  {url[:70]}… intento {i + 1}: {e}", flush=True)
            time.sleep(10 * (i + 1))
    return None


def bearing(lon1, lat1, lon2, lat2):
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1)))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def meters(lon1, lat1, lon2, lat2):
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * 111320 * k, (lat2 - lat1) * 110540)


def cam(id_, kind, lon, lat, src, elon=None, elat=None, road=None, direction=None, vmax=None):
    return {"id": id_, "kind": kind, "lon": round(lon, 6), "lat": round(lat, 6),
            "elon": None if elon is None else round(elon, 6), "elat": None if elat is None else round(elat, 6),
            "road": (road or None) and str(road)[:80], "dir": direction, "vmax": vmax, "src": src}


# ---------------------------------------------------------------- DGT
def dgt(raw):
    out = []
    root = ET.fromstring(raw)
    ns = {"d": "http://datex2.eu/schema/1_0/1_0"}
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
    for loc in root.iter("{http://datex2.eu/schema/1_0/1_0}predefinedLocation"):
        lid = loc.get("id")
        inner = loc.find("d:predefinedLocation", ns)
        if not lid or inner is None:
            continue

        def pt(path):
            p = inner.find(path, ns)
            if p is None:
                return None
            try:
                return float(p.find("d:longitude", ns).text), float(p.find("d:latitude", ns).text)
            except (AttributeError, TypeError, ValueError):
                return None

        road = None
        for n in inner.iter("{http://datex2.eu/schema/1_0/1_0}name"):
            if (n.findtext("d:tpegDescriptorType", "", ns) or "") == "linkName":
                road = n.findtext("d:descriptor/d:value", None, ns)
                break
        road = inner.findtext(".//d:roadNumber", None, ns) or road
        if "Linear" in (inner.get(xsi) or ""):
            a = pt("d:tpeglinearLocation/d:from/d:pointCoordinates")
            b = pt("d:tpeglinearLocation/d:to/d:pointCoordinates")
            if a and b:
                out.append(cam("dgt:" + lid, "tramo", a[0], a[1], "dgt", b[0], b[1], road,
                               str(round(bearing(a[0], a[1], b[0], b[1])))))
            elif a or b:
                p = a or b
                out.append(cam("dgt:" + lid, "fijo", p[0], p[1], "dgt", road=road))
        else:
            p = pt("d:tpegpointLocation/d:point/d:pointCoordinates")
            if p:
                out.append(cam("dgt:" + lid, "fijo", p[0], p[1], "dgt", road=road))
    return out


# ---------------------------------------------------------------- Cataluña (SCT)
def utm31(x, y):
    from pyproj import Transformer
    t = utm31.t = getattr(utm31, "t", None) or Transformer.from_crs("EPSG:25831", "EPSG:4326", always_xy=True)
    return t.transform(x, y)


def num(s):
    return float(s.replace(",", "."))


def fit(v, lo, hi):
    """Coordenada con la coma perdida en el fichero (460110597 = 460110,597): se divide por 10 hasta que cae en su rango."""
    while v > hi:
        v /= 10
    return v if lo < v < hi else None


def sct_rows(raw):
    text = raw.decode("latin-1")
    rows = []
    for line in text.splitlines():
        if "\t" in line:
            p = [x.strip() for x in line.split("\t")]
        else:
            m = re.match(r"^\s*(\S+)\s+(.+?)\s+(\d{2,3})\s+([\d.,]+)\s+([\d.,]+)\s*$", line)
            p = list(m.groups()) if m else []
        if len(p) < 5 or p[0].lower() == "via":
            continue
        if not re.search(r"[.,]", p[3]) and len(p[3]) != 8 and len(p[3]) > 6:
            # X sin coma: con 8 cifras es 123456,78; con 9 son dos Y sin la X (C-59, N-2): no se puede situar
            print("  SCT sin coordenadas válidas, se salta:", line.strip(), flush=True)
            continue
        try:
            x, y, v = num(p[3]), num(p[4]), int(num(p[2]))
        except ValueError:
            continue
        if not re.search(r"[.,]", p[3]) and len(p[3]) == 8:
            x /= 100
        if 3_200_000 < x < 3_600_000:  # errata del fichero: 3401992 en vez de 401992
            x -= 3_000_000
        x, y = fit(x, 250_000, 560_000), fit(y, 4_480_000, 4_760_000)
        if x is None or y is None:
            print("  SCT fuera de Cataluña, se salta:", line.strip(), flush=True)
            continue
        lon, lat = utm31(x, y)
        rows.append((p[0], re.sub(r"\s+", "", p[1]), v, lon, lat))
    return rows


def pk_range(pk):
    m = re.match(r"^([\d.,]+)-([\d.,]+)$", pk)
    return (num(m.group(1)), num(m.group(2))) if m else None


def sct(raw, remolc):
    out = []
    rows = sct_rows(raw)
    tramos = [(road, pk_range(pk), lon, lat) for road, pk, v, lon, lat in rows if pk_range(pk)]
    for road, pk, v, lon, lat in rows:
        r = pk_range(pk)
        if r:
            a, b = r
            # el de vuelta empieza más o menos donde acaba este (los PK no siempre cuadran: 501,5-506,5 y 506,3-501,5)
            back = sorted((abs(t[1][0] - b) + abs(t[1][1] - a), i) for i, t in enumerate(tramos)
                          if t[0] == road and t[1] != (a, b) and abs(t[1][0] - b) < 1.5 and abs(t[1][1] - a) < 1.5)
            if back:
                e = tramos[back[0][1]]
                out.append(cam(f"sct:{road}:{pk}", "tramo", lon, lat, "sct", e[2], e[3], road,
                               str(round(bearing(lon, lat, e[2], e[3]))), v))
            else:
                out.append(cam(f"sct:{road}:{pk}", "tramo", lon, lat, "sct", road=road, vmax=v))
        else:
            out.append(cam(f"sct:{road}:{pk}", "fijo", lon, lat, "sct", road=road, vmax=v))
    for road, pk, v, lon, lat in sct_rows(remolc):
        out.append(cam(f"sct-remolc:{road}:{pk}", "remolque", lon, lat, "sct", road=road, vmax=v))
    return out


# ---------------------------------------------------------------- Madrid
def madrid(raw):
    out = []
    text = raw.decode("utf-8-sig", errors="replace")
    rd = csv.reader(io.StringIO(text), delimiter=";")
    head = None
    for r in rd:
        if head is None:
            head = [h.strip().lower() for h in r]
            continue
        if len(r) < len(head) - 1:
            continue
        d = dict(zip(head, r))

        def f(k):
            try:
                return float(d.get(k, "").replace(",", "."))
            except ValueError:
                return None

        lon, lat = f("longitud"), f("latitud")
        slon, slat = f("longitud inicio tramo"), f("latitud inicio tramo")
        tipo = (d.get("tipo") or "").lower()
        try:
            v = int(float((d.get("velocidad límite") or d.get("velocidad limite") or "").replace(",", ".")))
        except ValueError:
            v = None
        road = d.get("carretara o vial") or d.get("carretera o vial")
        n = (d.get(head[0]) or "").strip()
        if "tramo" in tipo:
            if slon and slat and lon and lat:
                out.append(cam(f"madrid:{n}", "tramo", slon, slat, "madrid", lon, lat, road,
                               str(round(bearing(slon, slat, lon, lat))), v))
            elif lon and lat:
                out.append(cam(f"madrid:{n}", "fijo", lon, lat, "madrid", road=road, vmax=v))
        elif lon and lat:
            out.append(cam(f"madrid:{n}", "fijo", lon, lat, "madrid", road=road, vmax=v))
    return out


# ---------------------------------------------------------------- OpenStreetMap
CARD = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5, "S": 180,
        "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}


def osm_dir(v):
    if not v:
        return None
    v = v.strip().upper()
    if re.match(r"^\d{1,3}(\.\d+)?$", v):
        return str(round(float(v)) % 360)
    if v in CARD:
        return str(round(CARD[v]))
    return None


def speed(v):
    if v and re.match(r"^\d{1,3}$", v.strip()) and 5 <= int(v) <= 200:
        return int(v)
    return None


def overpass(cc):
    q = (f'[out:json][timeout:280];area["ISO3166-1"="{cc}"][admin_level=2]->.a;'
         'node["highway"="speed_camera"](area.a)->.n;rel["type"="enforcement"](area.a)->.r;'
         '(.n;.r;node(r.r););out body;')
    for i in range(len(OVERPASS) * 2):
        url = OVERPASS[i % len(OVERPASS)]
        raw = get(url, urllib.parse.urlencode({"data": q}).encode(), tries=1, timeout=320)
        if raw:
            try:
                d = json.loads(raw)
            except ValueError:
                print(f"  {cc} {url}: no es JSON ({raw[:80]!r})", flush=True)
                continue
            if "remark" in d and not d.get("elements"):
                print(f"  {cc} {url}: {d['remark'][:120]}", flush=True)
                continue
            return d
        time.sleep(20)
    return None


def osm(data):
    nodes, rels = {}, []
    for e in data.get("elements", []):
        if e["type"] == "node":
            nodes[e["id"]] = e
        elif e["type"] == "relation":
            rels.append(e)
    out, used = [], set()
    for r in rels:
        t = r.get("tags", {})
        enf = (t.get("enforcement") or "").lower()
        kind = {"maxspeed": "fijo", "average_speed": "tramo", "traffic_signals": "semaforo"}.get(enf)
        if not kind:
            continue
        mem = r.get("members", [])
        dev = [nodes[m["ref"]] for m in mem if m["type"] == "node" and m.get("role") == "device" and m["ref"] in nodes]
        frm = [nodes[m["ref"]] for m in mem if m["type"] == "node" and m.get("role") == "from" and m["ref"] in nodes]
        to = [nodes[m["ref"]] for m in mem if m["type"] == "node" and m.get("role") == "to" and m["ref"] in nodes]
        vmax = speed(t.get("maxspeed"))
        road = t.get("ref") or t.get("name")
        if kind == "tramo" and frm and to:
            a, b = frm[0], to[0]
            out.append(cam(f"osm:r{r['id']}", "tramo", a["lon"], a["lat"], "osm", b["lon"], b["lat"], road,
                           str(round(bearing(a["lon"], a["lat"], b["lon"], b["lat"]))), vmax))
            for d in dev:
                used.add(d["id"])
            continue
        for d in dev:
            dt = d.get("tags", {})
            dirn = osm_dir(dt.get("direction") or dt.get("camera:direction"))
            if not dirn and to:
                dirn = str(round(bearing(d["lon"], d["lat"], to[0]["lon"], to[0]["lat"])))
            k = "fijo" if kind == "tramo" else kind
            out.append(cam(f"osm:{d['id']}", k, d["lon"], d["lat"], "osm", road=road or dt.get("ref"), direction=dirn,
                           vmax=None if k == "semaforo" else (vmax or speed(dt.get("maxspeed")))))
            used.add(d["id"])
    for n in nodes.values():
        t = n.get("tags", {})
        if n["id"] in used or t.get("highway") != "speed_camera":
            continue
        out.append(cam(f"osm:{n['id']}", "fijo", n["lon"], n["lat"], "osm", road=t.get("ref"),
                       direction=osm_dir(t.get("direction") or t.get("camera:direction")), vmax=speed(t.get("maxspeed"))))
    return out


# ---------------------------------------------------------------- juntar
def dedupe(official, others):
    """Quita de «others» lo que ya está en lo oficial (mismo tipo, o fijo/tramo, a menos de 150 m)."""
    grid = {}
    for c in official:
        grid.setdefault((round(c["lat"] * 200), round(c["lon"] * 150)), []).append(c)
    keep = []
    for c in others:
        gy, gx = round(c["lat"] * 200), round(c["lon"] * 150)
        dup = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for o in grid.get((gy + dy, gx + dx), []):
                    same = o["kind"] == c["kind"] or {o["kind"], c["kind"]} <= {"fijo", "tramo", "remolque"}
                    if same and meters(o["lon"], o["lat"], c["lon"], c["lat"]) < 150:
                        dup = True
        if not dup:
            keep.append(c)
    return keep


def main():
    outp = sys.argv[1]
    local = sys.argv[2] if len(sys.argv) > 2 else None

    def src(name, url):
        if local:
            p = os.path.join(local, name)
            return open(p, "rb").read() if os.path.exists(p) else None
        return get(url)

    counts, official = {}, []
    raw = src("dgt.xml", DGT)
    if raw:
        a = dgt(raw)
        official += a
        counts["dgt"] = len(a)
    raw, rem = src("sct.txt", SCT), src("sct-remolc.txt", SCT_REMOLC)
    if raw:
        a = sct(raw, rem or b"")
        official += a
        counts["sct"] = len(a)
    raw = src("madrid.csv", MADRID)
    if raw:
        a = madrid(raw)
        official += a
        counts["madrid"] = len(a)
    # entre oficiales: Madrid y Cataluña frente a la DGT
    dg = [c for c in official if c["src"] == "dgt"]
    official = dg + dedupe(dg, [c for c in official if c["src"] != "dgt"])

    cache = os.environ.get("CACHE")  # carpeta con lo último bueno de cada país (si Overpass falla, se usa eso)
    allosm, failed = [], []
    for cc in COUNTRIES:
        if local:
            p = os.path.join(local, f"osm-{cc}.json")
            d = json.load(open(p)) if os.path.exists(p) else None
        else:
            d = overpass(cc)
            time.sleep(5)
        cp = os.path.join(cache, f"osm-{cc}.json") if cache else None
        if d is None:
            failed.append(cc)
            if cp and os.path.exists(cp):
                a = json.load(open(cp))
                print(f"  OSM {cc}: no responde, se usa lo de la última vez ({len(a)})", flush=True)
                allosm += a
            continue
        a = osm(d)
        print(f"  OSM {cc}: {len(a)}", flush=True)
        if cp and (a or not os.path.exists(cp)):
            os.makedirs(cache, exist_ok=True)
            json.dump(a, open(cp, "w"), separators=(",", ":"))
        allosm += a
    seen, uniq = set(), []
    for c in allosm:
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    osm_keep = dedupe(official, uniq)
    counts["osm"] = len(osm_keep)
    cams, ids = [], set()
    for c in official + osm_keep:
        if c["id"] not in ids:
            ids.add(c["id"])
            cams.append(c)
    kinds = {}
    for c in cams:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    res = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "counts": counts, "kinds": kinds,
           "osm_failed": failed,
           "cameras": [[c["id"], c["kind"], c["lon"], c["lat"], c["elon"], c["elat"], c["road"], c["dir"], c["vmax"], c["src"]]
                       for c in cams]}
    with open(outp, "w") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({k: res[k] for k in ("counts", "kinds", "osm_failed")}, ensure_ascii=False))
    if failed:
        print("Países de OpenStreetMap que no han respondido:", " ".join(failed))


if __name__ == "__main__":
    main()
