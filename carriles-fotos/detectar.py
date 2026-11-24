#!/usr/bin/env python3
"""
Carriles de las fotos del PNOA para las calzadas de un sentido de OpenStreetMap (autopistas, autovías y sus enlaces)
que NO tienen «lanes». Usa el mismo detector que se ha medido con validar.py (98 % de acierto cuando responde) y solo
apunta las vías en que responde con claridad.

Uso: python3 detectar.py spain.osm.pbf salida.csv resumen.json [minutos máximos]
"""
import collections, concurrent.futures as cf, csv, json, math, os, sys, time

import osmium

import validar as V

CLASES = {"motorway", "trunk", "motorway_link", "trunk_link"}
# Doble sentido: medido (24/11/2026, 400 vías) acierta el 98,6 % cuando dice 3 (2+1 con carril de adelantamiento),
# pero el 2 y el 1 fallan más y el 2 ya es lo que se supone sin dato: de estas solo se apuntan las de 3.
DOBLE = {"trunk", "primary", "secondary"}


class SinCarriles(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.vias = []

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if hw not in CLASES and hw not in DOBLE:
            return
        un_sentido = t.get("oneway") in ("yes", "-1") or hw.startswith("motorway") or t.get("junction") == "roundabout"
        if t.get("tunnel") or t.get("bridge") or t.get("lanes"):
            return
        if un_sentido and hw not in CLASES:
            return
        doble = not un_sentido
        try:
            pts = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if not (-9.5 < pts[0][0] < 4.5 and 35.8 < pts[0][1] < 43.9):
            return
        largo = sum(math.hypot((b[0] - a[0]) * 85000, (b[1] - a[1]) * 110540) for a, b in zip(pts, pts[1:]))
        if largo >= (300 if doble else 160):
            self.vias.append((w.id, 0, pts, largo, hw, doble))


def medir(via):
    wid, _, pts, largo, hw, doble = via
    mirar = 200.0 if hw.endswith("_link") else 600.0
    if largo < mirar + 100:
        mirar = max(100.0, largo - 60)
    r = V.medir((wid, 0, pts, largo), mirar, doble)
    lanes = r[2]
    if doble and lanes != 3:
        lanes = None
    return wid, hw + ("" if not doble else ":doble"), lanes


def main(pbf, salida, resumen, minutos="300"):
    t0 = time.time()
    h = SinCarriles()
    h.apply_file(pbf, locations=True, idx="sparse_file_array,/tmp/nodos2.idx")
    parte, partes = int(os.environ.get("PARTE", "0")), int(os.environ.get("PARTES", "1"))
    # Primero las calzadas de un sentido, luego los enlaces y al final las de doble sentido.
    vias = sorted((v for v in h.vias if v[0] % partes == parte), key=lambda v: (v[5], v[4].endswith("_link")))
    print(f"{len(vias)} vías sin carriles", flush=True)
    res, mirados = {}, collections.Counter()
    limite = t0 + float(minutos) * 60
    with cf.ThreadPoolExecutor(int(os.environ.get("HILOS", "6"))) as ex:
        futuros = []
        for v in vias:
            futuros.append(ex.submit(medir, v))
        for i, f in enumerate(cf.as_completed(futuros)):
            wid, hw, lanes = f.result()
            mirados[hw] += 1
            if lanes:
                res[wid] = (lanes, hw)
            if i % 200 == 0:
                print(i, len(res), flush=True)
            if time.time() > limite:
                for g in futuros:
                    g.cancel()
                print("Se acaba el tiempo: lo que falte, en la próxima vuelta", flush=True)
                break
    with open(salida, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["way_id", "lanes", "highway"])
        for wid, (lanes, hw) in sorted(res.items()):
            w.writerow([wid, lanes, hw])
    r = {"vias_sin_carriles": len(vias), "miradas": dict(mirados), "con_carriles": len(res),
         "por_carriles": dict(collections.Counter(l for l, _ in res.values())), "fuente": "PNOA © IGN (CC BY 4.0) · © OpenStreetMap"}
    json.dump(r, open(resumen, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(r, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:5])
