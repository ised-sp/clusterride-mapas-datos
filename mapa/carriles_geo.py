#!/usr/bin/env python3
"""
Carriles realistas a partir de OpenStreetMap (ClusterRide, 26/11/2026).

Cada calzada se dibuja como una superficie continua (no un trozo por vía de OSM) con su ancho real:
  * cadenas: las vías que siguen una a otra (mismo sentido, sin giro fuerte) forman una sola calzada;
  * perfil lateral: ancho de los carriles + arcenes; cuando cambia el número de carriles el borde se abre o se cierra
    en una cuña (transición) y el otro borde se queda quieto;
  * uniones y desdobles (ramales de autovía, glorietas partidas, calzadas que se separan): el ramal sale pegado a su
    sitio en la calzada (sus carriles son los que se separan o, si no los hay, sale de cero con una cuña) y se va
    separando hasta tomar su propio eje; entre los dos, el cebreado;
  * cruces a nivel: la vía con prioridad (la que sigue) conserva sus rayas; las que llegan cortan sus rayas en su borde
    y llevan línea de ceda el paso (o de detención si hay semáforo o stop); las esquinas se redondean;
  * rayas: borde, separación de carriles (discontinua; ancha en carriles de entrada y salida), eje de las de doble
    sentido; flechas por carril, espaciadas, y las de giro (turn:lanes), las de salida y las de fin de carril.

Todo sale en metros reales (polígonos y líneas con su ancho en «w») listo para teselas.
"""
import math
from collections import defaultdict
import numpy as np
from shapely.geometry import LineString, Polygon, Point, MultiLineString
from shapely.ops import unary_union, linemerge
from shapely.strtree import STRtree
import shapely

BASES = ["motorway", "trunk", "primary", "secondary", "tertiary"]
RANK = {"tertiary": 1, "secondary": 2, "primary": 3, "trunk": 4, "motorway": 5}
LANE_W = {"motorway": 3.5, "trunk": 3.5, "primary": 3.4, "secondary": 3.2, "tertiary": 3.0}
LINK_W = 3.6
TAPER = {"motorway": 120.0, "trunk": 100.0, "primary": 60.0, "secondary": 45.0, "tertiary": 35.0}
DS = 2.0                      # m entre muestras a lo largo del eje

# arcenes (fuera de la línea de borde): (derecho/exterior, izquierdo/interior en sentido único)
SHOULDER = {"motorway": (2.5, 1.0), "trunk": (2.5, 1.0), "primary": (1.5, 0.6), "secondary": (1.0, 0.5),
            "tertiary": (0.5, 0.4)}
SHOULDER_LINK = (1.5, 0.8)
SHOULDER_URBAN = (0.3, 0.3)


def smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def gauss(a, sigma):
    """Suavizado gaussiano de un vector (sigma en muestras), bordes repetidos."""
    if sigma <= 0.5 or len(a) < 3:
        return a.copy()
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    p = np.concatenate([np.full(r, a[0]), a, np.full(r, a[-1])])
    return np.convolve(p, k, mode="valid")


def as_int(v):
    try:
        return int(str(v).split(";")[0].strip())
    except Exception:
        return None


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


# ------------------------------------------------------------------------------------------------------------------
# Datos de OSM
# ------------------------------------------------------------------------------------------------------------------
class Piece:
    """Una vía de OSM de las clases que se dibujan."""

    def __init__(self, wid, refs, t):
        self.wid, self.refs, self.t = wid, list(refs), t
        hw = t["highway"]
        self.link = hw.endswith("_link")
        self.base = hw.replace("_link", "")
        self.rb = t.get("junction") in ("roundabout", "circular")
        ow = t.get("oneway")
        self.oneway = ow in ("yes", "1", "true", "-1") or self.rb or (self.base == "motorway" and ow != "no")
        if ow == "-1":
            self.refs = self.refs[::-1]
        lanes = as_int(t.get("lanes"))
        ms = as_int(t.get("maxspeed"))
        self.urban = ms is not None and ms <= 50
        if self.oneway:
            if lanes is None:
                lanes = 1 if (self.link or self.rb or self.base in ("secondary", "tertiary")) else 2
            self.nf, self.nb = max(1, lanes), 0
            self.tf = split_turns(t.get("turn:lanes"))
            self.tb = None
        else:
            fw, bw = as_int(t.get("lanes:forward")), as_int(t.get("lanes:backward"))
            tot = lanes or 2
            if fw is None and bw is None:
                fw = bw = max(1, tot // 2)
            elif fw is None:
                fw = max(1, tot - bw)
            elif bw is None:
                bw = max(1, tot - fw)
            self.nf, self.nb = fw, bw
            self.tf = split_turns(t.get("turn:lanes:forward"))
            self.tb = split_turns(t.get("turn:lanes:backward"))
        lay = as_int(t.get("layer")) or 0
        self.bridge = t.get("bridge") not in (None, "no")
        self.tunnel = t.get("tunnel") not in (None, "no") or t.get("covered") == "yes"
        self.lv = max(lay, 1) if self.bridge else (min(lay, -1) if self.tunnel else lay)
        self.ref = t.get("ref") or ""
        self.name = t.get("name") or ""
        if self.rb:
            self.lw = 4.5 if self.nf == 1 else 4.0
        else:
            self.lw = LINK_W if self.link else LANE_W[self.base]
        if self.urban and not self.link and self.base in ("secondary", "tertiary", "primary"):
            self.sh = SHOULDER_URBAN
        elif self.rb:
            self.sh = (0.5, 1.0)
        else:
            self.sh = SHOULDER_LINK if self.link else SHOULDER[self.base]


def split_turns(v):
    if not v:
        return None
    return [x.strip() for x in v.split("|")]


# ------------------------------------------------------------------------------------------------------------------
# Cadenas
# ------------------------------------------------------------------------------------------------------------------
class Chain:
    """Calzada continua: una o varias vías seguidas, en un sentido de recorrido del eje."""

    def __init__(self, parts, P):
        # parts: [(piece, reversed)]
        self.parts = parts
        self.oneway = parts[0][0].oneway
        pts, nodes, seg = [], [], []
        for k, (pc, rev) in enumerate(parts):
            refs = pc.refs[::-1] if rev else pc.refs
            if k and nodes and nodes[-1] == refs[0]:
                refs = refs[1:]
            for r in refs:
                pts.append(P[r])
                nodes.append(r)
            seg += [k] * (len(refs) - (1 if k == 0 else 0))
        self.raw = np.array(pts, float)
        self.raw_nodes = nodes
        self.closed = len(nodes) > 3 and nodes[0] == nodes[-1]
        pc0 = parts[0][0]
        self.base, self.link, self.rb = pc0.base, pc0.link, pc0.rb
        self.urban = any(pc.urban for pc, _ in parts)
        self.rank = RANK[self.base] - (0.5 if self.link else 0)

    # --- muestreo ----------------------------------------------------------------------------------------------
    def sample(self, fixed_nodes):
        raw = self.raw
        d = np.hypot(*np.diff(raw, axis=0).T)
        keep = np.concatenate([[True], d > 0.05])
        raw = raw[keep]
        nodes = [n for n, k in zip(self.raw_nodes, keep) if k]
        # parte (índice en parts) de cada vértice: la del tramo que empieza en él
        part_of_vertex = []
        k = 0
        for i, n in enumerate(nodes):
            part_of_vertex.append(k)
            # avanzar de parte cuando el nodo es el final de la vía actual
            pc, rev = self.parts[k]
            last = pc.refs[0] if rev else pc.refs[-1]
            if n == last and k + 1 < len(self.parts) and i > 0:
                k += 1
                part_of_vertex[-1] = k
        cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(raw, axis=0).T))])
        L = cum[-1]
        n = max(2, int(math.ceil(L / DS)) + 1)
        s = np.linspace(0, L, n)
        x = np.interp(s, cum, raw[:, 0])
        y = np.interp(s, cum, raw[:, 1])
        pts = np.stack([x, y], 1)
        # cada vértice de OSM a la muestra más cercana; los nodos de cruce exactos
        vi = np.clip(np.round(cum / (L / (n - 1))).astype(int), 0, n - 1)
        self.node_at = {}
        for j, nd in enumerate(nodes):
            if nd in fixed_nodes or j == 0 or j == len(nodes) - 1:
                pts[vi[j]] = raw[j]
            self.node_at.setdefault(nd, []).append(int(vi[j]))
        # parte de cada muestra
        part = np.zeros(n, int)
        for j in range(len(nodes) - 1):
            part[vi[j]:vi[j + 1] + 1] = part_of_vertex[j]
        self.part = part
        # suavizado del eje (quita esquinas de la digitalización), sin mover los nodos de cruce
        fix = np.zeros(n)
        for nd, idx in self.node_at.items():
            if nd in fixed_nodes:
                for i in idx:
                    fix[i] = 1
        fix[0] = fix[-1] = 1
        dist = np.full(n, 1e9)
        for i in np.nonzero(fix)[0]:
            dist = np.minimum(dist, np.abs(s - s[i]))
        w = smooth(dist / 14.0)
        sm = np.stack([gauss(pts[:, 0], 3.0), gauss(pts[:, 1], 3.0)], 1)
        pts = pts + (sm - pts) * w[:, None]
        if self.rb and self.closed and len(pts) > 12:
            pts = fit_circle(pts)
        self.p = pts
        self.s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
        self.L = self.s[-1]
        t = np.gradient(pts, axis=0)
        t /= np.maximum(1e-9, np.hypot(t[:, 0], t[:, 1]))[:, None]
        t = np.stack([gauss(t[:, 0], 1.5), gauss(t[:, 1], 1.5)], 1)
        t /= np.maximum(1e-9, np.hypot(t[:, 0], t[:, 1]))[:, None]
        self.t = t
        self.nrm = np.stack([-t[:, 1], t[:, 0]], 1)        # a la izquierda
        # atributos por muestra
        N = len(pts)
        self.nf = np.zeros(N)
        self.nb = np.zeros(N)
        self.lw = np.zeros(N)
        self.lv = np.zeros(N, int)
        self.tun = np.zeros(N, bool)
        self.shR = np.zeros(N)
        self.shL = np.zeros(N)
        for i in range(N):
            pc, rev = self.parts[part[i]]
            f, b = (pc.nb, pc.nf) if rev else (pc.nf, pc.nb)
            self.nf[i], self.nb[i], self.lw[i] = f, b, pc.lw
            self.lv[i], self.tun[i] = pc.lv, pc.tunnel
            self.shR[i], self.shL[i] = pc.sh
        self.sh_factorR = np.ones(N)
        self.sh_factorL = np.ones(N)
        self.shift = np.zeros(N)
        self.wf_over = None       # anchos forzados (cuñas de los ramales)
        self.wb_over = None

    def idx(self, node):
        return self.node_at.get(node, [None])[0]

    def heading_at(self, i, ahead=True, dist=35.0):
        """Rumbo medio del eje desde la muestra i hacia delante (o hacia atrás) unos metros."""
        j = int(np.searchsorted(self.s, self.s[i] + dist)) if ahead else int(np.searchsorted(self.s, self.s[i] - dist))
        j = min(max(j, 0), len(self.s) - 1)
        if j == i:
            j = min(i + 1, len(self.s) - 1) if ahead else max(i - 1, 0)
        a, b = (self.p[i], self.p[j]) if ahead else (self.p[j], self.p[i])
        return math.atan2(b[1] - a[1], b[0] - a[0])

    # --- perfil lateral ------------------------------------------------------------------------------------------
    def tapered(self, n):
        """Ancho de los carriles con cuñas donde cambia el número."""
        W = n * self.lw
        out = W.copy()
        Tm = TAPER[self.base] if not self.link else 50.0
        ch = np.nonzero(np.diff(n) != 0)[0]
        bounds = np.concatenate([[0], ch + 1, [len(n)]])
        for k, c in enumerate(ch):
            seg_before = self.s[c] - self.s[bounds[k]]
            seg_after = self.s[bounds[k + 2] - 1] - self.s[c + 1]
            a, b = W[c], W[c + 1]
            if b > a:      # empieza un carril: cuña después del nodo
                T = float(np.clip(0.6 * seg_after, 15, Tm))
                m = (self.s >= self.s[c]) & (self.s <= self.s[c] + T)
                out[m] = a + (b - a) * smooth((self.s[m] - self.s[c]) / T)
            elif b < a:    # se termina: cuña antes del nodo
                T = float(np.clip(0.6 * seg_before, 15, Tm))
                m = (self.s >= self.s[c + 1] - T) & (self.s <= self.s[c + 1])
                out[m] = b + (a - b) * smooth((self.s[c + 1] - self.s[m]) / T)
        return out

    def profile(self):
        sig = 60.0 / DS
        self.Wf = self.tapered(self.nf) if self.wf_over is None else self.wf_over
        if self.oneway:
            nat = self.tapered(self.nf)
            self.inner = 0.5 * gauss(nat, sig) + self.shift           # borde izquierdo (pintado)
            self.outer = self.inner - self.Wf                           # borde derecho (pintado)
            self.C = None
        else:
            self.Wb = self.tapered(self.nb) if self.wb_over is None else self.wb_over
            natf, natb = self.tapered(self.nf), self.tapered(self.nb)
            self.C = 0.5 * (gauss(natb, sig) - gauss(natf, sig)) + self.shift
            self.inner = self.C
            self.outer = self.C - self.Wf                               # borde derecho (sentido del eje)
            self.outerB = self.C + self.Wb                              # borde izquierdo (sentido contrario)

    def halves(self):
        """(sentido, borde interior, borde exterior, carriles, nombre). sentido +1 = el del eje."""
        if self.oneway:
            return [(+1, self.inner, self.outer, self.nf, "f")]
        return [(+1, self.C, self.outer, self.nf, "f"), (-1, self.C, self.outerB, self.nb, "b")]

    def asphalt_edges(self):
        """Bordes del asfalto (izquierdo, derecho) con los arcenes."""
        if self.oneway:
            return self.inner + self.shL * self.sh_factorL, self.outer - self.shR * self.sh_factorR
        return self.outerB + self.shR * self.sh_factorL, self.outer - self.shR * self.sh_factorR

    def line(self, off, i0=0, i1=None):
        i1 = len(self.p) if i1 is None else i1
        return self.p[i0:i1] + self.nrm[i0:i1] * np.asarray(off)[i0:i1, None] if np.ndim(off) else \
            self.p[i0:i1] + self.nrm[i0:i1] * off


def fit_circle(pts):
    """Glorieta: si el anillo es casi un círculo, se dibuja como círculo (sin las ondas de la digitalización)."""
    x, y = pts[:, 0], pts[:, 1]
    A = np.stack([x, y, np.ones_like(x)], 1)
    b = x * x + y * y
    try:
        c = np.linalg.lstsq(A, b, rcond=None)[0]
    except Exception:
        return pts
    cx, cy = c[0] / 2, c[1] / 2
    r = math.sqrt(max(1e-6, c[2] + cx * cx + cy * cy))
    d = np.hypot(x - cx, y - cy)
    if r < 6 or r > 120 or np.sqrt(np.mean((d - r) ** 2)) > 0.12 * r:
        return pts
    ang = np.arctan2(y - cy, x - cx)
    return np.stack([cx + r * np.cos(ang), cy + r * np.sin(ang)], 1)


def clean_offset(pts, t):
    """Quita los bucles de una línea desplazada (en las curvas cerradas, por dentro)."""
    if len(pts) < 3:
        return pts, np.arange(len(pts))
    keep = [0]
    for i in range(1, len(pts)):
        d = pts[i] - pts[keep[-1]]
        if np.dot(d, t[i]) > 0.05 or i == len(pts) - 1:
            keep.append(i)
    keep = np.array(keep)
    return pts[keep], keep


# ------------------------------------------------------------------------------------------------------------------
# Red
# ------------------------------------------------------------------------------------------------------------------
class Network:
    def __init__(self, nodes_ll, node_tags, ways, proj):
        self.proj = proj
        self.P = {}
        self.pieces = []
        used = set()
        for wid, refs, t in ways:
            if t.get("highway", "").replace("_link", "") not in BASES or t.get("area") == "yes":
                continue
            if len(refs) < 2:
                continue
            pc = Piece(wid, refs, t)
            self.pieces.append(pc)
            used.update(refs)
        for r in used:
            self.P[r] = proj(*nodes_ll[r])
        self.node_tags = node_tags
        self.build_chains()

    # --- cadenas -----------------------------------------------------------------------------------------------
    def build_chains(self):
        P = self.P
        touch = defaultdict(list)          # nodo -> [(pieza, 'start'|'end')]
        for pc in self.pieces:
            touch[pc.refs[0]].append((pc, "start"))
            touch[pc.refs[-1]].append((pc, "end"))
        self.touch = touch
        # también los nodos intermedios con otras vías (cruces en medio de una vía)
        inner = defaultdict(list)
        for pc in self.pieces:
            for r in pc.refs[1:-1]:
                inner[r].append(pc)
        self.inner_touch = inner

        def head(pc, at, outward):
            """Rumbo de la pieza en su extremo `at`, mirando hacia fuera del nodo (outward) o hacia el nodo."""
            refs = pc.refs
            pts = [P[r] for r in (refs if at == "start" else refs[::-1])]
            # unos 25 m
            acc, j = 0.0, 1
            while j < len(pts) - 1 and acc < 25:
                acc += math.dist(pts[j - 1], pts[j])
                j += 1
            a, b = pts[0], pts[j]
            h = math.atan2(b[1] - a[1], b[0] - a[0])
            return h if outward else wrap(h + math.pi)

        best = {}
        for node, ports in touch.items():
            if len(ports) < 2:
                continue
            cand = []
            for i, (p, pa) in enumerate(ports):
                for j, (q, qa) in enumerate(ports):
                    if i >= j or p is q:
                        continue
                    if p.oneway != q.oneway:
                        continue
                    if p.oneway:
                        # una entra y la otra sale
                        if pa == "end" and qa == "start":
                            a, b = (p, pa), (q, qa)
                        elif qa == "end" and pa == "start":
                            a, b = (q, qa), (p, pa)
                        else:
                            continue
                    else:
                        a, b = (p, pa), (q, qa)
                    h_in = head(a[0], a[1], False)        # rumbo llegando al nodo
                    h_out = head(b[0], b[1], True)        # rumbo saliendo
                    d = abs(wrap(h_out - h_in))
                    if d > math.radians(50):
                        continue
                    sc = -math.degrees(d)
                    sc += 30 * (a[0].base == b[0].base) + 20 * (a[0].link == b[0].link) + 15 * (a[0].rb == b[0].rb)
                    if (a[0].ref and a[0].ref == b[0].ref) or (a[0].name and a[0].name == b[0].name):
                        sc += 30
                    cand.append((sc, a, b))
            cand.sort(key=lambda c: -c[0])
            taken = set()
            for sc, a, b in cand:
                ka, kb = (id(a[0]), a[1]), (id(b[0]), b[1])
                if ka in taken or kb in taken:
                    continue
                # en sentido único no se encadena si hay otra con la que la llegada se reparte casi igual (desdoble
                # simétrico): lo decide la puntuación; basta con el mejor
                taken.add(ka)
                taken.add(kb)
                best[ka] = b
                best[kb] = a
        self.best = best
        # recorrer
        seen = set()
        chains = []
        for pc in self.pieces:
            if id(pc) in seen:
                continue
            # ir hacia atrás hasta el principio
            cur, rev = pc, False
            guard = 0
            while True:
                start_end = "end" if rev else "start"
                prev = best.get((id(cur), start_end))
                if prev is None or id(prev[0]) == id(pc) and guard > 0:
                    break
                q, qa = prev
                if id(q) in seen:
                    break
                # q llega por su extremo qa; para recorrer hacia delante, q va hacia cur
                cur, rev = q, (qa == "start")
                guard += 1
                if cur is pc or guard > 10000:
                    break
            parts = []
            c, r = cur, rev
            while c is not None and id(c) not in seen:
                seen.add(id(c))
                parts.append((c, r))
                nxt = best.get((id(c), "start" if r else "end"))
                if nxt is None:
                    break
                q, qa = nxt
                c, r = q, (qa == "end")
            chains.append(Chain(parts, P))
        self.chains = chains
        # nodos fijos: los que tocan a más de una cadena o son extremos
        cnt = defaultdict(set)
        for k, ch in enumerate(chains):
            for nd in ch.raw_nodes:
                cnt[nd].add(k)
        self.node_chains = cnt
        fixed = {nd for nd, ks in cnt.items() if len(ks) > 1}
        for ch in chains:
            fixed.add(ch.raw_nodes[0])
            fixed.add(ch.raw_nodes[-1])
        self.fixed = fixed
        for ch in chains:
            ch.sample(fixed)

    # --- uniones -------------------------------------------------------------------------------------------------
    def relations(self):
        """Clasifica cada nodo con varias cadenas: desdoble/unión (pegadas) o cruce a nivel."""
        self.glued = set()           # pares (id cadena a, id cadena b) que no se cortan entre sí
        self.glue_jobs = []
        self.atgrade = []            # (nodo, [(cadena, i, pasa)])
        for nd, ks in self.node_chains.items():
            if len(ks) < 2:
                continue
            items = []
            for k in ks:
                ch = self.chains[k]
                for i in ch.node_at.get(nd, []):
                    passes = 0 < i < len(ch.p) - 1 or (ch.closed and i in (0, len(ch.p) - 1))
                    items.append((ch, i, passes))
            self.classify(nd, items)

    def classify(self, nd, items):
        thr = math.radians(40)
        through = [(c, i) for c, i, ps in items if ps]
        ends = [(c, i) for c, i, ps in items if not ps]
        handled = set()
        # 1) ramal que sale de / llega a una calzada que sigue (sentido único con sentido único)
        for (m, mi) in through:
            if not m.oneway:
                continue
            # pegado solo en vías rápidas (en ciudad, un ramal es un cruce más: la calzada conserva sus rayas)
            fast_main = m.base in ("motorway", "trunk") or m.link
            hm_out = m.heading_at(mi, True)
            hm_in = m.heading_at(mi, False)
            for (b, bi) in ends:
                if not b.oneway or id(b) in handled:
                    continue
                if not (fast_main or (m.base == "primary" and b.link and not m.urban)):
                    continue
                if bi == 0:          # sale del nodo: desdoble
                    hb = b.heading_at(0, True)
                    d = wrap(hb - hm_out)
                    if abs(d) < thr and m.lv[mi] == b.lv[0]:
                        self.glue_jobs.append(("branch", m, mi, b, "start", "L" if d > 0 else "R"))
                        self.glued.add((id(m), id(b)))
                        handled.add(id(b))
                else:                # llega al nodo: unión
                    hb = b.heading_at(bi, False)
                    d = wrap(hm_in - hb)
                    if abs(d) < thr and m.lv[mi] == b.lv[bi]:
                        # lado: dónde queda el ramal respecto a la calzada, mirando hacia atrás
                        side = "L" if wrap(b.heading_at(bi, False) - hm_in) < 0 else "R"
                        q = b.p[max(0, bi - 15)] - m.p[mi]
                        side = "L" if np.dot(q, m.nrm[mi]) > 0 else "R"
                        self.glue_jobs.append(("branch", m, mi, b, "end", side))
                        self.glued.add((id(m), id(b)))
                        handled.add(id(b))
        # 2) desdoble sin calzada que siga: una llega y varias salen (o al revés), o doble sentido que se parte
        rest = [(c, i) for c, i in ends if id(c) not in handled]
        if len(rest) >= 2:
            twos = [(c, i) for c, i in rest if not c.oneway]
            ones = [(c, i) for c, i in rest if c.oneway]
            ins = [(c, i) for c, i in ones if i == len(c.p) - 1]
            outs = [(c, i) for c, i in ones if i == 0]
            if len(twos) == 1 and ins and outs and not through:
                t, ti = twos[0]
                ht = out_heading(t, ti)               # mirando hacia fuera del nodo por la doble
                okk = []
                for c, i in ins + outs:
                    hc = out_heading(c, i)
                    if abs(wrap(hc - ht - math.pi)) < math.radians(50):
                        okk.append((c, i))
                if len(okk) >= 2:
                    self.glue_jobs.append(("dual", t, ti, okk))
                    for c, i in okk:
                        self.glued.add((id(t), id(c)))
                        handled.add(id(c))
                    for a, _ in okk:
                        for b, _ in okk:
                            if a is not b:
                                self.glued.add((id(a), id(b)))
                    handled.add(id(t))
            elif not twos and len(ins) == 1 and len(outs) >= 2 and not through:
                c, ci = ins[0]
                hc = c.heading_at(ci, False)
                ok = [(o, oi) for o, oi in outs if abs(wrap(o.heading_at(0, True) - hc)) < thr]
                if len(ok) >= 2:
                    self.glue_jobs.append(("split", c, ci, ok, "start"))
                    for o, _ in ok:
                        self.glued.add((id(c), id(o)))
                        handled.add(id(o))
                    for a, _ in ok:
                        for b, _ in ok:
                            if a is not b:
                                self.glued.add((id(a), id(b)))
                    handled.add(id(c))
            elif not twos and len(outs) == 1 and len(ins) >= 2 and not through:
                c, ci = outs[0]
                hc = c.heading_at(0, True)
                ok = [(o, oi) for o, oi in ins if abs(wrap(hc - o.heading_at(oi, False))) < thr]
                if len(ok) >= 2:
                    self.glue_jobs.append(("split", c, ci, ok, "end"))
                    for o, _ in ok:
                        self.glued.add((id(c), id(o)))
                        handled.add(id(o))
                    for a, _ in ok:
                        for b, _ in ok:
                            if a is not b:
                                self.glued.add((id(a), id(b)))
                    handled.add(id(c))
        # 3) lo demás es un cruce a nivel
        others = [(c, i, ps) for c, i, ps in items if id(c) not in handled or ps]
        if len({id(c) for c, _, _ in others}) >= 2:
            self.atgrade.append((nd, others))

    # --- pegado de ramales ---------------------------------------------------------------------------------------
    def glue(self):
        for ch in self.chains:
            ch.profile()
        for _ in range(2):
            for ch in self.chains:
                ch.shift[:] = 0
                ch.wf_over = None
                ch.sh_factorR[:] = 1
                ch.sh_factorL[:] = 1
            # anchos de los ramales primero (cuñas), luego posiciones
            for job in self.glue_jobs:
                if job[0] == "branch":
                    self.branch_width(*job[1:])
            for ch in self.chains:
                ch.profile()
            for job in self.glue_jobs:
                if job[0] == "branch":
                    self.glue_branch(*job[1:])
                elif job[0] == "split":
                    self.glue_split(*job[1:])
                else:
                    self.glue_dual(*job[1:])
            for ch in self.chains:
                ch.profile()
        for job in self.glue_jobs:
            if job[0] == "branch":
                self.shoulders(job[1], job[3], job[4], job[5])

    def branch_width(self, m, mi, b, at, side):
        """Ancho del ramal al pegarse: sus carriles que vienen de la calzada; los demás salen de cero (cuña)."""
        if at == "start":
            nb_lanes = b.nf[0]
            before, after = m.nf[max(mi - 1, 0)], m.nf[min(mi + 1, len(m.nf) - 1)]
            peeled = max(0, before - after)
        else:
            nb_lanes = b.nf[-1]
            before, after = m.nf[max(mi - 1, 0)], m.nf[min(mi + 1, len(m.nf) - 1)]
            peeled = max(0, after - before)
        k0 = min(nb_lanes, peeled)
        if k0 >= nb_lanes:
            return
        T = 90.0 if m.base in ("motorway", "trunk") else 45.0
        W = b.tapered(b.nf).copy() if b.wf_over is None else b.wf_over
        d = b.s if at == "start" else b.L - b.s
        T = min(T, 0.7 * b.L)
        f = smooth(d / T)
        full = b.nf * b.lw
        W = np.where(d < T, (k0 * b.lw) + (full - k0 * b.lw) * f, W)
        b.wf_over = W

    def edge_line(self, m, side, i0, i1):
        off = m.outer if side == "R" else m.inner
        i0, i1 = max(0, i0), min(len(m.p), i1)
        pts = m.p[i0:i1] + m.nrm[i0:i1] * off[i0:i1, None]
        return pts, m.t[i0:i1]

    def glue_branch(self, m, mi, b, at, side):
        """Pega el ramal a la calzada: su borde de ese lado sobre el borde de la calzada y se va separando."""
        span = int(500 / DS)
        if at == "start":
            E, Et = self.edge_line(m, side, mi - 5, mi + span)
            idx = np.arange(0, min(len(b.p), int(450 / DS)))
        else:
            E, Et = self.edge_line(m, side, mi - span, mi + 6)
            idx = np.arange(len(b.p) - 1, max(-1, len(b.p) - 1 - int(450 / DS)), -1)
        if len(E) < 2 or len(idx) < 2:
            return
        # como mucho media calzada (un ramal corto puede ir pegado por los dos extremos)
        half = b.L / 2
        dd = b.s[idx] if at == "start" else b.L - b.s[idx]
        idx = idx[dd <= max(half, 20.0)]
        if len(idx) < 2:
            return
        face = b.inner if side == "R" else b.outer
        q = b.p[idx] + b.nrm[idx] * face[idx, None]
        # distancia con signo al borde (positiva = hacia fuera de la calzada)
        signed, et = signed_dist(q, E, Et, tangents=True)
        gap = -signed if side == "R" else signed
        dist = b.s[idx] if at == "start" else b.L - b.s[idx]
        R = 70.0 if m.base in ("motorway", "trunk") else 35.0
        ramp = smooth(dist / R)
        g = np.maximum(0, gap) * ramp
        delta = g - gap
        # deja de mirar cuando el ramal ya se ha ido lejos o gira y deja de ir junto a la calzada
        tb = b.t[idx]
        cosang = np.abs(np.sum(tb * et, axis=1))
        away = (gap > 18) | (cosang < math.cos(math.radians(35)))
        far = np.nonzero(away)[0]
        if len(far):
            cut = far[0]
            fade = smooth(1 - (np.arange(len(idx)) - cut) / 10.0)
            delta = np.where(np.arange(len(idx)) >= cut, delta * fade, delta)
        delta = np.where(np.abs(delta) > 25, 0, delta)
        if len(delta) > 8:
            d0 = delta[0]
            delta = gauss(delta, 2.5)
            delta[0] = d0
        # sobre el eje del ramal: hacia fuera de la calzada = a su derecha si va a la derecha
        sh = -delta if side == "R" else delta
        b.shift[idx] += sh

    def glue_split(self, c, ci, outs, at):
        """Calzada que se parte en varias (o varias que se juntan en una): cada una con sus carriles en su sitio."""
        H = []
        for o, oi in outs:
            j = 0 if at == "start" else len(o.p) - 1
            v = o.p[min(j + 10, len(o.p) - 1)] - o.p[j] if at == "start" else o.p[max(j - 10, 0)] - o.p[j]
            # de izquierda a derecha según el sentido de la marcha de la calzada única
            H.append((float(np.dot(v, c.nrm[ci])), o, oi))
        H.sort(key=lambda h: -h[0])
        xs = c.inner[ci]
        left_edge = c.inner[ci]
        lwc = c.lw[ci]
        nc = c.nf[ci]
        used = 0
        for k, (_, o, oi) in enumerate(H):
            j = 0 if at == "start" else len(o.p) - 1
            n_o = o.nf[j]
            give = max(0, min(n_o, nc - used))
            target_inner = left_edge - used * lwc
            used += n_o
            # el eje del ramal sale del mismo nodo: su borde interior debe estar en target_inner
            cur = o.inner[j] - o.shift[j]
            off0 = target_inner - cur
            # cuña si le faltan carriles
            d = o.s if at == "start" else o.L - o.s
            if give < n_o:
                T = min(90.0, 0.7 * o.L)
                full = o.nf * o.lw
                o.wf_over = np.where(d < T, give * o.lw + (full - give * o.lw) * smooth(d / T),
                                     o.tapered(o.nf))
            D = self.separation_length(o, at, [h[1] for h in H if h[1] is not o])
            o.shift += off0 * (1 - smooth(d / D))

    def separation_length(self, o, at, others):
        """Lo que tardan los ejes en separarse lo bastante (para soltar el pegado)."""
        if not others:
            return 40.0
        lines = [LineString(x.p) for x in others if len(x.p) >= 2]
        if not lines:
            return 40.0
        u = unary_union(lines)
        rng = range(0, len(o.p)) if at == "start" else range(len(o.p) - 1, -1, -1)
        need = float(o.nf.max() * o.lw.max()) + 3.0
        for i in rng:
            d = o.s[i] if at == "start" else o.L - o.s[i]
            if d > 250:
                break
            if Point(o.p[i]).distance(u) > need:
                return float(np.clip(d * 1.1, 15, 250))
        return 250.0

    def glue_dual(self, t, ti, ones):
        """Doble sentido que se parte en dos calzadas de sentido único (o al revés): cada una en su mitad."""
        dir_t = t.t[ti]
        for c, ci in ones:
            sig = 1.0 if np.dot(c.t[ci], dir_t) > 0 else -1.0
            target = sig * t.C[ti]
            cur = c.inner[ci] - c.shift[ci]
            off0 = target - cur
            at = "start" if ci == 0 else "end"
            d = c.s if at == "start" else c.L - c.s
            half_n = t.nf[ti] if sig > 0 else t.nb[ti]
            n_c = c.nf[ci]
            if half_n < n_c:
                T = min(60.0, 0.7 * c.L)
                full = c.nf * c.lw
                c.wf_over = np.where(d < T, half_n * c.lw + (full - half_n * c.lw) * smooth(d / T),
                                     c.tapered(c.nf))
            D = self.separation_length(c, at, [x for x, _ in ones if x is not c])
            c.shift += off0 * (1 - smooth(d / D))

    def shoulders(self, m, b, at, side):
        """Sin arcén entre una calzada y el ramal pegado a ella mientras van juntos."""
        if len(b.p) < 2:
            return
        face_b = b.inner if side == "R" else b.outer
        bl = LineString(b.p + b.nrm * face_b[:, None])
        face_m = m.outer if side == "R" else m.inner
        ml = LineString(m.p + m.nrm * face_m[:, None])
        env = bl.buffer(30)
        for ch, edge, fac in ((m, face_m, m.sh_factorR if side == "R" else m.sh_factorL),
                              (b, face_b, b.sh_factorL if side == "R" else b.sh_factorR)):
            other = bl if ch is m else ml
            pts = ch.p + ch.nrm * edge[:, None]
            sel = np.ones(len(pts), bool)
            if ch is m:
                sel = shapely.contains_xy(env, pts[:, 0], pts[:, 1])
            if not sel.any():
                continue
            d = shapely.distance(other, shapely.points(pts[sel]))
            fac[sel] = np.minimum(fac[sel], smooth((d - 0.3) / 3.0))

    # --- geometría -----------------------------------------------------------------------------------------------
    def build(self):
        self.relations()
        self.glue()
        out = Output()
        chains = self.chains
        # superficies por cadena y nivel
        lane_polys = []          # (polígono del área de carriles de una mitad, cadena, sentido)
        asph = []                # (polígono, cadena, nivel, túnel)
        for ch in chains:
            Lft, Rgt = ch.asphalt_edges()
            for i0, i1, lv, tun in runs(ch):
                poly = ribbon(ch, Lft, Rgt, i0, i1, ext=1.0)
                if poly is not None:
                    asph.append((poly, ch, lv, tun))
            for sg, inn, outr, n, name in ch.halves():
                poly = ribbon(ch, np.maximum(inn, outr), np.minimum(inn, outr), 0, len(ch.p), ext=0.0)
                if poly is not None:
                    lane_polys.append((poly, ch, sg))
        self.asph = asph
        # cortes en los cruces a nivel
        cut_all = defaultdict(list)      # cadena -> polígonos que le cortan todo
        cut_edge = defaultdict(list)     # cadena -> polígonos que solo le cortan los bordes
        priority_hits = defaultdict(list)  # cadena que llega -> (polígono de la prioritaria, nodo)
        by_chain = defaultdict(list)
        for poly, ch, lv, tun in asph:
            by_chain[id(ch)].append((poly, lv))
        self.junction_nodes = []
        for nd, items in self.atgrade:
            pt = Point(self.P[nd])
            disk = pt.buffer(70)
            for a, ai, apass in items:
                for b, bi, bpass in items:
                    if a is b or (id(a), id(b)) in self.glued or (id(b), id(a)) in self.glued:
                        continue
                    if a.lv[ai] != b.lv[bi]:
                        continue
                    bp = [p for p, lv in by_chain[id(b)] if lv == b.lv[bi]]
                    if not bp:
                        continue
                    bpoly = unary_union(bp).intersection(disk)
                    cut_edge[id(a)].append(bpoly)
                    if apass and not bpass:
                        continue        # a tiene prioridad: conserva sus rayas
                    cut_all[id(a)].append(bpoly)
                    if bpass and not apass:
                        priority_hits[id(a)].append((bpoly, nd, ai))
            self.junction_nodes.append((nd, items))
        # calzadas que se cruzan sin nodo común al mismo nivel (giros dentro de un cruce, datos sin unir): la más
        # importante conserva sus rayas; la otra las corta (y si son iguales, las dos)
        tree_a = STRtree([a[0] for a in asph])
        connected = set()
        for nd, ks in self.node_chains.items():
            ks = list(ks)
            for x in ks:
                for y in ks:
                    connected.add((id(self.chains[x]), id(self.chains[y])))
        for k, (poly, ch, lv, tun) in enumerate(asph):
            for j in tree_a.query(poly, predicate="intersects"):
                op, oc, olv, otun = asph[j]
                if oc is ch or olv != lv or (id(ch), id(oc)) in connected or (id(ch), id(oc)) in self.glued \
                        or (id(oc), id(ch)) in self.glued:
                    continue
                inter = poly.intersection(op)
                if inter.is_empty or inter.area < 2:
                    continue
                c = np.array(inter.centroid.coords[0])
                ja = int(np.argmin(np.hypot(*(ch.p - c).T)))
                jb = int(np.argmin(np.hypot(*(oc.p - c).T)))
                if abs(float(np.dot(ch.t[ja], oc.t[jb]))) > math.cos(math.radians(30)):
                    continue        # van en paralelo: no es un cruce
                cut_edge[id(ch)].append(op)
                if oc.rank >= ch.rank:
                    cut_all[id(ch)].append(op)
        # un ramal pegado no pinta nada dentro de los carriles de la calzada de la que sale o a la que llega
        for job in self.glue_jobs:
            if job[0] != "branch":
                continue
            m, b = job[1], job[3]
            mp = [lp[0] for lp in lane_polys if lp[1] is m]
            if mp:
                inside = unary_union(mp).buffer(-0.3)
                if not inside.is_empty:
                    cut_all[id(b)].append(inside)
        # rayas
        lane_tree = STRtree([lp[0] for lp in lane_polys])
        for ch in chains:
            ca = unary_union(cut_all[id(ch)]).buffer(0.4) if cut_all[id(ch)] else None
            ce = unary_union(cut_edge[id(ch)] + cut_all[id(ch)]).buffer(0.4) if cut_edge[id(ch)] or cut_all[id(ch)] else None
            self.markings(ch, ca, ce, lane_polys, lane_tree, out)
            self.yield_lines(ch, priority_hits[id(ch)], out)
        self.gores(out)
        # asfalto: unión por nivel y esquinas redondeadas en los cruces a nivel
        levels = defaultdict(list)
        for poly, ch, lv, tun in asph:
            levels[(lv, tun)].append((poly, ch))
        trans_zones = []
        for ch in chains:
            rr = runs(ch)
            for (a0, a1, l0, t0), (b0, b1, l1, t1) in zip(rr, rr[1:]):
                i = b0
                Lft, Rgt = ch.asphalt_edges()
                r = abs(Lft[i] - Rgt[i]) / 2 + 3
                trans_zones.append(Point(ch.p[i] + ch.nrm[i] * (Lft[i] + Rgt[i]) / 2).buffer(r))
        for nd, ks in self.node_chains.items():
            lvs = set()
            for k in ks:
                ch = self.chains[k]
                for i in ch.node_at.get(nd, []):
                    lvs.add((int(ch.lv[i]), bool(ch.tun[i])))
            if len(lvs) > 1:
                trans_zones.append(Point(self.P[nd]).buffer(14))
        for (lv, tun), items in sorted(levels.items()):
            polys_lv = [p for p, _ in items]
            u = unary_union(polys_lv)
            # cierra las rendijas entre calzadas pegadas (sin rayas de contorno dentro del asfalto)
            u = u.buffer(0.35, join_style=2).buffer(-0.35, join_style=2)
            fl = self.fillets(polys_lv, lv)
            fill = unary_union([f for f, _ in fl]) if fl else None
            full = u if fill is None else unary_union([u, fill]).buffer(0.05).buffer(-0.05)
            out.add_poly(full, "asfalto", lv=lv, tu=int(tun))
            # contorno del asfalto (bordillo), sin cerrar los cambios de nivel
            # el contorno solo se abre donde una calzada cambia de nivel (principio y final de un puente),
            # no donde otra pasa por debajo
            others = []
            for zone in trans_zones:
                others.append(zone)
            bnd = full.boundary
            if others:
                other_lv = unary_union([p for (l2, t2), it2 in levels.items() if (l2, t2) != (lv, tun) for p, _ in it2])
                near = unary_union(others).intersection(other_lv.buffer(0.6))
                bnd = bnd.difference(near)
            out.add_line(bnd, "contorno", lv=lv, tu=int(tun), w=0.25)
        return out

    def fillets(self, polys, lv):
        pieces = []
        tree = STRtree(polys)
        for nd, items in self.junction_nodes:
            if not any(c.lv[i] == lv for c, i, _ in items):
                continue
            base = max(items, key=lambda x: x[0].rank)[0]
            low = min(items, key=lambda x: x[0].rank)[0]
            r = 3.5
            disk = Point(self.P[nd]).buffer(28)
            near = [polys[k] for k in tree.query(disk, predicate="intersects")]
            if not near:
                continue
            local = unary_union(near).intersection(disk)
            if local.is_empty:
                continue
            closed = local.buffer(r, join_style=1).buffer(-r, join_style=1)
            add = closed.difference(local).intersection(Point(self.P[nd]).buffer(28 - r))
            if not add.is_empty and add.area > 0.5:
                pieces.append((add, low))
        return pieces

    def markings(self, ch, ca, ce, lane_polys, lane_tree, out):
        N = len(ch.p)
        cls = ch.base
        fast = cls in ("motorway", "trunk") or ch.link
        on, off = (3.5, 9.0) if (fast or cls in ("primary",)) else (2.5, 5.5)
        w_lane = 0.15
        w_edge = 0.20 if fast else 0.15
        for i0, i1, lv, tun in runs(ch):
            if tun:
                continue
            for sg, inn, outr, n, name in ch.halves():
                lw = ch.lw
                # bordes: exterior siempre; interior solo en sentido único (en doble sentido es el eje)
                edges = [("outer", outr)]
                if ch.oneway:
                    edges.append(("inner", inn))
                for which, off_e in edges:
                    pts = ch.p[i0:i1] + ch.nrm[i0:i1] * off_e[i0:i1, None]
                    outward = -sg if which == "outer" else sg      # signo de la normal hacia fuera del área
                    if which == "inner" and ch.oneway:
                        outward = +1
                    if which == "outer" and ch.oneway:
                        outward = -1
                    if not ch.oneway:
                        outward = -1 if sg > 0 else +1
                    probe = pts + ch.nrm[i0:i1] * (0.6 * outward)
                    kind = classify_edge(ch, sg, probe, ch.t[i0:i1] * sg, lane_polys, lane_tree)
                    # kind: 0 borde, 1 separador (lo pinta esta), 2 separador (lo pinta la otra), 3 ancha
                    run_lines(pts, ch.t[i0:i1], kind, which, out, ce, lv, on, off, w_lane, w_edge, cls)
                # rayas entre carriles
                nmax = int(n[i0:i1].max()) if i1 > i0 else 0
                for k in range(1, nmax):
                    pos = inn[i0:i1] - sg * k * lw[i0:i1]
                    valid = (np.abs(pos - outr[i0:i1]) >= 0.55 * lw[i0:i1]) & \
                            ((pos - outr[i0:i1]) * sg > 0)
                    pts = ch.p[i0:i1] + ch.nrm[i0:i1] * pos[:, None]
                    wide = self.wide_mask(ch, sg, k, i0, i1)
                    segs = split_mask(pts, valid)
                    for seg, a, b in segs:
                        wm = wide[a:b]
                        for sub, wflag in split_by(seg, wm):
                            ls = LineString(sub)
                            if ca is not None:
                                ls = ls.difference(ca)
                            if wflag:
                                dashed(ls, 1.0, 1.0, out, "raya", lv=lv, w=0.40, c=cls)
                            else:
                                dashed(ls, on, off, out, "raya", lv=lv, w=w_lane, c=cls)
            # eje de las de doble sentido
            if not ch.oneway:
                pts = ch.p[i0:i1] + ch.nrm[i0:i1] * ch.C[i0:i1, None]
                ls = LineString(pts) if len(pts) >= 2 else None
                if ls is not None:
                    if ca is not None:
                        ls = ls.difference(ca)
                    tot = ch.nf[i0:i1].max() + ch.nb[i0:i1].max()
                    if tot >= 4:
                        out.add_line(ls, "eje", lv=lv, w=0.15, c=cls)
                    else:
                        dashed(ls, on, off, out, "eje", lv=lv, w=0.15, c=cls)
            self.arrows(ch, i0, i1, lv, ca, out)

    def wide_mask(self, ch, sg, k, i0, i1):
        """Raya ancha: la que separa un carril de entrada o salida (el que se acaba o el que viene de un ramal)."""
        m = np.zeros(i1 - i0, bool)
        if not ch.oneway or ch.link:
            return m
        n = ch.nf
        # tramos donde el carril k (0 = izquierda) es el de más a la derecha y desaparece o aparece con un ramal
        for job in self.glue_jobs:
            if job[0] != "branch" or job[1] is not ch:
                continue
            _, m_, mi, b, at, side = job
            if side != "R":
                continue
            before, after = n[max(mi - 1, 0)], n[min(mi + 1, len(n) - 1)]
            if at == "start" and before > after and k == after:
                # carril de salida: desde donde aparece (o 600 m) hasta el nodo
                j = mi
                while j > 0 and n[j - 1] >= before and ch.s[mi] - ch.s[j] < 600:
                    j -= 1
                lo, hi = j, mi
            elif at == "end" and after > before and k == before:
                j = mi
                while j < len(n) - 1 and n[j + 1] >= after and ch.s[j] - ch.s[mi] < 600:
                    j += 1
                lo, hi = mi, j
            else:
                continue
            a, b2 = max(lo, i0), min(hi + 1, i1)
            if b2 > a:
                m[a - i0:b2 - i0] = True
        return m

    def yield_lines(self, ch, hits, out):
        """Línea de ceda el paso (o de detención) donde la vía que llega entra en la que tiene prioridad."""
        for poly, nd, i in hits:
            if i == 0:
                rng, sgs = range(0, min(len(ch.p), 60)), (-1,)       # el sentido que va hacia el nodo
            else:
                rng, sgs = range(len(ch.p) - 1, max(-1, len(ch.p) - 61), -1), (+1,)
            if ch.oneway:
                if i == 0:
                    continue          # sale del cruce: no hay línea
                sgs = (+1,)
            j_edge = None
            for j in rng:
                if not poly.buffer(0.3).contains(Point(ch.p[j])):
                    j_edge = j
                    break
            if j_edge is None:
                continue
            # un poco antes del borde
            step = 1 if i == 0 else -1
            j = j_edge + step * 1
            j = min(max(j, 0), len(ch.p) - 1)
            stop = self.has_signal(nd)
            for sg in sgs:
                h = [hh for hh in ch.halves() if hh[0] == sg]
                if not h:
                    continue
                _, inn, outr, n, _ = h[0]
                a = ch.p[j] + ch.nrm[j] * inn[j]
                b = ch.p[j] + ch.nrm[j] * outr[j]
                ls = LineString([a, b])
                if stop:
                    out.add_line(ls, "detencion", lv=int(ch.lv[j]), w=0.40, c=ch.base)
                else:
                    dashed(ls, 0.8, 0.4, out, "ceda", lv=int(ch.lv[j]), w=0.40, c=ch.base)

    def has_signal(self, nd):
        t = self.node_tags.get(nd, {})
        return t.get("highway") in ("traffic_signals", "stop") or t.get("crossing") == "traffic_signals"

    # --- flechas -------------------------------------------------------------------------------------------------
    def arrows(self, ch, i0, i1, lv, ca, out):
        fast = ch.base in ("motorway", "trunk")
        AL = 7.5 if (fast or ch.link) else 5.0
        for sg, inn, outr, n, name in ch.halves():
            if sg > 0:
                idxs = range(i0, i1)
            else:
                idxs = range(i1 - 1, i0 - 1, -1)
            # posiciones en el sentido de la marcha
            sl = ch.s if sg > 0 else ch.L - ch.s
            # puntos donde termina cada vía con giros (antes del cruce)
            placed = []
            turns_by_part = {}
            for k, (pc, rev) in enumerate(ch.parts):
                tt = (pc.tb if not rev else pc.tf) if sg < 0 else (pc.tf if not rev else pc.tb)
                if not ch.oneway and sg < 0:
                    tt = pc.tf if rev else pc.tb
                elif not ch.oneway:
                    tt = pc.tb if rev else pc.tf
                else:
                    tt = pc.tf
                turns_by_part[k] = tt
            # final de cada parte en el sentido de la marcha
            parts_idx = defaultdict(list)
            for i in range(i0, i1):
                parts_idx[int(ch.part[i])].append(i)
            for k, ii in parts_idx.items():
                tt = turns_by_part.get(k)
                if not tt or len(tt) < 1:
                    continue
                end_i = ii[-1] if sg > 0 else ii[0]
                ln = ch.s[ii[-1]] - ch.s[ii[0]]
                for back in (22.0, 60.0):
                    if back + AL > ln - 5:
                        continue
                    target = sl[end_i] - back
                    j = nearest_s(sl, target, ii)
                    if j is None:
                        continue
                    self.place_lane_arrows(ch, j, sg, inn, outr, n, tt, AL, ca, lv, out)
                    placed.append(sl[j])
            # flechas de selección en calzadas de 2 o más carriles, cada ~250 m (en ciudad, una por tramo)
            if n[i0:i1].max() >= 2:
                every = 260.0 if (fast or ch.base == "primary") else 140.0
                s0, s1 = min(sl[i0], sl[i1 - 1]), max(sl[i0], sl[i1 - 1])
                L = s1 - s0
                cnt = int(L // every)
                for q in range(cnt):
                    target = s0 + (q + 0.5) * (L / cnt) if cnt else s0 + L / 2
                    if any(abs(target - p) < 90 for p in placed):
                        continue
                    j = nearest_s(sl, target, range(i0, i1))
                    if j is None or not self.straight(ch, j) or self.near_change(ch, j, sg):
                        continue
                    self.place_lane_arrows(ch, j, sg, inn, outr, n, None, AL, ca, lv, out)
                    placed.append(target)
            # salida y fin de carril
            self.exit_arrows(ch, sg, inn, outr, n, sl, AL, ca, lv, out, i0, i1)

    def straight(self, ch, j):
        a = ch.heading_at(j, True, 25)
        b = ch.heading_at(j, False, 25)
        return abs(wrap(a - b)) < math.radians(12)

    def near_change(self, ch, j, sg):
        lo, hi = max(0, j - 30), min(len(ch.p), j + 30)
        return bool(np.any(np.diff(ch.nf[lo:hi]) != 0) or np.any(np.abs(np.diff(ch.shift[lo:hi])) > 0.05) or
                    (ch.wf_over is not None and np.any(np.abs(ch.wf_over[lo:hi] - ch.nf[lo:hi] * ch.lw[lo:hi]) > 0.2)))

    def place_lane_arrows(self, ch, j, sg, inn, outr, n, turns, AL, ca, lv, out):
        width = abs(inn[j] - outr[j])
        lw = ch.lw[j]
        k_here = int(round(width / lw))
        d = ch.t[j] * sg
        for k in range(k_here):
            pos = inn[j] - sg * (k + 0.5) * lw
            if (pos - outr[j]) * sg < 0.3 * lw:
                continue
            c = ch.p[j] + ch.nrm[j] * pos
            kind = "through"
            if turns and k < len(turns):
                kind = arrow_kind(turns[k])
                if kind is None:
                    continue
            poly = arrow_poly(c, d, kind, AL)
            if ca is not None and poly.intersects(ca):
                continue
            out.add_poly(poly, "flecha", lv=lv, c=ch.base)

    def exit_arrows(self, ch, sg, inn, outr, n, sl, AL, ca, lv, out, i0, i1):
        if not ch.oneway:
            return
        for job in self.glue_jobs:
            if job[0] != "branch" or job[1] is not ch:
                continue
            _, m, mi, b, at, side = job
            if at != "start" or not (i0 <= mi < i1):
                continue
            before, after = ch.nf[max(mi - 1, 0)], ch.nf[min(mi + 1, len(ch.nf) - 1)]
            if before <= after:
                continue
            kind = "slight_right" if side == "R" else "slight_left"
            for back in (45.0, 130.0):
                j = nearest_s(sl, sl[mi] - back, range(i0, i1))
                if j is None or ch.nf[j] < before:
                    continue
                lw = ch.lw[j]
                for k in range(int(after), int(before)) if side == "R" else range(0, int(before - after)):
                    pos = inn[j] - (k + 0.5) * lw
                    c = ch.p[j] + ch.nrm[j] * pos
                    poly = arrow_poly(c, ch.t[j], kind, AL)
                    if ca is None or not poly.intersects(ca):
                        out.add_poly(poly, "flecha", lv=lv, c=ch.base)
        # fin de carril sin ramal: flecha de retorno
        n_ = ch.nf
        ch_idx = np.nonzero(np.diff(n_) < 0)[0]
        for c in ch_idx:
            node_i = c + 1
            if any(job[0] == "branch" and job[1] is ch and job[2] in (c, c + 1, c + 2) for job in self.glue_jobs):
                continue
            Tm = TAPER[ch.base] if not ch.link else 50.0
            for back in (Tm + 40, Tm + 110, Tm + 180):
                j = nearest_s(ch.s, ch.s[node_i] - back, range(i0, i1))
                if j is None or n_[j] != n_[c]:
                    continue
                lw = ch.lw[j]
                pos = inn[j] - (n_[c] - 0.5) * lw
                cc = ch.p[j] + ch.nrm[j] * pos
                poly = arrow_poly(cc, ch.t[j], "merge_left", AL)
                if ca is None or not poly.intersects(ca):
                    out.add_poly(poly, "flecha", lv=lv, c=ch.base)

    # --- cebreados -----------------------------------------------------------------------------------------------
    def gores(self, out):
        for job in self.glue_jobs:
            if job[0] == "branch":
                _, m, mi, b, at, side = job
                self.gore_between(m, "R" if side == "R" else "L", b, "L" if side == "R" else "R", at, m.base, out=out)
            elif job[0] == "split":
                _, c, ci, outs, at = job
                for (o1, _), (o2, _) in zip(outs, outs[1:]):
                    a, bb = order_lr(c, ci, o1, o2, at)
                    self.gore_between(a, "R", bb, "L", at, c.base, both_branches=True, out=out)
            else:
                _, t, ti, ones = job
                if len(ones) == 2:
                    (a, ai), (bb, bi) = ones
                    self.gore_between(a, "L", bb, "L", "start" if ai == 0 else "end", t.base, dual=True, out=out)

    def gore_between(self, m, mside, b, bside, at, base, both_branches=False, dual=False, out=None):
        gmax = 7.0 if base in ("motorway", "trunk") else (3.5 if base == "primary" else 2.5)
        if dual:
            gmax = 2.5
        eb = (b.inner if bside == "L" else b.outer)
        bl = b.p + b.nrm * eb[:, None]
        em = (m.inner if mside == "L" else m.outer)
        ml = LineString(m.p + m.nrm * em[:, None])
        rng = range(0, len(bl)) if at == "start" else range(len(bl) - 1, -1, -1)
        side_pts, opp_pts = [], []
        started = False
        maxlen = 120.0 if base in ("motorway", "trunk") else 60.0
        s_start = None
        for i in rng:
            q = Point(bl[i])
            d = ml.distance(q)
            dist = b.s[i] if at == "start" else b.L - b.s[i]
            if dist > 350:
                break
            if d < 0.15 and not started:
                side_pts, opp_pts = [bl[i]], [np.array(ml.interpolate(ml.project(q)).coords[0])]
                continue
            started = True
            if s_start is None:
                s_start = dist
                hist = []
            if dist - s_start > maxlen:
                break
            # si las dos calzadas van en paralelo (la separación no crece), no es una cuña: se acaba
            hist.append((dist, d))
            old_d = [h for h in hist if h[0] <= dist - 40]
            if old_d and d - old_d[-1][1] < 0.8:
                break
            side_pts.append(bl[i])
            opp_pts.append(np.array(ml.interpolate(ml.project(q)).coords[0]))
            if d > gmax:
                break
        if len(side_pts) < 4:
            return
        poly = Polygon(side_pts + opp_pts[::-1]).buffer(0)
        if poly.is_empty or poly.area < 4:
            return
        # rayas a 45°
        tdir = m.t[min(len(m.t) - 1, len(m.t) // 2)]
        mid = np.mean(np.array(side_pts), axis=0)
        j = int(np.argmin(np.hypot(*(m.p - mid).T)))
        tdir = m.t[j]
        ang = math.atan2(tdir[1], tdir[0]) + (math.radians(45) if mside == "R" else -math.radians(45))
        u = np.array([math.cos(ang), math.sin(ang)])
        v = np.array([-u[1], u[0]])
        cx, cy = poly.centroid.coords[0]
        R = max(poly.bounds[2] - poly.bounds[0], poly.bounds[3] - poly.bounds[1])
        sp = 3.0 if base in ("motorway", "trunk") else 1.8
        lines = []
        for k in np.arange(-R, R, sp):
            p0 = np.array([cx, cy]) + v * k - u * R
            p1 = np.array([cx, cy]) + v * k + u * R
            seg = LineString([p0, p1]).intersection(poly.buffer(-0.25))
            if not seg.is_empty:
                lines.append(seg)
        if lines:
            out.add_line(unary_union(lines), "cebreado", lv=int(m.lv[min(len(m.lv) - 1, j)]), w=0.45, c=base)


def out_heading(ch, i):
    """Rumbo saliendo del nodo (muestra i, extremo de la cadena) a lo largo de la cadena."""
    if i == 0:
        return ch.heading_at(0, True)
    return wrap(ch.heading_at(i, False) + math.pi)


def order_lr(c, ci, o1, o2, at):
    j1 = 0 if at == "start" else len(o1.p) - 1
    j2 = 0 if at == "start" else len(o2.p) - 1
    k1 = min(j1 + 10, len(o1.p) - 1) if at == "start" else max(j1 - 10, 0)
    k2 = min(j2 + 10, len(o2.p) - 1) if at == "start" else max(j2 - 10, 0)
    a = np.dot(o1.p[k1] - c.p[ci], c.nrm[ci])
    b = np.dot(o2.p[k2] - c.p[ci], c.nrm[ci])
    return (o1, o2) if a > b else (o2, o1)


def nearest_s(sl, target, idxs):
    idxs = np.asarray(list(idxs) if not isinstance(idxs, range) else np.arange(idxs.start, idxs.stop, idxs.step or 1))
    if len(idxs) == 0:
        return None
    d = np.abs(np.asarray(sl)[idxs] - target)
    k = int(np.argmin(d))
    return int(idxs[k]) if d[k] < 4 else None


def runs(ch):
    """Tramos seguidos del mismo nivel."""
    out = []
    N = len(ch.p)
    a = 0
    for i in range(1, N + 1):
        if i == N or ch.lv[i] != ch.lv[a] or ch.tun[i] != ch.tun[a]:
            out.append((a, min(i + 1, N), int(ch.lv[a]), bool(ch.tun[a])))
            a = i
    return out


def ribbon(ch, left, right, i0, i1, ext=1.0):
    if i1 - i0 < 2:
        return None
    Lp = ch.p[i0:i1] + ch.nrm[i0:i1] * left[i0:i1, None]
    Rp = ch.p[i0:i1] + ch.nrm[i0:i1] * right[i0:i1, None]
    Lp, _ = clean_offset(Lp, ch.t[i0:i1])
    Rp, _ = clean_offset(Rp, ch.t[i0:i1])
    if ext:
        t0, t1 = ch.t[i0], ch.t[i1 - 1]
        Lp = np.vstack([Lp[0] - t0 * ext, Lp, Lp[-1] + t1 * ext])
        Rp = np.vstack([Rp[0] - t0 * ext, Rp, Rp[-1] + t1 * ext])
    try:
        poly = Polygon(np.vstack([Lp, Rp[::-1]])).buffer(0)
    except Exception:
        return None
    if poly.is_empty:
        return None
    return poly


def signed_dist(q, E, Et, tangents=False):
    """Distancia con signo (izquierda +) de los puntos q a la polilínea E con tangentes Et."""
    out = np.zeros(len(q))
    tout = np.zeros((len(q), 2))
    for k, p in enumerate(q):
        d = np.hypot(*(E - p).T)
        j = int(np.argmin(d))
        # proyección sobre los segmentos vecinos
        best, bd, bt = None, 1e9, None
        for a in (j - 1, j):
            if a < 0 or a + 1 >= len(E):
                continue
            A, B = E[a], E[a + 1]
            AB = B - A
            L2 = float(np.dot(AB, AB)) or 1e-9
            u = float(np.clip(np.dot(p - A, AB) / L2, 0, 1))
            X = A + AB * u
            dd = float(np.hypot(*(p - X)))
            if dd < bd:
                best, bd, bt = X, dd, AB / math.sqrt(L2)
        if best is None:
            best, bt = E[j], Et[j]
            bd = float(d[j])
        cross = bt[0] * (p - best)[1] - bt[1] * (p - best)[0]
        out[k] = bd if cross >= 0 else -bd
        tout[k] = bt
    return (out, tout) if tangents else out


def classify_edge(ch, sg, probe, tdir, lane_polys, tree):
    """0 = borde pintado; 1 = separador con otra calzada pegada (lo pinta esta); 2 = lo pinta la otra; 3 = ancha."""
    kind = np.zeros(len(probe), int)
    pts = shapely.points(probe)
    hits = tree.query(pts, predicate="within")
    for a, b in zip(*hits):
        poly, other, osg = lane_polys[b]
        if other is ch and osg == sg:
            continue
        # misma dirección de marcha
        j = int(np.argmin(np.hypot(*(other.p - probe[a]).T)))
        od = other.t[j] * osg
        if np.dot(od, tdir[a]) < math.cos(math.radians(35)):
            continue
        if other is ch:
            continue
        kind[a] = 1 if id(ch) < id(other) else 2
        if other.link or ch.link:
            kind[a] = 3 if kind[a] == 1 else 2
    return kind


def run_lines(pts, t, kind, which, out, ce, lv, on, off, w_lane, w_edge, cls):
    # tramos por tipo
    if len(pts) < 2:
        return
    a = 0
    for i in range(1, len(pts) + 1):
        if i == len(pts) or kind[i] != kind[a]:
            seg = pts[a:min(i + 1, len(pts))]
            k = kind[a]
            if len(seg) >= 2:
                ls = LineString(seg)
                if ce is not None:
                    ls = ls.difference(ce)
                if k == 0:
                    out.add_line(ls, "borde", lv=lv, w=w_edge, c=cls)
                elif k == 1:
                    dashed(ls, on, off, out, "raya", lv=lv, w=w_lane, c=cls)
                elif k == 3:
                    dashed(ls, 1.0, 1.0, out, "raya", lv=lv, w=0.40, c=cls)
            a = i


def split_mask(pts, mask):
    out = []
    a = None
    for i, m in enumerate(list(mask) + [False]):
        if m and a is None:
            a = i
        elif not m and a is not None:
            if i - a >= 2:
                out.append((pts[a:i], a, i))
            a = None
    return out


def split_by(seg, flags):
    out = []
    a = 0
    for i in range(1, len(seg) + 1):
        if i == len(seg) or flags[i] != flags[a]:
            sub = seg[a:min(i + 1, len(seg))]
            if len(sub) >= 2:
                out.append((sub, bool(flags[a])))
            a = i
    return out


DASH_CODES = {(3.5, 9.0): "m", (2.5, 5.5): "u", (1.0, 1.0): "a", (0.8, 0.4): "c"}


def dashed(geom, on, off, out, kind, **props):
    """Raya discontinua: se guarda la línea entera con su patrón («d»); el estilo pinta los trazos en metros."""
    out.add_line(geom, kind, d=DASH_CODES.get((float(on), float(off)), "m"), **props)


def iter_lines(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    if hasattr(g, "geoms"):
        r = []
        for x in g.geoms:
            r += iter_lines(x)
        return r
    return []


def rank_name(r):
    for k, v in RANK.items():
        if v == r:
            return k
        if v - 0.5 == r:
            return k + "_link"
    return "tertiary"


# ------------------------------------------------------------------------------------------------------------------
# Flechas pintadas
# ------------------------------------------------------------------------------------------------------------------
def arrow_kind(tok):
    tok = tok or "none"
    parts = set(tok.split(";"))
    if parts <= {"none", ""}:
        return "through"
    has = lambda x: x in parts
    if has("merge_to_left"):
        return "merge_left"
    if has("merge_to_right"):
        return "merge_right"
    l = has("left") or has("sharp_left")
    r = has("right") or has("sharp_right")
    sl, sr = has("slight_left"), has("slight_right")
    th = has("through") or has("none")
    if has("reverse") and not (l or r or th):
        return "reverse"
    if th and l and r:
        return "all"
    if l and r:
        return "left_right"
    if th and (l or sl):
        return "through_left"
    if th and (r or sr):
        return "through_right"
    if l:
        return "left"
    if r:
        return "right"
    if sl:
        return "slight_left"
    if sr:
        return "slight_right"
    return "through"


def _arc(start, h0, h1, R, n=10):
    """Puntos de un arco desde start con rumbo h0 hasta h1 (radianes), radio R. Rumbo 0 = adelante (u)."""
    pts = [np.array(start, float)]
    hs = np.linspace(h0, h1, n)
    p = np.array(start, float)
    step = abs(h1 - h0) * R / (n - 1) if n > 1 else 0
    for k in range(1, n):
        h = hs[k]
        p = p + step * np.array([math.cos(h), math.sin(h)])
        pts.append(p.copy())
    return pts


def _path_poly(path, w):
    return LineString(path).buffer(w / 2, cap_style=2, join_style=1)


def _head(tip_base, h, hl, hw):
    d = np.array([math.cos(h), math.sin(h)])
    n = np.array([-d[1], d[0]])
    b = np.array(tip_base)
    return Polygon([b + n * hw / 2, b + d * hl, b - n * hw / 2])


def arrow_shape_local(kind, L):
    """Flecha en coordenadas (u adelante, v izquierda), de 0 a L de largo, centrada en v = 0."""
    ws = 0.075 * L            # ancho del trazo
    hl, hw = 0.30 * L, 0.20 * L
    g = []
    stem_top = L - hl

    def turn(side, start_u, ang, R=0.16 * L):
        sgn = 1 if side == "left" else -1
        pts = [np.array([start_u, 0.0])] + _arc((start_u, 0.0), 0.0, sgn * ang, R, 12)[1:]
        end = pts[-1]
        h = sgn * ang
        d = np.array([math.cos(h), math.sin(h)])
        ext = end + d * 0.10 * L
        pts.append(ext)
        g.append(_path_poly(pts, ws))
        g.append(_head(ext, h, hl * 0.85, hw * 0.95))

    if kind in ("through", "through_left", "through_right", "all"):
        g.append(_path_poly([(0, 0), (stem_top, 0)], ws))
        g.append(_head((stem_top, 0), 0.0, hl, hw))
        if kind in ("through_left", "all"):
            turn("left", 0.30 * L, math.radians(90), 0.14 * L)
        if kind in ("through_right", "all"):
            turn("right", 0.30 * L, math.radians(90), 0.14 * L)
        if kind != "through":
            g.append(_path_poly([(0, 0), (0.30 * L, 0)], ws))
    elif kind in ("left", "right"):
        g.append(_path_poly([(0, 0), (0.42 * L, 0)], ws))
        turn(kind, 0.42 * L, math.radians(90))
    elif kind == "left_right":
        g.append(_path_poly([(0, 0), (0.42 * L, 0)], ws))
        turn("left", 0.42 * L, math.radians(90))
        turn("right", 0.42 * L, math.radians(90))
    elif kind in ("slight_left", "slight_right"):
        side = "left" if kind == "slight_left" else "right"
        g.append(_path_poly([(0, 0), (0.30 * L, 0)], ws))
        sgn = 1 if side == "left" else -1
        a = math.radians(28)
        pts = [np.array([0.30 * L, 0.0])] + _arc((0.30 * L, 0.0), 0, sgn * a, 0.5 * L, 10)[1:]
        end = pts[-1]
        d = np.array([math.cos(sgn * a), math.sin(sgn * a)])
        tip_base = end + d * max(0.0, (stem_top - end[0]) / max(0.3, d[0]))
        pts.append(tip_base)
        g.append(_path_poly(pts, ws))
        g.append(_head(tip_base, sgn * a, hl, hw))
    elif kind in ("merge_left", "merge_right"):
        # flecha de fin de carril (retorno): recta y luego hacia el carril de al lado
        sgn = 1 if kind == "merge_left" else -1
        a = math.radians(32)
        p0 = np.array([0.0, 0.0])
        p1 = np.array([0.35 * L, 0.0])
        d = np.array([math.cos(sgn * a), math.sin(sgn * a)])
        p2 = p1 + d * 0.33 * L
        g.append(_path_poly([p0, p1, p2], ws))
        g.append(_head(p2, sgn * a, hl * 0.9, hw))
    elif kind == "reverse":
        # cambio de sentido: sube, gira 180° a la izquierda y baja con la punta hacia atrás
        R = 0.17 * L
        top = 0.62 * L
        pts = [np.array([0.0, 0.0]), np.array([top, 0.0])]
        for a in np.linspace(0, math.pi, 18)[1:]:
            pts.append(np.array([top + R * math.sin(a), R * (1 - math.cos(a))]))
        end = pts[-1]
        tip_base = end + np.array([-0.16 * L, 0.0])
        pts.append(tip_base)
        g.append(_path_poly(pts, ws))
        g.append(_head(tip_base, math.pi, hl * 0.7, hw * 0.9))
    poly = unary_union(g)
    # centrar a lo largo (el centro de la flecha en el punto)
    return poly, L


def arrow_poly(center, d, kind, L):
    poly, _ = arrow_shape_local(kind, L)
    d = np.asarray(d, float)
    d = d / max(1e-9, np.hypot(*d))
    n = np.array([-d[1], d[0]])
    c = np.asarray(center, float) - d * (L / 2)

    def tr(x, y, z=None):
        x = np.asarray(x)
        y = np.asarray(y)
        return c[0] + d[0] * x + n[0] * y, c[1] + d[1] * x + n[1] * y

    return shapely.ops.transform(tr, poly)


# ------------------------------------------------------------------------------------------------------------------
# Salida
# ------------------------------------------------------------------------------------------------------------------
class Output:
    def __init__(self):
        self.feats = []

    def add_poly(self, g, t, **props):
        if g is None or g.is_empty:
            return
        tol = 0.02 if t == "flecha" else 0.12
        for p in getattr(g, "geoms", [g]):
            if p.geom_type == "Polygon" and p.area > 0.05:
                q = p.simplify(tol, preserve_topology=True)
                if not q.is_empty:
                    for r in getattr(q, "geoms", [q]):
                        self.feats.append((r, dict(t=t, **props)))

    def add_line(self, g, t, **props):
        for ls in iter_lines(g):
            if ls.length > 0.2:
                self.feats.append((ls.simplify(0.06), dict(t=t, **props)))


class Proj:
    def __init__(self, lon0, lat0):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110574.0

    def __call__(self, lon, lat):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def inv(self, x, y):
        return (self.lon0 + np.asarray(x) / self.kx, self.lat0 + np.asarray(y) / self.ky)


def to_geojson(out, proj):
    feats = []
    for g, props in out.feats:
        g2 = shapely.ops.transform(lambda x, y, z=None: proj.inv(x, y), g)
        feats.append({"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(g2)})
    return {"type": "FeatureCollection", "features": feats}


def load_osm(path):
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    nodes, ntags = {}, {}
    for n in root.iter("node"):
        nodes[n.get("id")] = (float(n.get("lon")), float(n.get("lat")))
        t = {k.get("k"): k.get("v") for k in n.iter("tag")}
        if t:
            ntags[n.get("id")] = t
    ways = []
    for w in root.iter("way"):
        t = {k.get("k"): k.get("v") for k in w.iter("tag")}
        refs = [nd.get("ref") for nd in w.iter("nd")]
        if "highway" in t and all(r in nodes for r in refs):
            ways.append((w.get("id"), refs, t))
    return nodes, ntags, ways


def run_file(path, out_path):
    import json, time
    t0 = time.time()
    nodes, ntags, ways = load_osm(path)
    lon0 = np.mean([v[0] for v in nodes.values()])
    lat0 = np.mean([v[1] for v in nodes.values()])
    proj = Proj(lon0, lat0)
    net = Network(nodes, ntags, ways, proj)
    out = net.build()
    gj = to_geojson(out, proj)
    json.dump(gj, open(out_path, "w"))
    print(path, len(net.chains), "calzadas", len(gj["features"]), "elementos", round(time.time() - t0, 1), "s")
    return net


if __name__ == "__main__":
    import sys
    run_file(sys.argv[1], sys.argv[2])
