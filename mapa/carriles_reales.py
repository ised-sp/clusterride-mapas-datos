"""Carriles realistas para el mapa de ClusterRide (26/11/2026).

Lee un OpenStreetMap ya filtrado (vías principales y sus enlaces, y los nodos de semáforo y stop) y los carriles del IGN
cruzados (espana-carriles.csv.gz: way_id,lanes, solo para vías de OSM que no los tienen) y escribe un GeoJSON por líneas
con lo que se pinta de cerca: asfalto, contorno, bordes, rayas, eje, cebreados, ceda el paso, detención y flechas
(mapa/carriles_geo.py hace la geometría; aquí se trocea el país en celdas con margen y se juntan).

Uso: python3 carriles_reales.py <osm.pbf> [espana-carriles.csv.gz] [izquierda] > carriles.geojsonl
(«izquierda» para Reino Unido, Irlanda, Malta y Chipre: se hace en espejo y se deshace al final)
"""
import csv, gzip, json, math, os, sys, time, traceback
from collections import defaultdict
import multiprocessing as mp
import numpy as np
import osmium
from shapely.geometry import box
import shapely
import warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carriles_geo as G

CELL = 0.08          # grados
MARGIN = 0.012       # ~1 km de margen alrededor de cada celda
KEEP = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
        "tertiary", "tertiary_link"}
TAGS = {"highway", "oneway", "junction", "lanes", "lanes:forward", "lanes:backward", "turn:lanes", "turn:lanes:forward",
        "turn:lanes:backward", "layer", "bridge", "tunnel", "covered", "ref", "name", "maxspeed", "area"}


def entero(v):
    try:
        n = int(str(v).split(";")[0].strip())
        return n if 0 < n < 13 else None
    except Exception:
        return None


IZQ = len(sys.argv) > 3 and sys.argv[3] == "izquierda"
ign = {}
if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
    for r in csv.reader(gzip.open(sys.argv[2], "rt")):
        if r and r[0].isdigit():
            ign[int(r[0])] = entero(r[1])

WAYS = []            # (id, refs np.int64, lon np.float64, lat np.float64, tags)
NTAGS = {}


def espejo_giros(v):
    if not v:
        return v
    sw = {"left": "right", "right": "left", "slight_left": "slight_right", "slight_right": "slight_left",
          "sharp_left": "sharp_right", "sharp_right": "sharp_left", "merge_to_left": "merge_to_right",
          "merge_to_right": "merge_to_left"}
    lanes = [";".join(sw.get(x, x) for x in l.split(";")) for l in v.split("|")]
    return "|".join(lanes[::-1])


class H(osmium.SimpleHandler):
    def node(self, n):
        h = n.tags.get("highway")
        if h in ("traffic_signals", "stop") or n.tags.get("crossing") == "traffic_signals":
            NTAGS[n.id] = {"highway": h or "", "crossing": n.tags.get("crossing", "")}

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if hw not in KEEP or t.get("area") == "yes":
            return
        try:
            refs = np.array([nd.ref for nd in w.nodes], np.int64)
            lon = np.array([nd.lon for nd in w.nodes], np.float64)
            lat = np.array([nd.lat for nd in w.nodes], np.float64)
        except osmium.InvalidLocationError:
            return
        if len(refs) < 2:
            return
        tags = {k: t.get(k) for k in TAGS if k in t}
        if "lanes" not in tags and w.id in ign and ign[w.id]:
            tags["lanes"] = str(ign[w.id])
        if IZQ:
            for k in ("turn:lanes", "turn:lanes:forward", "turn:lanes:backward"):
                if k in tags:
                    tags[k] = espejo_giros(tags[k])
            # en espejo, «forward» y «backward» siguen siendo los del sentido de la vía
        WAYS.append((w.id, refs, lon, lat, tags))


class MirrorProj(G.Proj):
    def __call__(self, lon, lat):
        x, y = super().__call__(lon, lat)
        return (-x, y)

    def inv(self, x, y):
        return super().inv(-np.asarray(x), y)


MAXW = 3000          # vías como mucho por celda: las de más (centros de ciudad) se parten en cuatro


def celda(args):
    lon0, lat0, size, idx = args
    t0 = time.time()
    i, j = lon0, lat0
    try:
        nodes = {}
        ways = []
        for k in idx:
            wid, refs, lon, lat, tags = WAYS[k]
            for r, a, b in zip(refs.tolist(), lon.tolist(), lat.tolist()):
                nodes[r] = (a, b)
            ways.append((wid, refs.tolist(), tags))
        nt = {r: NTAGS[r] for r in nodes if r in NTAGS}
        cx, cy = lon0 + size / 2, lat0 + size / 2
        proj = MirrorProj(cx, cy) if IZQ else G.Proj(cx, cy)
        net = G.Network(nodes, nt, ways, proj)
        out = net.build()
        # recorte a la celda (sin el margen)
        corners = [proj(lon0, lat0), proj(lon0 + size, lat0 + size)]
        xs = sorted(c[0] for c in corners); ys = sorted(c[1] for c in corners)
        clip = box(xs[0], ys[0], xs[1], ys[1])
        lines = []
        for g, props in out.feats:
            if not g.intersects(clip):
                continue
            if not clip.contains(g):
                g = g.intersection(clip)
                if g.is_empty:
                    continue
            g2 = shapely.ops.transform(lambda x, y, z=None: proj.inv(x, y), g)
            g2 = shapely.set_precision(g2, 1e-7)
            if g2.is_empty:
                continue
            props = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in props.items() if k in ("t", "lv", "tu", "w", "d")}
            lines.append(json.dumps({"type": "Feature", "properties": props,
                                     "geometry": shapely.geometry.mapping(g2)}, separators=(",", ":")))
        dt = time.time() - t0
        if dt > 120:
            print(f"  celda lenta {lon0:.3f},{lat0:.3f} ({size:g}°, {len(idx)} vías): {dt:.0f} s", file=sys.stderr, flush=True)
        return i, j, lines, None
    except Exception:
        return i, j, [], traceback.format_exc()[-1500:]


def partir(lon0, lat0, size, idx, bb):
    """Celdas de como mucho MAXW vías: las muy llenas se parten en cuatro (hasta 0,01°)."""
    if len(idx) <= MAXW or size <= 0.0101:
        return [(lon0, lat0, size, idx)]
    h = size / 2
    out = []
    for a in (0, 1):
        for b in (0, 1):
            x0, y0 = lon0 + a * h, lat0 + b * h
            sub = [k for k in idx if bb[k][0] <= x0 + h + MARGIN and bb[k][2] >= x0 - MARGIN
                   and bb[k][1] <= y0 + h + MARGIN and bb[k][3] >= y0 - MARGIN]
            if sub:
                out += partir(x0, y0, h, sub, bb)
    return out


def main():
    t0 = time.time()
    H().apply_file(sys.argv[1], locations=True, idx="flex_mem")
    print(f"vías: {len(WAYS)} · semáforos y stops: {len(NTAGS)} · {time.time() - t0:.0f} s", file=sys.stderr)
    cells = defaultdict(list)
    for k, (wid, refs, lon, lat, tags) in enumerate(WAYS):
        a0 = math.floor((lon.min() - MARGIN) / CELL); a1 = math.floor((lon.max() + MARGIN) / CELL)
        b0 = math.floor((lat.min() - MARGIN) / CELL); b1 = math.floor((lat.max() + MARGIN) / CELL)
        for i in range(a0, a1 + 1):
            for j in range(b0, b1 + 1):
                cells[(i, j)].append(k)
    # solo celdas con alguna vía dentro de verdad (no solo en el margen)
    core = set()
    for k, (wid, refs, lon, lat, tags) in enumerate(WAYS):
        for a, b in zip(np.floor(lon / CELL).astype(int).tolist(), np.floor(lat / CELL).astype(int).tolist()):
            core.add((a, b))
    bb = [(lon.min(), lat.min(), lon.max(), lat.max()) for (wid, refs, lon, lat, tags) in WAYS]
    jobs = []
    for (i, j) in cells:
        if (i, j) in core:
            jobs += partir(i * CELL, j * CELL, CELL, cells[(i, j)], bb)
    jobs.sort(key=lambda x: -len(x[3]))
    # PARTE=k/N: solo una de N partes (por turnos sobre las celdas ordenadas, para que todas pesen lo mismo).
    parte = os.environ.get("PARTE", "")
    if "/" in parte:
        k0, nparts = (int(x) for x in parte.split("/"))
        jobs = jobs[k0::nparts]
        print(f"parte {k0 + 1} de {nparts}", file=sys.stderr)
    if jobs:
        print(f"la más llena: {len(jobs[0][3])} vías", file=sys.stderr)
    print(f"celdas: {len(jobs)}", file=sys.stderr)
    n = 0
    errs = 0
    # «fork»: los procesos heredan las vías ya leídas (en Python 3.14 el modo por defecto ya no es este)
    with mp.get_context("fork").Pool(os.cpu_count() or 2) as pool:
        for k, (i, j, lines, err) in enumerate(pool.imap_unordered(celda, jobs, chunksize=1)):
            if err:
                errs += 1
                print(f"ERROR celda {i:.3f},{j:.3f}: {err}", file=sys.stderr)
            for ln in lines:
                sys.stdout.write(ln + "\n")
            n += len(lines)
            if k % 50 == 0:
                print(f"  {k}/{len(jobs)} celdas · {n} elementos · {time.time() - t0:.0f} s", file=sys.stderr)
    print(f"elementos: {n} · celdas con error: {errs} · {time.time() - t0:.0f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
