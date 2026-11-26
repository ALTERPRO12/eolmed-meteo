#!/usr/bin/env python3
"""
ifremer_ww3.py — prévisions de vagues Ifremer WAVEWATCH III (MARC) pour un point.

Utilisé par eolmed_weather_report.py : fetch_ifremer_waves(lat, lon, tz, hourly_times)
renvoie {"wave_height": {t: m}, "wave_peak_period": {t: s}, "wave_direction": {t: deg}}
aux heures locales de l'axe Open-Meteo ("%Y-%m-%dT%H:%M"), ou None si indisponible.
Le rapport garde alors Météo-France MFWAM (Open-Meteo) : aucun risque pour l'envoi.

Accès : serveurs THREDDS publics d'Ifremer, service NCSS (extraction en un point, CSV).
Le jeu de données est cherché dans les catalogues (zone Roussillon 200 m, puis
Méditerranée Nord-Ouest, puis Méditerranée) et mémorisé dans ifremer_cache.json.
Licence : Licence Ouverte Etalab — citer « Ifremer / MARC ».

Lancer seul pour diagnostiquer :  python3 ifremer_ww3.py
"""
import os, csv, io, json, math, sys
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "ifremer_cache.json")
TIMEOUT = 25
UA = {"User-Agent": "MetOcean-Reporter/3 (python-requests)"}

SERVERS = ["https://tds1.ifremer.fr", "https://tds3.ifremer.fr", "https://tds0.ifremer.fr"]

# Chemins connus ou probables (testés avant l'exploration des catalogues)
CANDIDATES = [
    ("https://tds1.ifremer.fr", "MARC-MENOR-WW3_ROUSSILLON200M-FOR_FULL_TIME_SERIE"),
    ("https://tds1.ifremer.fr", "MARC-WW3_ROUSSILLON_200M-FOR_FULL_TIME_SERIE"),
    ("https://tds1.ifremer.fr", "MARC-WW3_MENOR_UG-FOR_FULL_TIME_SERIE"),
    ("https://tds3.ifremer.fr", "IOWAGA-WW3-FORECAST_MEDMULTI_MED-6M_FIELD_NC_MARC_WW3-MED-6M"),
]

# ordre de préférence des zones (la plus fine d'abord)
ZONE_PREF = ["ROUSSILLON", "MENOR", "MED"]

VAR_HS = ["hs", "VHM0", "swh"]
VAR_FP = ["fp"]                         # fréquence pic (Hz) -> Tp = 1/fp
VAR_TP = ["tp", "VTPK", "t0m1", "t02"]  # repli si pas de fp
VAR_DIR = ["dp", "dir", "VMDR"]


def log(msg):
    print("    [Ifremer] " + msg)


def _get(url, **kw):
    return requests.get(url, headers=UA, timeout=TIMEOUT, **kw)


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


# --------------------------------------------------------------------------- catalogue
def _crawl(server, max_catalogs=60):
    """Parcourt le catalogue THREDDS et renvoie les urlPath de prévisions WW3 Méditerranée."""
    found, seen = [], set()
    queue = [server + "/thredds/catalog.xml"]
    XL = "{http://www.w3.org/1999/xlink}"
    while queue and len(seen) < max_catalogs:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = _get(url)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
        except Exception:
            continue
        refs = [(el.get(XL + "href") or el.get("href") or "", el.get(XL + "title") or el.get("title") or "")
                for el in root.iter() if el.tag.endswith("catalogRef")]
        _strip_ns(root)
        for ds in root.iter("dataset"):
            p = ds.get("urlPath") or ""
            up = p.upper()
            if "WW3" in up and any(z in up for z in ZONE_PREF) and ("FOR" in up or "FORECAST" in up):
                found.append(p)
        for href, title in refs:
            tu = (title + href).upper()
            if not any(k in tu for k in ("MARC", "WW3", "IOWAGA", "MENOR", "MED", "ROUSSILLON", "VAGUE", "WAVE")):
                continue
            if href.startswith("http"):
                nxt = href
            elif href.startswith("/"):
                nxt = server + href
            else:
                nxt = url.rsplit("/", 1)[0] + "/" + href
            queue.append(nxt)
    return found


def _zone_rank(path):
    up = path.upper()
    for i, z in enumerate(ZONE_PREF):
        if z in up:
            return i
    return 99


# --------------------------------------------------------------------------- NCSS
def _describe(server, path):
    """Lit dataset.xml (NCSS) : variables + emprise. None si inaccessible."""
    try:
        r = _get("%s/thredds/ncss/%s/dataset.xml" % (server, path))
        if r.status_code != 200:
            return None
        root = _strip_ns(ET.fromstring(r.content))
    except Exception:
        return None
    names = {g.get("name") for g in root.iter("grid")}
    box = None
    b = root.find(".//LatLonBox")
    if b is not None:
        try:
            box = {k: float(b.find(k).text) for k in ("west", "east", "south", "north")}
        except Exception:
            box = None
    return {"vars": names, "box": box}


def _inside(box, lat, lon):
    if not box:
        return True
    w, e = box["west"], box["east"]
    if w > 180:
        w -= 360
    if e > 180:
        e -= 360
    return box["south"] <= lat <= box["north"] and w <= lon <= e


def _pick(names, wanted):
    low = {n.lower(): n for n in names if n}
    for w in wanted:
        if w.lower() in low:
            return low[w.lower()]
    return None


def _usable(server, path, lat, lon):
    d = _describe(server, path)
    if not d:
        return None
    if not _inside(d["box"], lat, lon):
        log("%s : site hors emprise %s" % (path, d["box"]))
        return None
    hs = _pick(d["vars"], VAR_HS)
    if not hs:
        log("%s : pas de variable Hs (%s)" % (path, ", ".join(sorted(d["vars"]))[:200]))
        return None
    return {
        "server": server, "path": path, "hs": hs,
        "fp": _pick(d["vars"], VAR_FP), "tp": _pick(d["vars"], VAR_TP), "dir": _pick(d["vars"], VAR_DIR),
    }


def find_dataset(lat, lon, refresh=False):
    """Trouve un jeu WW3 couvrant le site. Résultat mémorisé (cache 7 jours)."""
    if not refresh and os.path.exists(CACHE):
        try:
            c = json.load(open(CACHE))
            if datetime.now() - datetime.fromisoformat(c["checked"]) < timedelta(days=7):
                return c["dataset"]
        except Exception:
            pass
    tried, ds = [], None
    for server, path in CANDIDATES:
        tried.append(path)
        ds = _usable(server, path, lat, lon)
        if ds:
            break
    if not ds:
        paths = []
        for server in SERVERS:
            log("exploration du catalogue %s ..." % server)
            paths += [(server, p) for p in _crawl(server)]
        paths.sort(key=lambda sp: _zone_rank(sp[1]))
        log("%d jeu(x) WW3 Méditerranée trouvé(s) dans les catalogues" % len(paths))
        for server, path in paths:
            if path in tried:
                continue
            ds = _usable(server, path, lat, lon)
            if ds:
                break
    if ds:
        try:
            json.dump({"checked": datetime.now().isoformat(timespec="seconds"), "dataset": ds},
                      open(CACHE, "w"), indent=1)
        except Exception:
            pass
    return ds


def _scales(ds):
    """scale_factor / add_offset des variables (attributs OPeNDAP .das) : NCSS renvoie parfois les valeurs brutes."""
    out = {}
    try:
        r = _get("%s/thredds/dodsC/%s.das" % (ds["server"], ds["path"]))
        if r.status_code != 200:
            return out
        cur = None
        for line in r.text.splitlines():
            st = line.strip()
            if st.endswith("{") and len(st.split()) == 2:
                cur = st.split()[0]
                continue
            parts = st.rstrip(";").split()
            if cur and len(parts) == 3 and parts[1] in ("scale_factor", "add_offset"):
                try:
                    out.setdefault(cur, {})[parts[1]] = float(parts[2])
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def _plausible(hs_vals):
    hs_vals = [h for h in hs_vals if h is not None]
    return bool(hs_vals) and max(hs_vals) < 25


def point_series(ds, lat, lon, days=6):
    """Extraction NCSS au point → liste triée de (datetime UTC, {hs, tp, dir})."""
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    end = start + timedelta(days=days + 1)
    vars_ = [v for v in (ds["hs"], ds["fp"], ds["tp"], ds["dir"]) if v]
    params = [("var", v) for v in vars_] + [
        ("latitude", "%.4f" % lat), ("longitude", "%.4f" % lon),
        ("time_start", start.strftime("%Y-%m-%dT%H:%M:%SZ")), ("time_end", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("accept", "csv"),
    ]
    r = _get("%s/thredds/ncss/%s" % (ds["server"], ds["path"]), params=params)
    if r.status_code != 200:
        raise RuntimeError("NCSS HTTP %s : %s" % (r.status_code, r.text[:200]))
    rd = csv.reader(io.StringIO(r.text))
    header = [h.split("[")[0].strip().strip('"') for h in next(rd)]
    idx = {h: i for i, h in enumerate(header)}
    sc = _scales(ds)
    rows = [row for row in rd if row]
    # Valeurs brutes (non décompressées) ? -> appliquer scale_factor / add_offset
    raw_hs = []
    for row in rows:
        try:
            raw_hs.append(float(row[idx[ds["hs"]]]))
        except (ValueError, KeyError, IndexError):
            pass
    apply_scale = not _plausible([h for h in raw_hs if not math.isnan(h)])
    if apply_scale:
        log("valeurs brutes -> décompression (Hs ×%s, fp ×%s)" % (sc.get(ds["hs"], {}).get("scale_factor"), sc.get(ds["fp"] or "", {}).get("scale_factor")))

    def conv(name, x):
        if not apply_scale:
            return x
        a = sc.get(name, {})
        return x * a.get("scale_factor", 1.0) + a.get("add_offset", 0.0)

    out = []
    for row in rows:
        if not row:
            continue
        try:
            t = datetime.strptime(row[idx["time"]].replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue

        def val(name):
            if not name or name not in idx:
                return None
            try:
                x = float(row[idx[name]])
            except ValueError:
                return None
            if math.isnan(x) or abs(x) > 1e5:
                return None
            return conv(name, x)

        hs = val(ds["hs"])
        if hs is None or hs < 0:
            continue
        fp, tp = val(ds["fp"]), None
        if fp and fp > 0.02:
            tp = 1.0 / fp
        elif val(ds["tp"]):
            tp = val(ds["tp"])
        if tp is not None and not (1.0 <= tp <= 30.0):
            tp = None
        out.append((t, {"hs": hs, "tp": tp, "dir": val(ds["dir"])}))
    out.sort(key=lambda x: x[0])
    if not _plausible([v["hs"] for _, v in out]):
        raise RuntimeError("Hs non plausible après décompression (max %.1f) — données ignorées"
                           % max(v["hs"] for _, v in out))
    return out


# --------------------------------------------------------------------------- interpolation
def _interp(t, pts, key, circular=False):
    """Interpolation linéaire (circulaire pour une direction) entre les pas encadrants."""
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            va, vb = a.get(key), b.get(key)
            if va is None or vb is None:
                return va if t - t0 <= t1 - t else vb
            w = (t - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
            if circular:
                d = ((vb - va + 540) % 360) - 180
                return (va + w * d) % 360
            return va + w * (vb - va)
    return None


def fetch_ifremer_waves(lat, lon, tz, hourly_times):
    """Vagues Ifremer WW3 aux heures locales du rapport, ou None (le rapport garde MFWAM)."""
    try:
        ds = find_dataset(lat, lon)
        if not ds:
            log("aucun jeu WW3 Ifremer couvrant le site — MFWAM conservé")
            return None
        try:
            pts = point_series(ds, lat, lon)
        except Exception as e:
            log("extraction échouée (%s) — nouvelle recherche du jeu" % e)
            ds = find_dataset(lat, lon, refresh=True)
            pts = point_series(ds, lat, lon) if ds else []
        if len(pts) < 2:
            log("pas de données au point — MFWAM conservé")
            return None
        zone = ZoneInfo(tz) if ZoneInfo else None
        res = {"wave_height": {}, "wave_peak_period": {}, "wave_direction": {}}
        for t in hourly_times:
            try:
                loc = datetime.strptime(t, "%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            tu = loc.replace(tzinfo=zone).astimezone(timezone.utc) if zone else loc.replace(tzinfo=timezone.utc)
            hs = _interp(tu, pts, "hs")
            if hs is None:
                continue
            res["wave_height"][t] = round(hs, 2)
            tp = _interp(tu, pts, "tp")
            if tp is not None:
                res["wave_peak_period"][t] = round(tp, 1)
            d = _interp(tu, pts, "dir", circular=True)
            if d is not None:
                res["wave_direction"][t] = round(d)
        if not res["wave_height"]:
            log("aucune heure commune avec le rapport — MFWAM conservé")
            return None
        res["_dataset"] = ds["path"]
        log("✅ %s : %d h de prévision (jusqu'au %s UTC)" % (
            ds["path"], len(res["wave_height"]), pts[-1][0].strftime("%d/%m %H:%M")))
        return res
    except Exception as e:
        log("indisponible (%s) — MFWAM conservé" % e)
        return None


if __name__ == "__main__":
    lat, lon = 43.023, 3.320
    print("Diagnostic Ifremer WW3 — site %.3f N / %.3f E" % (lat, lon))
    ds = find_dataset(lat, lon, refresh=True)
    print("Jeu retenu :", json.dumps(ds, indent=1) if ds else "AUCUN")
    if not ds:
        sys.exit(1)
    pts = point_series(ds, lat, lon)
    print("%d pas de temps" % len(pts))
    for t, v in pts[:16]:
        print("  %s  Hs %.2f m  Tp %s s  Dir %s°" % (
            t.strftime("%d/%m %H:%MZ"), v["hs"],
            "%.1f" % v["tp"] if v["tp"] else "—",
            "%.0f" % v["dir"] if v["dir"] is not None else "—"))
