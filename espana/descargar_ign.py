#!/usr/bin/env python3
"""
España · carriles del IGN (Información Geográfica de Referencia de Redes de Transporte, CC BY 4.0) por su OGC API
(https://api-features.idee.es/collections/roadlink). Solo lo que aporta a OpenStreetMap: tramos con 3 o más carriles y
los de 1 o 2 carriles de autopistas/autovías (freeway), calzadas separadas, enlaces y glorietas. Las carreteras de
una calzada con 1 o 2 carriles no se bajan (son la inmensa mayoría y es lo que se supone sin dato).

Salida: un fichero de líneas JSON comprimido {"id", "lanes", "form", "c": [[lon, lat], …]}.
Uso: python3 descargar_ign.py salida.jsonl.gz
"""
import gzip, json, sys, time, urllib.parse, urllib.request

API = "https://api-features.idee.es/collections/roadlink/items"
FOW = "http://inspire.ec.europa.eu/codelist/FormOfWayValue/"
CONSULTAS = [({"numberoflanes": n}, None) for n in range(3, 13)] + [
    ({"numberoflanes": n, "formofway_href": FOW + f}, None)
    for n in (1, 2) for f in ("freeway", "dualCarriageway", "slipRoad", "roundabout")
]
PAGINA = 1000


def pedir(url):
    for intento in range(8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ClusterRide-mapas/1.0 (+https://clusterride.clusterapps.es)"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as e:  # el servidor del IGN a veces corta: se reintenta con calma
            espera = 5 * (intento + 1)
            print(f"  reintento {intento + 1} en {espera} s: {e}", flush=True)
            time.sleep(espera)
    raise SystemExit(f"No responde: {url}")


def main(salida):
    vistos = set()
    total = 0
    with gzip.open(salida, "wt", encoding="utf-8") as out:
        for params, _ in CONSULTAS:
            q = {"f": "json", "limit": PAGINA, **params}
            url = API + "?" + urllib.parse.urlencode(q)
            n = 0
            while url:
                j = pedir(url)
                for f in j.get("features", []):
                    fid = f.get("id")
                    p = f.get("properties") or {}
                    g = f.get("geometry") or {}
                    if not fid or fid in vistos or g.get("type") != "LineString" or not p.get("numberoflanes"):
                        continue
                    vistos.add(fid)
                    c = [[round(x[0], 6), round(x[1], 6)] for x in g["coordinates"]]
                    form = (p.get("formofway_href") or "").replace(FOW, "")
                    out.write(json.dumps({"id": fid, "lanes": int(p["numberoflanes"]), "form": form, "c": c}, separators=(",", ":")) + "\n")
                    n += 1
                url = next((l["href"] for l in j.get("links", []) if l.get("rel") == "next"), None)
                if url and "f=json" not in url:  # el enlace «next» no lleva el formato
                    url += ("&" if "?" in url else "?") + "f=json"
                if j.get("numberReturned", 0) == 0:
                    url = None
                time.sleep(0.3)
            total += n
            print(f"{params}: {n} tramos (total {total})", flush=True)
    print(f"Hecho: {total} tramos con carriles", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
