#!/usr/bin/env python3
"""
Carriles desde las ortofotos del PNOA (IGN, CC BY 4.0). Prueba: «endereza» la calzada a lo largo de su eje (filas =
metros a lo largo, columnas = metros a lo ancho), resalta las líneas blancas estrechas y suma a lo largo de 40-60 m:
las líneas de carril (continuas o discontinuas) quedan como picos en el perfil transversal. Carriles = huecos de
2,6-4,3 m entre líneas seguidas.
"""
import io, math, os, sys, urllib.request
from functools import lru_cache

import numpy as np
from PIL import Image
from scipy import ndimage, signal

Z = 19
WMTS = ("https://www.ign.es/wmts/pnoa-ma?layer=OI.OrthoimageCoverage&style=default&tilematrixset=GoogleMapsCompatible"
        "&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image/jpeg&TileMatrix={z}&TileCol={x}&TileRow={y}")
CACHE = os.environ.get("CACHE_TESELAS", "/tmp/pnoa")
os.makedirs(CACHE, exist_ok=True)


@lru_cache(maxsize=4096)
def tesela(x, y, z=Z):
    ruta = f"{CACHE}/{z}_{x}_{y}.jpg"
    if not os.path.exists(ruta):
        req = urllib.request.Request(WMTS.format(z=z, x=x, y=y), headers={"User-Agent": "ClusterRide-mapas/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            open(ruta, "wb").write(r.read())
    return np.asarray(Image.open(ruta).convert("L"), dtype=np.float32)


def a_pixel(lon, lat, z=Z):
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    r = np.radians(lat)
    y = (1 - np.log(np.tan(r) + 1 / np.cos(r)) / np.pi) / 2 * n
    return x, y


def muestra(lon, lat):
    """Gris (bilineal) en una lista de puntos."""
    xs, ys = a_pixel(np.asarray(lon), np.asarray(lat))
    out = np.zeros(xs.shape, np.float32)
    x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
    fx, fy = xs - x0, ys - y0
    for dx in (0, 1):
        for dy in (0, 1):
            px, py = x0 + dx, y0 + dy
            w = (fx if dx else 1 - fx) * (fy if dy else 1 - fy)
            tx, ty = px // 256, py // 256
            v = np.zeros(xs.shape, np.float32)
            for key in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
                m = (tx == key[0]) & (ty == key[1])
                v[m] = tesela(*key)[py[m] % 256, px[m] % 256]
            out += w * v
    return out


def enderezar(eje, paso=0.25, ancho=22.0):
    """eje: [(lon, lat)…]. Devuelve la imagen enderezada (filas a lo largo, columnas de -ancho a +ancho m)."""
    lat0 = eje[0][1]
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110540.0
    P = np.array([[lo * kx, la * ky] for lo, la in eje])
    seg = np.diff(P, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0], np.cumsum(L)])
    s = np.arange(0, cum[-1], paso)
    i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(L) - 1)
    t = (s - cum[i]) / L[i]
    c = P[i] + seg[i] * t[:, None]
    d = seg[i] / L[i][:, None]
    # dirección suavizada (las curvas no deben «doblar» la imagen)
    d = ndimage.uniform_filter1d(d, size=max(3, int(8 / paso)), axis=0)
    d /= np.hypot(d[:, 0], d[:, 1])[:, None]
    nrm = np.stack([d[:, 1], -d[:, 0]], axis=1)  # derecha del sentido de marcha
    off = np.arange(-ancho, ancho + 1e-9, paso)
    X = c[:, None, 0] + nrm[:, None, 0] * off[None, :]
    Y = c[:, None, 1] + nrm[:, None, 1] * off[None, :]
    img = muestra(X / kx, Y / ky)
    return img, off


def lineas(img, paso=0.25):
    # sombrero de copa: lo blanco y estrecho (< 0,6 m) sobre su entorno de 1,5 m
    fondo = ndimage.median_filter(img, size=(1, int(1.5 / paso) | 1))
    th = np.clip(img - fondo, 0, None)
    perfil = th.mean(axis=0)
    perfil = ndimage.gaussian_filter1d(perfil, 0.6)
    picos, prop = signal.find_peaks(perfil, prominence=perfil.max() * 0.18, distance=int(1.8 / paso))
    return th, perfil, picos


def carriles(picos, off):
    x = off[picos]
    gaps = np.diff(x)
    # bloque más largo de huecos de anchura de carril alrededor del eje (0)
    mejor, actual, ini = (0, None), 0, None
    for k, g in enumerate(gaps):
        if 2.6 <= g <= 4.3:
            if actual == 0:
                ini = k
            actual += 1
            if actual > mejor[0]:
                mejor = (actual, (ini, k + 1))
        else:
            actual = 0
    return mejor[0], (x[mejor[1][0]] if mejor[1] else None, x[mejor[1][1]] if mejor[1] else None), x


def analizar(img, off, paso=0.25, umbral=22.0):
    """Líneas por su continuidad a lo largo: continua (borde de calzada) o discontinua (entre carriles). Carriles =
    líneas discontinuas entre las dos continuas que encierran el eje, + 1, si los huecos tienen anchura de carril."""
    fondo = ndimage.median_filter(img, size=(1, int(1.5 / paso) | 1))
    th = img - fondo
    blanco = (th > umbral).astype(np.float32)
    blanco = ndimage.maximum_filter1d(blanco, size=2, axis=1)   # ±1 columna (la línea no está siempre en el mismo píxel)
    frac = ndimage.gaussian_filter1d(blanco.mean(axis=0), 0.7)
    picos, _ = signal.find_peaks(frac, height=0.12, distance=int(1.6 / paso))
    lin = [(float(off[p]), float(frac[p])) for p in picos]
    solidas = [x for x, f in lin if f >= 0.6]
    izq = max([x for x in solidas if x < -1.0], default=None)
    der = min([x for x in solidas if x > 1.0], default=None)
    if izq is None or der is None:
        return None, lin
    discont = [x for x, f in lin if izq < x < der and 0.15 <= f < 0.6]
    bordes = [izq] + discont + [der]
    huecos = np.diff(bordes)
    if len(huecos) == 0 or not all(2.5 <= g <= 4.5 for g in huecos):
        return None, lin
    return len(huecos), lin


def analizar_doble(img, off, paso=0.25, umbral=22.0):
    """Calzada de doble sentido (el eje de OSM va más o menos por la línea del centro, que puede ser continua o
    discontinua). Carriles = la cadena más larga de líneas seguidas, todas separadas por anchura de carril, que
    pasa por el eje y acaba a los dos lados en una línea de borde (continua, o discontinua marcada en ≥30 %)."""
    fondo = ndimage.median_filter(img, size=(1, int(1.5 / paso) | 1))
    th = img - fondo
    blanco = (th > umbral).astype(np.float32)
    blanco = ndimage.maximum_filter1d(blanco, size=2, axis=1)
    frac = ndimage.gaussian_filter1d(blanco.mean(axis=0), 0.7)
    picos, _ = signal.find_peaks(frac, height=0.12, distance=int(1.6 / paso))
    lin = [(float(off[p]), float(frac[p])) for p in picos]
    mejor = None
    for i in range(len(lin)):
        for j in range(i + 1, len(lin)):
            cad = lin[i:j + 1]
            xs = [x for x, _ in cad]
            if not (xs[0] < -1.5 and xs[-1] > 1.5):
                continue
            if cad[0][1] < 0.3 or cad[-1][1] < 0.3:
                continue
            if not all(2.5 <= g <= 4.5 for g in np.diff(xs)):
                continue
            if mejor is None or len(cad) > len(mejor):
                mejor = cad
    if mejor is None:
        return None, lin
    return len(mejor) - 1, lin
