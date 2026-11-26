#!/usr/bin/env python3
"""
EolMed MetOcean Reporter — v3
=============================
v3 (25/11/2026) : retours phase de test — vent 20 m / 120 m en moyenne sur les heures
travaillées (06h–18h), température de l'eau, critère MOSS (Hs max selon Tp),
éphémérides, légende remontée, en-têtes répétés par jour, libellés en clair.

Rapport météo-océanique automatisé pour le site EolMed (ferme pilote d'éoliennes
flottantes, au large de Gruissan & Port-La Nouvelle).

MODÈLES ATMOSPHÉRIQUES (via Open-Meteo, choisis explicitement — plus de "best match") :
  Court terme (0 à ~42-48 h) — échelle côtière :
    1. Météo-France AROME France HD  (0.01°  ~1.5 km, horaire, maj toutes les 3 h)
    2. Météo-France AROME France     (0.025° ~2.5 km, horaire, maj toutes les 3 h)
  Moyen terme (au-delà, jusqu'à 10 jours) — large :
    3. ECMWF IFS HRES 9 km (horaire jusqu'à 90 h, puis 3 h / 6 h)  — repli IFS 0.25°
  Pour chaque heure et chaque variable, la première source disponible dans cet ordre est
  retenue. La colonne "Src" du tableau indique le modèle utilisé pour le vent.

VAGUES / COURANTS : Open-Meteo Marine API (Météo-France MFWAM, SMOC courants).
FOUDRE : CAPE + codes OMM (modèles ci-dessus) + Blitzortung (si disponible).

Usage :
  python3 eolmed_weather_report.py              # génère et envoie par email
  python3 eolmed_weather_report.py --pdf-only   # PDF sans email
  python3 eolmed_weather_report.py --test       # données simulées, sans réseau

Automatisation : voir installer_cron_eolmed.command (crontab 05:30 / 11:00 / 16:00 / 21:00).
Dépendances : pip3 install requests "reportlab==3.6.13" matplotlib
"""

# =============================================================================
# CONFIGURATION
# =============================================================================

import os as _os

CONFIG = {
    # Site
    "latitude":  43.023,
    "longitude": 3.32,
    "site_name": "EolMed site — offshore",
    "timezone":  "Europe/Paris",

    # Email (OVH Zimbra)
    "smtp_server":    _os.environ.get("EOLMED_SMTP_SERVER", "ssl0.ovh.net"),
    "smtp_port":      int(_os.environ.get("EOLMED_SMTP_PORT", "587")),
    # Expéditeur et secret lus dans les variables d'environnement (ne jamais écrire le mot de passe ici)
    "email_from":     _os.environ.get("EOLMED_SMTP_USER", ""),
    "email_password": _os.environ.get("EOLMED_SMTP_PASSWORD", ""),
    # Destinataires : variable EOLMED_EMAIL_TO (adresses séparées par des virgules), hors du code
    "email_to": [x.strip() for x in _os.environ.get("EOLMED_EMAIL_TO", "").split(",") if x.strip()],

    # Phase de test (objet "[TEST]" + bandeau dans le PDF jusqu'à cette date incluse)
    "test_phase_until": "2026-12-02",
    "feedback_to": "nicolasdalmon@alterproject.fr",
    "website_url": "https://alterpro12.github.io/eolmed-meteo/",

    # Sortie PDF
    "output_dir": _os.environ.get("EOLMED_OUTPUT_DIR", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "output")),

    # Modèles Open-Meteo, par ordre de priorité
    "models_short": ["meteofrance_arome_france_hd", "meteofrance_arome_france"],
    "models_long":  ["ecmwf_ifs", "ecmwf_ifs025"],   # le premier qui répond est retenu
    "forecast_days": 10,

    # Hauteurs de vent affichées (v3 : 20 m et 120 m au lieu de 10 m et 150 m)
    "h_low": 20,
    "h_high": 120,
    # Heures travaillées (fenêtre de travail, heure locale) : 06:00 -> 18:00
    "work_start": 6,
    "work_end": 18,
    # Seuils opérationnels (rouge si >= seuil, orange si >= seuil x warn_ratio)
    #   hs  : Hs limite 1.5 m
    #   wgL : rafale à h_low (20 m) 15 m/s   (seuil StormGeo, défini à l'origine à 10 m)
    #   wsH : vent moyen à h_high (120 m) 10 m/s (seuil StormGeo, défini à l'origine à 150 m)
    "limits": {"hs": 1.5, "wgL": 15.0, "wsH": 10.0},
    # MOSS — Max Operating Hs (1 h) en fonction de Tp (Table 18, transmise par H. Charruau le 12/11/2026)
    "moss_hs_vs_tp": [(0, 3.40), (8, 3.60), (8.5, 3.70), (9, 3.95), (9.5, 4.30), (10, 4.50),
                      (10.5, 4.45), (11, 3.60), (12, 2.59), (13, 1.91), (15, 1.09)],
    # v3 : forecaster retiré du rapport (mettre un nom de fichier pour le réafficher)
    "forecaster_name": "",
    "forecaster_photo": "",
    "warn_ratio": 0.8,
    # Fenêtre minimale (heures de jour consécutives, tous seuils OK) pour un "GO"
    "min_window_h": 4,

    # Blitzortung — rayon de détection foudre (km)
    "lightning_radius_km": 50,
}

# =============================================================================
# IMPORTS
# =============================================================================

import sys, os, math, smtplib, argparse, random, io
from datetime import datetime, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import requests
try:
    from ifremer_ww3 import fetch_ifremer_waves
except Exception:
    fetch_ifremer_waves = None
WAVES_LABEL = "Météo-France MFWAM (Open-Meteo)"
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, KeepTogether,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# CHARTE GRAPHIQUE EOLMED
# =============================================================================

def _hex(h):
    return colors.HexColor(h)

EM_NAVY   = "#1B3A8C"   # bleu EolMed (logo)
EM_RED    = "#D4321A"   # rouge-orangé EolMed (le "o" du logo)
EM_NAVY_D = "#12285F"
EM_SKY    = "#E8EEF8"   # fond clair
EM_GREY   = "#F4F6FA"
EM_LINE   = "#C9D3E6"
EM_TEXT   = "#1E2433"
EM_MUTED  = "#6B7385"

ST_GREEN_BG, ST_GREEN_FG = "#D3EFD9", "#1E6B34"
ST_AMBER_BG, ST_AMBER_FG = "#FFE3A8", "#7A4B00"
ST_RED_BG,   ST_RED_FG   = "#F7B9B0", "#8E1B0E"
ST_SOLID = {0: "#2E9E50", 1: "#E9A100", 2: "#D4321A"}

NAVY, RED, NAVY_D = _hex(EM_NAVY), _hex(EM_RED), _hex(EM_NAVY_D)
SKY, GREY, LINE, TEXT, MUTED = _hex(EM_SKY), _hex(EM_GREY), _hex(EM_LINE), _hex(EM_TEXT), _hex(EM_MUTED)
WHITE = colors.white
STATUS_BG = {0: _hex(ST_GREEN_BG), 1: _hex(ST_AMBER_BG), 2: _hex(ST_RED_BG)}
STATUS_FG = {0: _hex(ST_GREEN_FG), 1: _hex(ST_AMBER_FG), 2: _hex(ST_RED_FG)}

LTG_RANK = {"NIL": 0, "LOW": 1, "MED": 2, "HIGH": 3}
LTG_STATUS = {"NIL": 0, "LOW": 1, "MED": 2, "HIGH": 2}

MODEL_LABELS = {
    "meteofrance_arome_france_hd": ("AH", "AROME France HD 1.5 km"),
    "meteofrance_arome_france":    ("AR", "AROME France 2.5 km"),
    "ecmwf_ifs":                   ("EC", "ECMWF IFS HRES 9 km"),
    "ecmwf_ifs025":                ("EC", "ECMWF IFS 0.25°"),
    "best_match":                  ("BM", "Open-Meteo best match"),
}

MS_TO_KT = 1.94384

# =============================================================================
# OUTILS
# =============================================================================

def fnum(x):
    try:
        if x is None:
            return None
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None

def v(val, fmt=".0f"):
    f = fnum(val)
    return "—" if f is None else format(f, fmt)

def deg_to_card(deg):
    f = fnum(deg)
    if f is None:
        return "—"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((f % 360 + 11.25) / 22.5) % 16]

def utc_offset_hours(d):
    if ZoneInfo is not None:
        try:
            dt = datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(CONFIG["timezone"]))
            return dt.utcoffset().total_seconds() / 3600.0
        except Exception:
            pass
    return 2 if 4 <= d.month <= 10 else 1

def tz_label(d):
    return "CEST" if utc_offset_hours(d) == 2 else "CET"

def sun_times(lat, lon, d):
    """Lever / coucher du soleil (heures locales décimales)."""
    doy = d.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 81))))
    cos_ha = max(-1.0, min(1.0, -math.tan(math.radians(lat)) * math.tan(decl)))
    ha = math.degrees(math.acos(cos_ha))
    B = math.radians(360 / 365 * (doy - 81))
    eq_time = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    noon = 12 - lon / 15.0 - eq_time / 60 + utc_offset_hours(d)
    return noon - ha / 15.0, noon + ha / 15.0

def hhmm(h):
    hh = int(h); mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return "%02d:%02d" % (hh, mm)

def in_test_phase(d=None):
    try:
        end = datetime.strptime(CONFIG.get("test_phase_until") or "", "%Y-%m-%d").date()
    except ValueError:
        return False
    return (d or date.today()) <= end

def test_end_label():
    return datetime.strptime(CONFIG["test_phase_until"], "%Y-%m-%d").strftime("%d/%m/%Y")

def status_of(val, limit):
    f = fnum(val)
    if f is None or limit is None:
        return None
    if f >= limit:
        return 2
    if f >= limit * CONFIG["warn_ratio"]:
        return 1
    return 0

def worst(*sts):
    s = [x for x in sts if x is not None]
    return max(s) if s else None

def is_work_hour(dt):
    """Heure de travail : créneaux horaires démarrant entre work_start et work_end (exclu)."""
    return CONFIG["work_start"] <= dt.hour < CONFIG["work_end"]

def work_label():
    return "%02d:00–%02d:00" % (CONFIG["work_start"], CONFIG["work_end"])

def moss_hs_limit(tp):
    """Hs max d'exploitation MOSS (1 h) pour une période pic Tp (interpolation linéaire, Table 18)."""
    f = fnum(tp)
    if f is None:
        return None
    pts = CONFIG["moss_hs_vs_tp"]
    if f <= pts[0][0]:
        return pts[0][1]
    for (t0, h0), (t1, h1) in zip(pts, pts[1:]):
        if t0 <= f <= t1:
            return h0 + (h1 - h0) * (f - t0) / (t1 - t0)
    return pts[-1][1]

def dir_plain(deg):
    """Direction en clair : 'from WNW (292°)'."""
    f = fnum(deg)
    return "—" if f is None else "%s (%d°)" % (deg_to_card(f), int(round(f)) % 360)

# =============================================================================
# FOUDRE
# =============================================================================

STORM_CODES = {95, 96, 99}

def calc_lightning_level(cape, weather_code):
    wc = fnum(weather_code)
    if wc is not None and int(wc) in STORM_CODES:
        return "HIGH"
    c = fnum(cape) or 0.0
    if c >= 2500: return "HIGH"
    if c >= 1000: return "MED"
    if c >= 300:  return "LOW"
    return "NIL"

# =============================================================================
# VENT EN ALTITUDE
# =============================================================================

def shear_alpha(ws10, ws100):
    a, b = fnum(ws10), fnum(ws100)
    if a is None or b is None or a < 1.0 or b <= 0:
        return 0.12
    return max(0.02, min(0.35, math.log(b / a) / math.log(10.0)))

def wind_at(ws10, ws100, z):
    """Vent à la hauteur z : profil en loi de puissance calé sur 10 m et 100 m du modèle."""
    a, b = fnum(ws10), fnum(ws100)
    if b is not None and z >= 100:
        return b * (z / 100.0) ** shear_alpha(a, b)
    if a is None:
        return None
    return a * (z / 10.0) ** shear_alpha(a, b)

def gust_at(ws10, wg10, ws_z, z):
    """Rafale estimée à z : facteur de rafale 10 m atténué avec la hauteur."""
    a, g, w = fnum(ws10), fnum(wg10), fnum(ws_z)
    if w is None:
        return None
    if a is None or g is None or a < 0.5:
        return w * 1.25
    gf = max(1.0, min(2.2, g / a))
    return w * (1 + (gf - 1) * (10.0 / z) ** 0.25)

# =============================================================================
# SOURCES DE DONNÉES
# =============================================================================

ATM_VARS = [
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "wind_speed_100m", "wind_direction_100m",
    "temperature_2m", "dew_point_2m", "relative_humidity_2m",
    "visibility", "precipitation", "cape", "weather_code", "pressure_msl",
]

MARINE_VARS = [
    "wave_height", "wave_direction", "wave_period", "wave_peak_period",
    "wind_wave_height", "wind_wave_direction", "wind_wave_period",
    "swell_wave_height", "swell_wave_direction", "swell_wave_period",
    "ocean_current_velocity", "ocean_current_direction", "sea_surface_temperature",
]

def fetch_model(model, lat, lon, tz, days):
    """Interroge Open-Meteo pour UN modèle. Retourne {var: {time: value}} ou None."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(ATM_VARS),
        "wind_speed_unit": "ms",
        "forecast_days": days,
        "timezone": tz,
    }
    if model != "best_match":
        params["models"] = model
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=25)
        if r.status_code != 200:
            print("  ⚠ %s : HTTP %s %s" % (model, r.status_code, r.text[:120]))
            return None
        hourly = r.json().get("hourly", {})
    except Exception as e:
        print("  ⚠ %s indisponible (%s)" % (model, e))
        return None
    times = hourly.get("time", [])
    out = {}
    n_valid = 0
    for var in ATM_VARS:
        vals = hourly.get(var) or []
        d = {}
        for t, x in zip(times, vals):
            if fnum(x) is not None:
                d[t] = x
        out[var] = d
        if var == "wind_speed_10m":
            n_valid = len(d)
    if n_valid == 0:
        print("  ⚠ %s : aucune donnée de vent" % model)
        return None
    out["_times"] = times
    print("  ✅ %-28s %3d h de vent" % (MODEL_LABELS.get(model, (model, model))[1], n_valid))
    return out

def fetch_atmosphere(lat, lon, tz):
    days = CONFIG["forecast_days"]
    sources = []
    for m in CONFIG["models_short"]:
        d = fetch_model(m, lat, lon, tz, min(days, 4))
        if d:
            sources.append((m, d))
    long_ok = False
    for m in CONFIG["models_long"]:
        d = fetch_model(m, lat, lon, tz, days)
        if d:
            sources.append((m, d)); long_ok = True
            break
    # Dernier recours, variable par variable
    d = fetch_model("best_match", lat, lon, tz, days)
    if d:
        sources.append(("best_match", d))
    if not sources:
        raise RuntimeError("Aucune source atmosphérique disponible (Open-Meteo)")
    if not long_ok:
        print("  ⚠ ECMWF indisponible → repli Open-Meteo best match au-delà d'AROME")
    return sources

def merge_atmosphere(sources):
    """Fusionne par priorité. Retourne (times, data{var:[...]}, src[...], ecmwf_ws10[...])."""
    long_src = None
    for m, d in sources:
        if m in CONFIG["models_long"]:
            long_src = d
    base = long_src or sources[-1][1]
    times = list(base["_times"])
    data = {var: [] for var in ATM_VARS}
    src = []
    ec_ws = []
    for t in times:
        row_src = None
        for var in ATM_VARS:
            val = None
            for m, d in sources:
                if t in d[var]:
                    val = d[var][t]
                    if var == "wind_speed_10m":
                        row_src = m
                    break
            data[var].append(val)
        src.append(row_src)
        ec_ws.append(long_src["wind_speed_10m"].get(t) if long_src else None)
    return times, data, src, ec_ws

def fetch_marine(lat, lon, tz):
    r = requests.get("https://marine-api.open-meteo.com/v1/marine", params={
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(MARINE_VARS),
        "forecast_days": CONFIG["forecast_days"],
        "timezone": tz,
    }, timeout=25)
    r.raise_for_status()
    h = r.json()["hourly"]
    return {var: dict(zip(h["time"], h.get(var) or [])) for var in MARINE_VARS}

def fetch_blitzortung(lat, lon, radius_km=50):
    """Nombre d'impacts dans le rayon sur la dernière heure, ou None si indisponible."""
    try:
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
        ts = int((datetime.utcnow() - timedelta(hours=1)).timestamp() * 1000)
        resp = requests.get("https://www.blitzortung.org/en/live_lightning_maps.php", params={
            "bo_page": "live_lightning_maps", "bo_show": "strikes_json", "region": "3",
            "ts_start": ts,
            "lat_min": "%.3f" % (lat - dlat), "lat_max": "%.3f" % (lat + dlat),
            "lon_min": "%.3f" % (lon - dlon), "lon_max": "%.3f" % (lon + dlon),
        }, timeout=10, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict) and "count" in data:
                return int(data["count"])
    except Exception:
        pass
    return None

# =============================================================================
# DONNÉES SIMULÉES (--test)
# =============================================================================

def mock_sources():
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now.replace(hour=0)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(240)]
    rng = random.Random(7)

    def series(i):
        base = 7 + 6 * math.sin(i / 20.0) + 4 * math.sin(i / 7.0)
        return max(0.5, base + rng.uniform(-1, 1))

    def model(name, n, bias):
        d = {var: {} for var in ATM_VARS}
        for i, t in enumerate(times[:n]):
            ws = series(i) + bias
            d["wind_speed_10m"][t] = round(ws, 1)
            d["wind_gusts_10m"][t] = round(ws * 1.35, 1)
            d["wind_speed_100m"][t] = round(ws * 1.25, 1)
            d["wind_direction_10m"][t] = (300 + 40 * math.sin(i / 15.0)) % 360
            d["wind_direction_100m"][t] = d["wind_direction_10m"][t]
            d["temperature_2m"][t] = 15 + 3 * math.sin((i % 24 - 9) / 24.0 * 2 * math.pi)
            d["dew_point_2m"][t] = 11.0
            d["relative_humidity_2m"][t] = 75
            d["visibility"][t] = 24000 if i % 37 else 3500
            d["precipitation"][t] = 0.0 if i % 23 else 1.2
            if name != "meteofrance_arome_france_hd":
                d["cape"][t] = 1200 if 60 < i < 66 else 80
            d["weather_code"][t] = 95 if i == 63 else 2
            d["pressure_msl"][t] = 1012
        d["_times"] = times[:n]
        return d

    return [
        ("meteofrance_arome_france_hd", model("meteofrance_arome_france_hd", 42, 0.6)),
        ("meteofrance_arome_france", model("meteofrance_arome_france", 51, 0.3)),
        ("ecmwf_ifs", model("ecmwf_ifs", 240, 0.0)),
    ], times

def mock_marine(times):
    m = {var: {} for var in MARINE_VARS}
    for i, t in enumerate(times):
        hs = max(0.2, 0.9 + 0.7 * math.sin(i / 20.0) + 0.3 * math.sin(i / 6.0))
        m["wave_height"][t] = round(hs, 2)
        m["wave_direction"][t] = 120
        m["wave_period"][t] = 4.5
        m["wave_peak_period"][t] = 6.0
        m["wind_wave_height"][t] = round(hs * 0.7, 2)
        m["wind_wave_direction"][t] = 300
        m["wind_wave_period"][t] = 3.5
        m["swell_wave_height"][t] = round(hs * 0.5, 2)
        m["swell_wave_direction"][t] = 140
        m["swell_wave_period"][t] = 7.0
        m["ocean_current_velocity"][t] = 0.4
        m["ocean_current_direction"][t] = 220
        m["sea_surface_temperature"][t] = 16.5
    return m

# =============================================================================
# CONSTRUCTION DU JEU DE DONNÉES HORAIRE
# =============================================================================

def build_rows(times, atm, src, ec_ws, marine, start_idx):
    L = CONFIG["limits"]
    rows = []
    for idx in range(start_idx, len(times)):
        t = times[idx]
        try:
            dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        g = lambda var: atm[var][idx]
        mg = lambda var: marine.get(var, {}).get(t)
        ws10, wg10, ws100 = fnum(g("wind_speed_10m")), fnum(g("wind_gusts_10m")), fnum(g("wind_speed_100m"))
        zL, zH = CONFIG["h_low"], CONFIG["h_high"]
        wsL = wind_at(ws10, ws100, zL)
        wsH = wind_at(ws10, ws100, zH)
        wgL = gust_at(ws10, wg10, wsL, zL)
        wgH = gust_at(ws10, wg10, wsH, zH)
        hs = fnum(mg("wave_height"))
        ltg = calc_lightning_level(g("cape"), g("weather_code"))
        lead = idx - start_idx
        # confiance : échéance + accord AROME / ECMWF
        diff = None
        if src[idx] in CONFIG["models_short"] and ws10 is not None and fnum(ec_ws[idx]) is not None:
            diff = abs(ws10 - fnum(ec_ws[idx]))
        if lead <= 48 and (diff is None or diff < 3):
            conf = 0
        elif lead <= 96 and (diff is None or diff < 6):
            conf = 1
        else:
            conf = 2 if lead > 96 else 1
        st = {
            "hs": status_of(hs, L["hs"]),
            "wgL": status_of(wgL, L["wgL"]),
            "wsH": status_of(wsH, L["wsH"]),
            "ltg": LTG_STATUS[ltg],
        }
        tp = fnum(mg("wave_peak_period"))
        if tp is None:  # période pic non fournie : période du système dominant (houle / mer du vent)
            swh_, wwh_ = fnum(mg("swell_wave_height")), fnum(mg("wind_wave_height"))
            tp = fnum(mg("swell_wave_period")) if (swh_ or 0) >= (wwh_ or 0) else fnum(mg("wind_wave_period"))
        moss_lim = moss_hs_limit(tp)
        moss_ratio = (hs / moss_lim) if (hs is not None and moss_lim) else None
        rows.append({
            "t": t, "dt": dt, "lead": lead, "src": src[idx], "conf": conf, "work": is_work_hour(dt),
            "dir": g("wind_direction_10m"), "ws10": ws10, "wg10": wg10,
            "wsL": wsL, "wgL": wgL, "wsH": wsH, "wgH": wgH,
            "moss_lim": moss_lim, "moss_ratio": moss_ratio,
            "moss_st": status_of(moss_ratio, 1.0) if moss_ratio is not None else None,
            "t2m": g("temperature_2m"), "vis": g("visibility"), "prec": g("precipitation"),
            "cape": g("cape"), "ltg": ltg,
            "hs": hs, "hmax": hs * 1.86 if hs is not None else None,
            "wdir": mg("wave_direction"), "tp": tp, "tm": mg("wave_period"),
            "wwh": mg("wind_wave_height"), "wwdir": mg("wind_wave_direction"), "wwp": mg("wind_wave_period"),
            "swh": mg("swell_wave_height"), "swdir": mg("swell_wave_direction"), "swp": mg("swell_wave_period"),
            "cur": mg("ocean_current_velocity"), "curdir": mg("ocean_current_direction"),
            "sst": mg("sea_surface_temperature"),
            "st": st, "status": worst(*st.values()),
        })
    return rows

def daily_summary(rows, lat, lon, max_days=10):
    """Synthèse par jour, calculée sur les heures travaillées (work_start–work_end)."""
    days = []
    by_day = {}
    for r in rows:
        by_day.setdefault(r["dt"].date(), []).append(r)
    for d in sorted(by_day)[:max_days]:
        rs = by_day[d]
        rise, sset = sun_times(lat, lon, d)
        work = [r for r in rs if r["work"]]
        base = work          # pas d'heure travaillée restante (ex. run de 21:00) -> valeurs vides
        vals = lambda k: [fnum(r[k]) for r in base if fnum(r[k]) is not None]
        mx = lambda k: max(vals(k), default=None)
        avg = lambda k: (sum(vals(k)) / len(vals(k))) if vals(k) else None
        ltg = max([r["ltg"] for r in base], key=lambda x: LTG_RANK[x]) if base else "NIL"
        op = sum(1 for r in work if r["status"] == 0) if work else None
        # nombre d'heures de travail couvertes (pas horaire supposé ; 3 h / 6 h au-delà de 48 h)
        cover = len(work)
        srcs = [MODEL_LABELS.get(r["src"], ("?",))[0] for r in rs if r["src"]]
        # plus longue fenêtre consécutive (heures travaillées) où tous les seuils sont OK
        best, cur = None, []
        for r in work:
            if r["status"] == 0:
                cur.append(r)
                if best is None or len(cur) > len(best):
                    best = list(cur)
            else:
                cur = []
        window = (best[0]["dt"], best[-1]["dt"] + timedelta(hours=1), len(best)) if best else None
        names = {"hs": "Hs", "wgL": "gust %d m" % CONFIG["h_low"], "wsH": "wind %d m" % CONFIG["h_high"], "ltg": "lightning"}
        cnt = {}
        for r in work:
            for k, sv in r["st"].items():
                if sv == 2:
                    cnt[k] = cnt.get(k, 0) + 1
        limiting = names[max(cnt, key=cnt.get)] if cnt else None
        wh = window[2] if window else 0
        verdict = None if not work else ("GO" if wh >= CONFIG["min_window_h"] else "LIMITED WINDOW" if wh > 0 else "NO-GO")
        moss = [r["moss_ratio"] for r in base if r["moss_ratio"] is not None]
        days.append({
            "window": window, "limiting": limiting, "verdict": verdict,
            "date": d, "rows": rs, "partial": len(work) < (CONFIG["work_end"] - CONFIG["work_start"]) and rs[0]["lead"] == 0,
            "hs": mx("hs"), "tp": mx("tp"), "sst": avg("sst"),
            "wsL": avg("wsL"), "wsL_max": mx("wsL"),
            "wgL": avg("wgL"), "wgL_max": mx("wgL"),
            "wsH": avg("wsH"), "wsH_max": mx("wsH"),
            "moss": max(moss) if moss else None,
            "ltg": ltg, "op_hours": op, "work_hours": cover,
            "daylight": sset - rise, "rise": rise, "set": sset,
            "src": "/".join(sorted(set(srcs), key=srcs.index)),
            "status": worst(*[r["status"] for r in base]) if base else None,
        })
    return days

# =============================================================================
# GRAPHIQUES
# =============================================================================

def build_charts(rows, width_cm, height_cm, short_until):
    L = CONFIG["limits"]
    zL, zH = CONFIG["h_low"], CONFIG["h_high"]
    dts = [r["dt"] for r in rows]
    nan = float("nan")
    num = lambda k: [fnum(r[k]) if fnum(r[k]) is not None else nan for r in rows]
    hs, hmax, moss = num("hs"), num("hmax"), num("moss_lim")
    wsL, wgL, wsH = num("wsL"), num("wgL"), num("wsH")

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 7})
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True,
                                   figsize=(width_cm / 2.54, height_cm / 2.54), facecolor="white")
    fig.subplots_adjust(hspace=0.30, left=0.055, right=0.945, top=0.94, bottom=0.11)

    d0 = dts[0].date()
    for ax in (ax1, ax2):
        ax.set_facecolor("white")
        for s_ in ("top", "right"):
            ax.spines[s_].set_visible(False)
        ax.spines["left"].set_color(EM_LINE); ax.spines["bottom"].set_color(EM_LINE)
        ax.tick_params(colors=EM_MUTED, labelsize=6.3, length=2)
        ax.grid(True, axis="y", color=EM_LINE, linewidth=0.5, alpha=0.7)
        # hors heures travaillées grisé, séparateurs de jours
        for k in range(12):
            d = d0 + timedelta(days=k)
            day0 = datetime.combine(d, datetime.min.time())
            ax.axvspan(day0 + timedelta(hours=CONFIG["work_end"]),
                       day0 + timedelta(days=1, hours=CONFIG["work_start"]), color="#EEF1F6", zorder=0, lw=0)
            ax.axvline(day0, color="#B9C3D6", lw=0.6, zorder=1)
        if short_until is not None:
            ax.axvline(short_until, color=EM_MUTED, lw=0.6, ls=":", zorder=1)
        # axe des temps : date chaque jour + heures 06/12/18, sur les DEUX graphiques
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[12]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d/%m"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 6, 18]))
        ax.xaxis.set_minor_formatter(mdates.DateFormatter("%Hh"))
        ax.tick_params(axis="x", which="minor", labelsize=5.2, colors="#98A1B3", length=2, pad=1)
        ax.tick_params(axis="x", which="major", labelsize=6.5, colors=EM_TEXT, length=0, pad=8)
        ax.tick_params(axis="x", labelbottom=True)
        for lab in ax.get_xticklabels(which="major"):
            lab.set_fontweight("bold")

    # --- Vagues
    ax1.fill_between(dts, hs, color=EM_NAVY, alpha=0.18, lw=0, zorder=2)
    ax1.plot(dts, hs, color=EM_NAVY, lw=1.6, label="Hs", zorder=3)
    ax1.plot(dts, hmax, color=EM_NAVY, lw=0.7, ls="--", alpha=0.6, label="Hmax (est.)", zorder=3)
    ax1.plot(dts, moss, color="#7B3FA0", lw=0.9, ls="-.", alpha=0.85, label="MOSS max Hs (Tp)", zorder=3)
    hs_arr = [h if h == h else 0 for h in hs]
    ax1.fill_between(dts, hs_arr, L["hs"], where=[h > L["hs"] for h in hs_arr],
                     color=EM_RED, alpha=0.35, lw=0, zorder=2, interpolate=True)
    ax1.axhline(L["hs"], color=EM_RED, lw=1.2, ls="--", zorder=4)
    ax1.text(dts[-1], L["hs"], "  Hs limit %.1f m" % L["hs"], color=EM_RED, fontsize=6.5,
             va="bottom", ha="right", fontweight="bold")
    ax1.set_ylabel("Waves (m)", color=EM_NAVY, fontsize=7)
    top1 = max([x for x in hmax if x == x] or [1]) * 1.1
    ax1.set_ylim(0, min(max(L["hs"] * 1.4, top1), 5.0))
    ax1.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), fontsize=6.3, frameon=False, ncol=3, borderaxespad=0.1)
    ax1.set_title("Significant wave height (grey = outside working hours %s)" % work_label(),
                  loc="left", fontsize=8, fontweight="bold", color=EM_TEXT, pad=2)
    if short_until is not None:
        ax1.text(short_until, ax1.get_ylim()[1], " ECMWF → ", fontsize=6, color=EM_MUTED, va="top", ha="left")
        ax1.text(short_until, ax1.get_ylim()[1], " ← AROME ", fontsize=6, color=EM_MUTED, va="top", ha="right")

    # --- Vent
    ax2.fill_between(dts, wsL, color=EM_NAVY, alpha=0.10, lw=0, zorder=2)
    ax2.plot(dts, wsL, color=EM_NAVY, lw=1.4, label="Mean wind %d m" % zL, zorder=3)
    ax2.plot(dts, wgL, color=EM_NAVY, lw=0.8, ls="--", alpha=0.75, label="Gust %d m" % zL, zorder=3)
    ax2.plot(dts, wsH, color=EM_RED, lw=1.3, label="Mean wind %d m" % zH, zorder=3)
    ax2.axhline(L["wgL"], color=EM_NAVY_D, lw=0.9, ls=":", zorder=4)
    ax2.text(dts[-1], L["wgL"], "Gust %d m limit %d m/s " % (zL, L["wgL"]), color=EM_NAVY_D, fontsize=6,
             va="bottom", ha="right")
    ax2.axhline(L["wsH"], color=EM_RED, lw=0.9, ls=":", zorder=4)
    ax2.text(dts[-1], L["wsH"], "Wind %d m limit %d m/s " % (zH, L["wsH"]), color=EM_RED, fontsize=6,
             va="bottom", ha="right")
    ax2.set_ylabel("Wind (m/s)", color=EM_NAVY, fontsize=7)
    top = max([x for x in wgL + wsH if x == x] or [10]) * 1.12
    ax2.set_ylim(0, max(top, L["wgL"] * 1.15))
    ax2r = ax2.twinx()
    ax2r.set_ylim(0, ax2.get_ylim()[1] * MS_TO_KT)
    ax2r.set_ylabel("kn", color=EM_MUTED, fontsize=7)
    ax2r.tick_params(colors=EM_MUTED, labelsize=6.3, length=2)
    for s_ in ("top", "left"):
        ax2r.spines[s_].set_visible(False)
    ax2r.spines["right"].set_color(EM_LINE)
    ax2.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), fontsize=6.3, frameon=False, ncol=3, borderaxespad=0.1)
    ax2.set_title("Wind", loc="left", fontsize=8, fontweight="bold", color=EM_TEXT, pad=2)
    ax2.set_xlim(dts[0], dts[-1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf

# =============================================================================
# PDF
# =============================================================================

def P(text, size=7, bold=False, color=TEXT, align=TA_LEFT, leading=None):
    return Paragraph(text, ParagraphStyle(
        "p", fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
        textColor=color, alignment=align, leading=leading or size * 1.22))

def find_asset(name, output_path):
    for d in (SCRIPT_DIR, os.path.dirname(output_path), CONFIG["output_dir"]):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None

def cropped_logo(path):
    """Retire les marges blanches du logo (renvoie un flux PNG, ou le chemin)."""
    try:
        from PIL import Image as PILImage, ImageChops
        im0 = PILImage.open(path).convert("RGBA")
        im = PILImage.new("RGB", im0.size, (255, 255, 255))
        im.paste(im0, mask=im0.split()[3])          # fond transparent -> blanc (v3)
        bg = PILImage.new("RGB", im.size, (255, 255, 255))
        bbox = ImageChops.difference(im, bg).getbbox()
        if bbox:
            im = im.crop(bbox)
        buf = io.BytesIO(); im.save(buf, format="PNG"); buf.seek(0)
        return buf, im.size[0] / float(im.size[1])
    except Exception:
        return path, 640 / 344.0

def kpi_tile(title, value, sub, status, w):
    bg = STATUS_BG.get(status, SKY)
    fg = STATUS_FG.get(status, NAVY)
    t = Table([[P(title.upper(), 6, True, MUTED)],
               [P(value, 13, True, fg, leading=15)],
               [P(sub, 6.3, False, MUTED)]], colWidths=[w])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEABOVE", (0, 0), (-1, 0), 2.2, _hex(ST_SOLID[status]) if status is not None else NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    return t

def fmt_time(dt):
    return dt.strftime("%a %H:%M") if dt else "—"

def synopsis(rows, days):
    r24 = rows[:24]
    if not r24:
        return "No data."
    zL = CONFIG["h_low"]
    mx = lambda k: max([fnum(r[k]) for r in r24 if fnum(r[k]) is not None], default=0)
    avg = lambda k: sum([fnum(r[k]) for r in r24 if fnum(r[k]) is not None]) / max(1, len([r for r in r24 if fnum(r[k]) is not None]))
    ws, wg, hs = avg("wsL"), mx("wgL"), mx("hs")
    bft = lambda w: ("light" if w < 5.5 else "moderate" if w < 8 else "fresh" if w < 10.8
                     else "strong" if w < 13.9 else "near gale" if w < 17.2 else "gale")
    sea = ("calm" if hs < 0.5 else "slight" if hs < 1.25 else "moderate" if hs < 2.5 else "rough")
    dirs = [deg_to_card(r["dir"]) for r in r24 if r["dir"] is not None]
    d_main = max(set(dirs), key=dirs.count) if dirs else "—"
    degs = [fnum(r["dir"]) for r in r24 if fnum(r["dir"]) is not None and deg_to_card(r["dir"]) == d_main]
    d_deg = (" (≈%d°)" % int(round(sum(degs) / len(degs)))) if degs else ""
    txt = ("<b>Next 24 h:</b> %s wind blowing from the %s%s, mean %.0f m/s (%.0f kn) at %d m, gusts up to %.0f m/s (%.0f kn). "
           "Sea %s, Hs max %.1f m. " % (bft(ws).capitalize(), d_main, d_deg, ws, ws * MS_TO_KT, zL,
                                        wg, wg * MS_TO_KT, sea, hs))
    out = []
    for d in days[1:4]:
        tag = {"GO": "workable", "LIMITED WINDOW": "short window", "NO-GO": "not workable"}.get(d["verdict"], "n/a")
        out.append("%s: Hs %s m, mean gust %s m/s — %s" % (d["date"].strftime("%a %d"), v(d["hs"], ".1f"), v(d["wgL"]), tag))
    if out:
        txt += "<b>Outlook (working hours %s):</b> " % work_label() + " · ".join(out) + "."
    return txt.replace("≈", "~")

def build_pdf(rows, days, meta, output_path):
    lat, lon = CONFIG["latitude"], CONFIG["longitude"]
    L = CONFIG["limits"]
    zL, zH = CONFIG["h_low"], CONFIG["h_high"]
    WL = work_label()
    nwork = CONFIG["work_end"] - CONFIG["work_start"]
    issue = meta["issue_time"]
    PW, PH = landscape(A4)
    M = 0.8 * cm
    W = PW - 2 * M

    # ---------- pages ----------
    def on_page(c, doc):
        c.saveState()
        c.setFillColor(NAVY); c.rect(0, PH - 0.32 * cm, PW, 0.32 * cm, fill=1, stroke=0)
        c.setFillColor(RED); c.rect(0, PH - 0.42 * cm, PW * 0.18, 0.10 * cm, fill=1, stroke=0)
        c.setFont("Helvetica", 6); c.setFillColor(MUTED)
        c.drawString(M, 0.45 * cm, "EolMed MetOcean Reporter v3  ·  %s  ·  issued %s %s  ·  automated forecast, not a substitute for a meteorologist's advice"
                     % (CONFIG["site_name"], issue.strftime("%d/%m/%Y %H:%M"), tz_label(issue.date())))
        c.drawRightString(PW - M, 0.45 * cm, "Page %d" % doc.page)
        if doc.page > 1:
            c.setFont("Helvetica-Bold", 8); c.setFillColor(NAVY)
            c.drawString(M, PH - 0.95 * cm, "EOLMED  ·  Hourly forecast table")
            c.setFont("Helvetica", 7); c.setFillColor(MUTED)
            c.drawRightString(PW - M, PH - 0.95 * cm, meta["issue_label"])
        c.restoreState()

    doc = BaseDocTemplate(output_path, pagesize=(PW, PH), leftMargin=M, rightMargin=M,
                          topMargin=0.7 * cm, bottomMargin=0.8 * cm,
                          title="EolMed weather forecast %s" % issue.strftime("%d/%m/%Y %H:%M"),
                          author="EolMed MetOcean Reporter")
    f1h = PH - 0.7 * cm - 0.8 * cm
    f1 = Frame(M, 0.8 * cm, W, f1h, id="f1", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    f2 = Frame(M, 0.8 * cm, W, PH - 1.25 * cm - 0.8 * cm, id="f2", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="first", frames=[f1], onPage=on_page),
                          PageTemplate(id="later", frames=[f2], onPage=on_page)])
    from reportlab.platypus import NextPageTemplate
    page1 = []

    # ---------- en-tête (logo, titre, éphémérides) ----------
    logo_path = find_asset("EOLMED-LOGO-QUADRI.png", output_path)
    if logo_path:
        src, ratio = cropped_logo(logo_path)
        lh = 1.15 * cm
        logo = Image(src, width=lh * ratio, height=lh)
    else:
        logo = P("eolmed", 20, True, NAVY)
    d0 = issue.date()
    rise, sset = sun_times(lat, lon, d0)
    rise1, sset1 = sun_times(lat, lon, d0 + timedelta(days=1))
    tzl = tz_label(d0)
    title = [P("Offshore weather forecast", 15, True, NAVY, leading=18),
             P("%s  ·  %.3f°N %.3f°E  ·  %s" % (CONFIG["site_name"], lat, lon, meta["issue_label"]), 7.5, False, MUTED)]
    dl = sset - rise
    info = Table([
        [P("Ephemeris (%s)" % tzl, 6, True, MUTED), P("Sunrise", 6, True, MUTED), P("Sunset", 6, True, MUTED),
         P("Daylight", 6, True, MUTED), P("Working hours", 6, True, MUTED), P("Models", 6, True, MUTED)],
        [P("Today %s" % d0.strftime("%d/%m"), 7, True, TEXT), P(hhmm(rise), 8, True, TEXT), P(hhmm(sset), 8, True, TEXT),
         P("%dh%02d" % (int(dl), int(round((dl % 1) * 60)) % 60), 7.5, False, TEXT),
         P(WL, 8, True, NAVY), P(meta["models_short_txt"], 6.2, False, TEXT)],
        [P("Tomorrow %s" % (d0 + timedelta(days=1)).strftime("%d/%m"), 7, False, MUTED), P(hhmm(rise1), 7.5, False, MUTED),
         P(hhmm(sset1), 7.5, False, MUTED), P("", 6), P("", 6), P("", 6)],
    ], colWidths=[2.1 * cm, 1.2 * cm, 1.2 * cm, 1.3 * cm, 2.1 * cm, 4.6 * cm])
    info.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                              ("TOPPADDING", (0, 0), (-1, -1), 0.3), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.3),
                              ("LINEBEFORE", (0, 0), (-1, -1), 0.6, LINE), ("SPAN", (5, 1), (5, 2)),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    info_w = 12.5 * cm
    logo_w = logo.drawWidth if hasattr(logo, "drawWidth") else 4 * cm
    fc_path = find_asset(CONFIG["forecaster_photo"], output_path) if CONFIG.get("forecaster_photo") else None
    fc_w, fc_cell = 0.0, ""
    if fc_path:
        ph = 1.1 * cm
        fc_img = Image(fc_path, width=ph * 305 / 360.0, height=ph)
        fc_cell = Table([[fc_img, [P("Forecaster", 6, True, MUTED),
                                   P(CONFIG.get("forecaster_name", "ND Junior"), 8, True, NAVY)]]],
                        colWidths=[fc_img.drawWidth + 0.15 * cm, 1.75 * cm])
        fc_cell.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                     ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                     ("BOX", (0, 0), (0, 0), 0.4, LINE)]))
        fc_w = fc_img.drawWidth + 0.15 * cm + 1.75 * cm + 0.35 * cm
    hdr = Table([[logo, title, info, fc_cell]],
                colWidths=[logo_w + 0.5 * cm, W - logo_w - 0.5 * cm - info_w - fc_w, info_w, fc_w])
    hdr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (3, 0), (3, 0), "RIGHT"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    page1 += [Spacer(1, 0.22 * cm), hdr, Spacer(1, 0.2 * cm)]

    # ---------- bandeau foudre ----------
    ltg = meta["ltg_now"]
    ltg_txt = {"NIL": "NIL — no thunderstorm risk", "LOW": "LOW — monitor conditions",
               "MED": "MEDIUM — thunderstorms possible", "HIGH": "HIGH — thunderstorm risk, stop exposed operations"}[ltg]
    nxt = meta["ltg_next"]
    right = ("Next 48 h max: %s%s" % (nxt[0], (" (" + fmt_time(nxt[1]) + ")") if nxt[1] else ""))
    right += "  ·  " + (("%d strike(s) < %d km last hour (Blitzortung)" % (meta["strikes"], CONFIG["lightning_radius_km"]))
                        if meta["strikes"] is not None else "based on CAPE / WMO weather codes")
    col = _hex({"NIL": "#5B6478", "LOW": "#1B3A8C", "MED": "#E07B00", "HIGH": EM_RED}[ltg])
    band = Table([[P("LIGHTNING  ·  " + ltg_txt, 8.5, True, WHITE), P(right, 7, False, WHITE, TA_RIGHT)]],
                 colWidths=[W * 0.5, W * 0.5])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), col), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                              ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
    page1 += [band]
    if in_test_phase(issue.date()):
        tb = Table([[P("NEW VERSION v3 — TEST until %s. Changes: wind at %d m / %d m averaged over working hours %s, sea temperature, "
                       "MOSS criterion, clearer legend. Live web page: %s · Comments to %s"
                       % (test_end_label(), zL, zH, WL, CONFIG["website_url"].replace("https://", ""), CONFIG["feedback_to"]), 7, True, _hex(ST_AMBER_FG))]], colWidths=[W])
        tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _hex(ST_AMBER_BG)),
                                ("LEFTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 1.8),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8)]))
        page1 += [tb]
    page1 += [Spacer(1, 0.18 * cm)]

    # ---------- tuiles KPI : prochaines 24 h, heures travaillées ----------
    r24 = [r for r in rows[:24] if r["work"]] or rows[:24]
    def avg_of(k):
        x = [fnum(r[k]) for r in r24 if fnum(r[k]) is not None]
        return sum(x) / len(x) if x else None
    def argmax(k):
        c = [r for r in r24 if fnum(r[k]) is not None]
        return max(c, key=lambda r: r[k]) if c else None
    aL, aG, aH, aT = avg_of("wsL"), avg_of("wgL"), avg_of("wsH"), avg_of("sst")
    mL, mG, mH, rh = argmax("wsL"), argmax("wgL"), argmax("wsH"), argmax("hs")
    vis_c = [r for r in r24 if fnum(r["vis"]) is not None]
    rv = min(vis_c, key=lambda r: r["vis"]) if vis_c else None
    dirs = [r["dir"] for r in r24 if fnum(r["dir"]) is not None]
    sinv = sum(math.sin(math.radians(fnum(x))) for x in dirs); cosv = sum(math.cos(math.radians(fnum(x))) for x in dirs)
    mdir = (math.degrees(math.atan2(sinv, cosv)) % 360) if dirs else None
    wday = r24[0]["dt"].strftime("%a %d/%m") if r24 else ""
    if r24 and r24[-1]["dt"].date() != r24[0]["dt"].date():
        wday += "–" + r24[-1]["dt"].strftime("%a")
    per = wday + " " + WL[:2] + "–" + WL[6:8] + "h"
    today = days[0] if days else None
    tom = days[1] if len(days) > 1 else None
    ntile, gap = 7, 0.16 * cm
    tw = (W - (ntile - 1) * gap) / ntile
    tiles = [
        kpi_tile("Mean wind %d m · average" % zL, "%s m/s" % v(aL),
                 "%s · %s kn · from %s · max %s" % (per, v(aL and aL * MS_TO_KT), dir_plain(mdir), v(mL and mL["wsL"])), None, tw),
        kpi_tile("Gust %d m · average" % zL, "%s m/s" % v(aG),
                 "%s · max %s (%s) · limit %d" % (per, v(mG and mG["wgL"]), fmt_time(mG and mG["dt"]), L["wgL"]),
                 status_of(aG, L["wgL"]), tw),
        kpi_tile("Mean wind %d m · average" % zH, "%s m/s" % v(aH),
                 "%s · max %s (%s) · limit %d" % (per, v(mH and mH["wsH"]), fmt_time(mH and mH["dt"]), L["wsH"]),
                 status_of(aH, L["wsH"]), tw),
        kpi_tile("Hs · max", "%s m" % v(rh and rh["hs"], ".1f"),
                 "%s · Tp %s s · limit %.1f m · MOSS limit %s m" % (wday, v(rh and rh["tp"], ".1f"), L["hs"], v(rh and rh["moss_lim"], ".1f")),
                 status_of(rh and rh["hs"], L["hs"]), tw),
        kpi_tile("Sea temperature", "%s °C" % v(aT, ".1f"), "sea surface · %s" % wday, None, tw),
        kpi_tile("Visibility · min", "%s km" % v(rv and fnum(rv["vis"]) / 1000.0), fmt_time(rv and rv["dt"]),
                 (2 if rv and fnum(rv["vis"]) < 1000 else 1 if rv and fnum(rv["vis"]) < 4000 else 0) if rv else None, tw),
        kpi_tile("Workable hours · today | tom.", "%s h | %s h" % (v(today and today["op_hours"]), v(tom and tom["op_hours"])),
                 "hours in %s with ALL limits met (max %d h)" % (WL, nwork),
                 (0 if (tom and (tom["op_hours"] or 0) >= 6) else 1 if (tom and (tom["op_hours"] or 0) > 0) else 2) if tom else None, tw),
    ]
    tt = Table([tiles], colWidths=[tw + gap] * (ntile - 1) + [tw])
    tt.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), gap),
                            ("RIGHTPADDING", (-1, 0), (-1, 0), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    page1 += [tt, Spacer(1, 0.2 * cm)]

    # ---------- synthèse journalière (heures travaillées) ----------
    nd = len(days)
    lw = 4.1 * cm
    dw = (W - lw) / max(1, nd)
    hdr_row = [P("<b>DAILY OUTLOOK</b><br/><font size=5.5>working hours %s</font>" % WL, 6.5, True, WHITE, leading=7.5)] + \
              [P("<b>%s</b><br/>%s" % (d["date"].strftime("%a").upper(), d["date"].strftime("%d/%m")), 6.5, True, WHITE, TA_CENTER, 7.5) for d in days]
    def cellp(txt, st):
        return P(txt, 7, st == 2, STATUS_FG.get(st, TEXT), TA_CENTER, 8)
    def am(a, m):
        return "—" if a is None else "%s <font size=5.5 color='%s'>(%s)</font>" % (v(a), EM_MUTED, v(m))
    lines = [
        ("Sunrise / sunset", lambda d: ("%s / %s" % (hhmm(d["rise"]), hhmm(d["set"])), None)),
        ("Hs max (m)", lambda d: (v(d["hs"], ".1f"), status_of(d["hs"], L["hs"]))),
        ("MOSS: Hs / max Hs(Tp)", lambda d: (("%d %%" % round(d["moss"] * 100)) if d["moss"] is not None else "—",
                                              status_of(d["moss"], 1.0) if d["moss"] is not None else None)),
        ("Gust %d m avg (max) m/s" % zL, lambda d: (am(d["wgL"], d["wgL_max"]), status_of(d["wgL"], L["wgL"]))),
        ("Wind %d m avg (max) m/s" % zH, lambda d: (am(d["wsH"], d["wsH_max"]), status_of(d["wsH"], L["wsH"]))),
        ("Sea temperature (°C)", lambda d: (v(d["sst"], ".1f"), None)),
        ("Lightning", lambda d: (d["ltg"], LTG_STATUS[d["ltg"]])),
        ("Workable hours (all limits OK)", lambda d: (("%s h / %d" % (d["op_hours"], d["work_hours"])) if d["op_hours"] is not None else "—",
                                             None if d["op_hours"] is None else (0 if d["op_hours"] >= 6 else 1 if d["op_hours"] > 0 else 2))),
        ("Model · confidence", lambda d: ("%s · %s" % (d["src"], {0: "high", 1: "med", 2: "low"}[max(r["conf"] for r in d["rows"])]), None)),
    ]
    data = [hdr_row]
    cmds = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, WHITE), ("LINEAFTER", (0, 0), (-1, -1), 0.4, WHITE),
            ("TOPPADDING", (0, 0), (-1, -1), 1.3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
    for i, (lab, fn) in enumerate(lines, start=1):
        row = [P(lab, 6.6, True, NAVY)]
        cmds.append(("BACKGROUND", (0, i), (0, i), SKY))
        for j, d in enumerate(days, start=1):
            txt, st = fn(d)
            row.append(cellp(txt, st))
            cmds.append(("BACKGROUND", (j, i), (j, i), STATUS_BG.get(st, GREY)))
        data.append(row)
    dtab = Table(data, colWidths=[lw] + [dw] * nd)
    dtab.setStyle(TableStyle(cmds))
    page1 += [dtab, Spacer(1, 0.1 * cm)]

    # ---------- légende courte (page 1) ----------
    key = ("<b>How to read:</b> "
           "<font color='%s'><b>green</b></font> &lt; %d%% of limit · <font color='%s'><b>amber</b></font> %d–100%% · "
           "<font color='%s'><b>red</b></font> limit reached. "
           "<b>Limits</b>: Hs %.1f m · gust %d m %d m/s · mean wind %d m %d m/s · lightning MED/HIGH. "
           "<b>MOSS</b> = Hs as %% of the max operating Hs for the forecast Tp (Table 18). "
           "Daily values are computed over working hours %s: wind = average (max in brackets). "
           "<b>Workable hours</b> = hours between %s where every limit is met (e.g. 5 h / 12)."
           % (ST_SOLID[0], CONFIG["warn_ratio"] * 100, ST_SOLID[1], CONFIG["warn_ratio"] * 100, ST_SOLID[2],
              L["hs"], zL, L["wgL"], zH, L["wsH"], WL, WL))
    page1 += [P(key, 6.3, False, TEXT, leading=7.6), Spacer(1, 0.1 * cm)]

    syn = Table([[P(synopsis(rows, days), 7.2, False, TEXT, leading=9)]], colWidths=[W])
    syn.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREY), ("LINEBEFORE", (0, 0), (0, -1), 2.5, RED),
                             ("LEFTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))

    # ---------- graphiques : hauteur = place restante ----------
    used = sum(f.wrap(W, f1h)[1] for f in page1) + syn.wrap(W, f1h)[1] + 0.25 * cm
    ch = max(4.5 * cm, f1h - used - 0.15 * cm)
    story = [NextPageTemplate("later")] + page1
    try:
        buf = build_charts(rows, W / cm, ch / cm, meta["short_until"])
        story.append(Image(buf, width=W, height=ch))
    except Exception as e:
        story.append(P("[Charts unavailable: %s]" % e, 7, False, MUTED))
    story += [Spacer(1, 0.1 * cm), syn, PageBreak()]

    # ---------- page 2 : légende complète AVANT le tableau ----------
    leg = [
        "<b>Colour code</b> — green: below %d%% of limit · amber: %d–100%% of limit · red: limit reached. "
        "Limits: Hs %.1f m · gust %d m %d m/s · mean wind %d m %d m/s · lightning MED/HIGH · MOSS: Hs ≥ max operating Hs for Tp. "
        "First cell of each row (date) = worst criterion of the hour. <b>Conf</b> (model confidence): green high (≤48 h and AROME/ECMWF agree), "
        "amber medium, red low (&gt; 96 h). Grey rows = outside working hours %s."
        % (CONFIG["warn_ratio"] * 100, CONFIG["warn_ratio"] * 100, L["hs"], zL, L["wgL"], zH, L["wsH"], WL),
        "<b>Columns</b> — Ws = mean wind speed, Wg = gust, number = height above sea (m). Dir = direction the wind / waves come FROM (°, 0 = North, 90 = East). "
        "Hs = significant wave height, Hmax = estimated max wave (1.86 × Hs), Tp = peak period, Tm = mean period. "
        "MOSS = Hs / max operating Hs(Tp) in %. Vis = visibility, T2m = air temperature, Sea T = sea surface temperature, Prec = rain, Ltg = lightning risk.",
        "<b>Wind at height (extrapolation model)</b> — the weather models give the wind at 10 m and 100 m. The wind at %d m and %d m is computed with a "
        "power-law profile fitted on these two levels: V(z) = V100 × (z/100)^α above 100 m, V(z) = V10 × (z/10)^α below, with α = ln(V100/V10) / ln(10) "
        "(bounded 0.02–0.35, default 0.12). Gusts at height = mean wind × gust factor of 10 m, attenuated with height ((10/z)^0.25)." % (zL, zH),
        "<b>Src</b> — AH: Météo-France AROME France HD 1.5 km · AR: AROME France 2.5 km (hourly, ~2 days, updated every 3 h) · "
        "EC: ECMWF IFS (up to 10 days) · BM: Open-Meteo best match (fallback). Models used for this issue: %s. "
        "<b>Sea</b>: waves %s; sea temperature Météo-France MFWAM, SMOC currents (Open-Meteo Marine). "
        "<b>Lightning</b>: CAPE (NIL &lt;300, LOW 300–999, MED 1000–2499, HIGH ≥2500 J/kg) or WMO codes 95/96/99. "
        "Data via open-meteo.com (CC BY 4.0); waves Ifremer/MARC (Licence Ouverte Etalab)." % (meta["models_used_txt"], WAVES_LABEL),
    ]
    legt = Table([[P(x.replace("≤", "&lt;=").replace("≥", "&gt;="), 6.2, False, TEXT, leading=7.6)] for x in leg], colWidths=[W])
    legt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREY), ("LINEBEFORE", (0, 0), (0, -1), 2.5, NAVY),
                              ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2)]))
    story += [legt, Spacer(1, 0.2 * cm)]

    # ---------- tableau détaillé : un bloc par jour, en-têtes répétés ----------
    groups = [("", 3), ("WIND (m/s)", 7), ("TOTAL SEA", 6), ("WIND WAVES", 3), ("SWELL", 3),
              ("WEATHER", 5), ("CURRENT", 2)]
    labels = ["Date / time", "Src", "Conf",
              "Dir", "Ws10", "Wg10", "Ws%d" % zL, "Wg%d" % zL, "Ws%d" % zH, "Wg%d" % zH,
              "Dir", "Hs", "Hmax", "Tp", "Tm", "MOSS",
              "Dir", "H", "T",
              "Dir", "H", "T",
              "Vis", "T2m", "Sea T", "Prec", "Ltg",
              "Dir", "Spd"]
    units = ["local", "model", "",
             "° from", "", "", "", "lim %d" % L["wgL"], "lim %d" % L["wsH"], "",
             "° from", "m · lim %.1f" % L["hs"], "m", "s", "s", "% lim",
             "°", "m", "s", "°", "m", "s",
             "km", "°C", "°C", "mm", "",
             "° to", "m/s"]
    ncol = len(labels)
    C_WGL, C_WSH, C_HS, C_MOSS, C_VIS, C_LTG = 7, 8, 11, 15, 22, 26
    fixed = {0: 1.75 * cm, 1: 0.6 * cm, 2: 0.75 * cm, C_LTG: 0.9 * cm, 11: 1.05 * cm}
    rest_w = (W - sum(fixed.values())) / (ncol - len(fixed))
    CW = [fixed.get(i, rest_w) for i in range(ncol)]

    hcell = lambda t, s=6.1: P("<b>%s</b>" % t, s, True, WHITE, TA_CENTER, s + 1)
    g_row, c, spans = [], 0, []
    for name, n in groups:
        g_row += [hcell(name, 6.3)] + [""] * (n - 1)
        if n > 1:
            spans.append(("SPAN", (c, 1), (c + n - 1, 1)))
        c += n
    l_row = [hcell(x) for x in labels]
    u_row = [P(x, 5.1, False, _hex("#C9D6F2"), TA_CENTER, 6) for x in units]
    base_cmds = [("BACKGROUND", (0, 1), (-1, 3), NAVY), ("BACKGROUND", (0, 1), (-1, 1), NAVY_D),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 1), (-1, -1), "CENTER"),
                 ("TOPPADDING", (0, 0), (-1, -1), 0.8), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.8),
                 ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                 ("LINEBELOW", (0, 3), (-1, -1), 0.25, LINE)] + spans
    for cc in (2, 9, 15, 18, 21, 26):
        base_cmds.append(("LINEAFTER", (cc, 1), (cc, -1), 0.8, NAVY))

    def cell(txt, bold=False, color=TEXT):
        return P(txt, 6.2, bold, color, TA_CENTER, 7.1)

    sel = [r for r in rows if r["lead"] <= 48 or (r["lead"] <= 120 and r["dt"].hour % 3 == 0)
           or r["dt"].hour % 6 == 0]
    by_day = []
    for r in sel:
        if not by_day or by_day[-1][0] != r["dt"].date():
            by_day.append((r["dt"].date(), []))
        by_day[-1][1].append(r)

    for k, (dday, drows) in enumerate(by_day):
        rise_d, sset_d = sun_times(lat, lon, dday)
        step = "hourly" if drows[0]["lead"] <= 48 else ("3-hourly" if drows[0]["lead"] <= 120 else "6-hourly")
        band_row = [P("<b>%s</b>   <font color='%s'>sunrise %s · sunset %s · working hours %s · %s</font>"
                      % (dday.strftime("%A %d %B").upper(), EM_MUTED, hhmm(rise_d), hhmm(sset_d), WL, step),
                      7, True, NAVY)] + [""] * (ncol - 1)
        table = [band_row, list(g_row), list(l_row), list(u_row)]
        tcmds = list(base_cmds) + [("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), SKY),
                                    ("ALIGN", (0, 0), (-1, 0), "LEFT"), ("TOPPADDING", (0, 0), (-1, 0), 2.2),
                                    ("BOTTOMPADDING", (0, 0), (-1, 0), 2.2), ("LEFTPADDING", (0, 0), (-1, 0), 4)]
        stripe = 0
        for r in drows:
            i = len(table)
            st = r["st"]
            ab = MODEL_LABELS.get(r["src"], ("?", ""))[0]
            vis = fnum(r["vis"])
            ms = r["moss_st"]
            row = [
                cell(r["dt"].strftime("%d/%m %H:%M"), True, WHITE if r["status"] == 2 else TEXT),
                cell(ab, False, MUTED), cell(""),
                cell(v(r["dir"])), cell(v(r["ws10"])), cell(v(r["wg10"])),
                cell(v(r["wsL"])),
                cell(v(r["wgL"]), st["wgL"] == 2, STATUS_FG.get(st["wgL"], TEXT)),
                cell(v(r["wsH"]), st["wsH"] == 2, STATUS_FG.get(st["wsH"], TEXT)),
                cell(v(r["wgH"])),
                cell(v(r["wdir"])),
                cell(v(r["hs"], ".1f"), st["hs"] == 2, STATUS_FG.get(st["hs"], TEXT)),
                cell(v(r["hmax"], ".1f")), cell(v(r["tp"], ".1f")), cell(v(r["tm"], ".1f")),
                cell(("%d" % round(r["moss_ratio"] * 100)) if r["moss_ratio"] is not None else "—",
                     bool(ms), STATUS_FG.get(ms, TEXT)),
                cell(v(r["wwdir"])), cell(v(r["wwh"], ".1f")), cell(v(r["wwp"], ".1f")),
                cell(v(r["swdir"])), cell(v(r["swh"], ".1f")), cell(v(r["swp"], ".1f")),
                cell(v(vis / 1000.0) if vis is not None else "—", vis is not None and vis < 1000,
                     STATUS_FG[2] if vis is not None and vis < 1000 else TEXT),
                cell(v(r["t2m"])), cell(v(r["sst"], ".1f")), cell(v(r["prec"], ".1f")),
                cell(r["ltg"], r["ltg"] in ("MED", "HIGH"), STATUS_FG.get(LTG_STATUS[r["ltg"]], TEXT)),
                cell(v(r["curdir"])), cell(v(r["cur"], ".1f")),
            ]
            table.append(row)
            base_bg = _hex("#DCE1EA") if not r["work"] else (GREY if stripe % 2 else WHITE)
            tcmds.append(("BACKGROUND", (0, i), (-1, i), base_bg))
            if not r["work"]:
                tcmds.append(("TEXTCOLOR", (0, i), (-1, i), MUTED))
            stripe += 1
            if r["status"] is not None:
                tcmds.append(("BACKGROUND", (0, i), (0, i), _hex(ST_SOLID[r["status"]]) if r["status"] == 2 else STATUS_BG[r["status"]]))
            tcmds.append(("BACKGROUND", (2, i), (2, i), _hex(ST_SOLID[r["conf"]])))
            for col, s_ in ((C_WGL, st["wgL"]), (C_WSH, st["wsH"]), (C_HS, st["hs"]), (C_LTG, st["ltg"]), (C_MOSS, ms)):
                if s_:
                    tcmds.append(("BACKGROUND", (col, i), (col, i), STATUS_BG[s_]))
                elif s_ == 0 and col != C_LTG:
                    tcmds.append(("BACKGROUND", (col, i), (col, i), _hex("#E6F5EA")))
            if vis is not None and vis < 1000:
                tcmds.append(("BACKGROUND", (C_VIS, i), (C_VIS, i), STATUS_BG[2]))
        t = Table(table, colWidths=CW, repeatRows=4)
        t.setStyle(TableStyle(tcmds))
        story += [t, Spacer(1, 0.15 * cm)]

    doc.build(story)
    return output_path

# =============================================================================
# EMAIL
# =============================================================================

VERDICT_COL = {"GO": ("#D3EFD9", "#1E6B34"), "LIMITED WINDOW": ("#FFE3A8", "#7A4B00"), "NO-GO": ("#F7B9B0", "#8E1B0E")}

def window_txt(d):
    w = d.get("window")
    if not w:
        return "—"
    return "%s–%s (%d h)" % (w[0].strftime("%H:%M"), w[1].strftime("%H:%M"), w[2])

def email_summary(issue_time, ltg_level, days, meta, test):
    """Retourne (texte brut, html) du résumé d'opérabilité."""
    L = CONFIG["limits"]
    rows_txt, rows_html = [], []
    for i, d in enumerate([x for x in days if x["work_hours"]][:4]):
        lab = ("Today (rest)" if d["date"] == issue_time.date() else d["date"].strftime("%a %d/%m"))
        verdict = d.get("verdict") or "n/a"
        why = ("limited by " + d["limiting"]) if d.get("limiting") and verdict != "GO" else ""
        rows_txt.append("  %-13s %-15s window %-20s Hs %s m · gust%d avg %s m/s · wind%d avg %s m/s %s"
                        % (lab, verdict, window_txt(d), v(d["hs"], ".1f"), CONFIG["h_low"], v(d["wgL"]), CONFIG["h_high"], v(d["wsH"]),
                           ("· " + why) if why else ""))
        bg, fg = VERDICT_COL.get(verdict, ("#F4F6FA", "#1E2433"))
        rows_html.append(
            "<tr>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0;font-weight:bold;color:#1B3A8C'>%s</td>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0;background:%s;color:%s;font-weight:bold;text-align:center'>%s</td>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0'>%s</td>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0;text-align:center'>%s</td>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0;text-align:center'>%s</td>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0;text-align:center'>%s</td>"
            "<td style='padding:6px 10px;border-bottom:1px solid #E1E6F0;color:#6B7385'>%s</td>"
            "</tr>" % (lab, bg, fg, verdict, window_txt(d), v(d["hs"], ".1f"), v(d["wgL"]), v(d["wsH"]), why or "&nbsp;"))
    crit = ("GO = at least %d consecutive working hours (%s) with all limits met (Hs &lt; %.1f m, gust %d m &lt; %d m/s, "
            "mean wind %d m &lt; %d m/s, no MED/HIGH lightning) · LIMITED WINDOW = favourable window shorter than that · "
            "NO-GO = no favourable working hour. Wind values = average over working hours. Decision support only — the final go/no-go remains with the "
            "marine coordinator." % (CONFIG["min_window_h"], work_label(), L["hs"], CONFIG["h_low"], L["wgL"], CONFIG["h_high"], L["wsH"]))
    test_html = ("<p style='background:#FFE3A8;color:#7A4B00;padding:6px 10px;font-weight:bold;margin:0 0 10px'>"
                 "NEW VERSION v3 — test until %s — please send your comments and remarks to %s</p>"
                 % (test_end_label(), CONFIG["feedback_to"])) if test else ""
    ltg_html = ("<p style='margin:0 0 10px'><b>Lightning now:</b> %s</p>" % ltg_level)
    html = (
        "<html><body style='font-family:Helvetica,Arial,sans-serif;font-size:13px;color:#1E2433'>"
        "<div style='border-top:4px solid #1B3A8C;padding-top:8px'>"
        "<p style='font-size:16px;font-weight:bold;color:#1B3A8C;margin:0'>EolMed offshore weather — operability summary</p>"
        "<p style='color:#6B7385;margin:2px 0 10px'>%s · issued %s %s</p>%s%s"
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;font-size:12px'>"
        "<tr style='background:#1B3A8C;color:#ffffff'>"
        "<th style='padding:6px 10px;text-align:left'>Day</th><th style='padding:6px 10px'>Verdict</th>"
        "<th style='padding:6px 10px;text-align:left'>Best window (work hours)</th><th style='padding:6px 10px'>Hs max (m)</th>"
        "<th style='padding:6px 10px'>Gust %d m avg (m/s)</th><th style='padding:6px 10px'>Wind %d m avg (m/s)</th>"
        "<th style='padding:6px 10px;text-align:left'>Comment</th></tr>%s</table>"
        "<p style='color:#6B7385;font-size:11px;margin:8px 0'>%s</p>"
        "<p style='margin:8px 0'>Full 10-day forecast (charts and hourly table) in the attached PDF.</p>"
        "<p style='color:#6B7385;font-size:11px'>Models: %s · Waves: %s<br/>"
        "EolMed MetOcean Reporter (automated)</p></div></body></html>"
    ) % (CONFIG["site_name"], issue_time.strftime("%d/%m/%Y %H:%M"), tz_label(issue_time.date()),
         test_html, ltg_html, CONFIG["h_low"], CONFIG["h_high"], "".join(rows_html), crit, meta["models_used_txt"], WAVES_LABEL)
    text = (
        "EolMed offshore weather — operability summary\n"
        "%s · issued %s %s\n%s"
        "Lightning now: %s\n\n%s\n\n%s\n\n"
        "Full 10-day forecast in the attached PDF.\nModels: %s\n--\nEolMed MetOcean Reporter (automated)"
    ) % (CONFIG["site_name"], issue_time.strftime("%d/%m/%Y %H:%M"), tz_label(issue_time.date()),
         ("NEW VERSION v3 — test until %s — please send your comments and remarks to %s\n" % (test_end_label(), CONFIG["feedback_to"])) if test else "",
         ltg_level, "\n".join(rows_txt), crit.replace("&lt;", "<"), meta["models_used_txt"])
    return text, html

def send_email(pdf_path, issue_time, ltg_level, days, meta):
    h = issue_time.hour
    slot = "05:30" if h < 8 else "11:00" if h < 13 else "16:00" if h < 18 else "21:00"
    test = in_test_phase(issue_time.date())
    ltg_risk = ltg_level in ("MED", "HIGH") or meta.get("ltg_next", ("NIL", None))[0] in ("MED", "HIGH")
    subject = "%sEolMed Weather Report — %s %s%s" % ("[TEST v3] " if test else "", issue_time.strftime("%d/%m/%Y"), slot,
                                                   " ⚡" if ltg_risk else "")
    text, html = email_summary(issue_time, ltg_level, days, meta, test)
    msg = MIMEMultipart("mixed")
    msg["From"] = CONFIG["email_from"]
    msg["To"] = ", ".join(CONFIG["email_to"])
    msg["Subject"] = subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "pdf"); part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment; filename=%s" % os.path.basename(pdf_path))
        msg.attach(part)
    with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"], timeout=30) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(CONFIG["email_from"], CONFIG["email_password"])
        s.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
    print("  ✅ Email envoyé — sujet : %s" % subject)

ANNOUNCE_SUBJECT = "[TEST] EolMed – Nouveau rapport météo-océanique : phase de test du {start} au {end}"
ANNOUNCE_BODY = """Bonjour à toutes et à tous,

Nous lançons aujourd'hui une phase de test de 7 jours (du {start} au {end} inclus) pour le nouveau rapport météo-océanique automatique du site EolMed.

Pendant cette période, vous recevrez le rapport 4 fois par jour (05h30, 11h00, 16h00, 21h00), avec la mention [TEST] dans l'objet. Vous trouverez ci-joint un premier exemplaire.

Principales évolutions :
- nouvelle mise en page inspirée du format StormGeo, aux couleurs EolMed : synthèse en première page (indicateurs clés sur 24 h, tableau de synthèse sur 10 jours, graphiques vagues et vent), puis tableau détaillé ;
- code couleur vert / orange / rouge selon les seuils opérationnels (Hs 1,5 m, rafale à 10 m 15 m/s, vent moyen à 150 m 10 m/s, foudre) et nombre d'heures de jour exploitables ;
- modèles météo : AROME France HD 1,5 km et AROME 2,5 km (Météo-France) sur les ~48 premières heures, puis ECMWF IFS jusqu'à 10 jours ; vagues Météo-France MFWAM.

Ce rapport est automatique et ne remplace pas l'avis d'un météorologue.

À l'issue de la phase de test, nous validerons le format définitif en tenant compte de vos retours. Merci d'envoyer tous vos commentaires et remarques à {feedback}.

Bien cordialement,
Nicolas Dalmon
"""

ANNOUNCE_V3_SUBJECT = "[TEST v3] EolMed – Nouvelle version du rapport météo-océanique (v3) : vos retours pris en compte"
ANNOUNCE_V3_BODY = """Bonjour à toutes et à tous,

Merci pour vos retours sur la phase de test du rapport météo-océanique EolMed. Une nouvelle version (v3) est en service à partir d'aujourd'hui ; vous en trouverez un exemplaire ci-joint. Vous continuerez à le recevoir 4 fois par jour (05h30, 11h00, 16h00, 21h00).

PRINCIPAUX CHANGEMENTS
- Heures travaillées : tous les indicateurs (tuiles, synthèse journalière, verdict GO / NO-GO du mail) sont calculés sur {wl}, et non plus du lever au coucher du soleil.
- Vent à {zl} m et {zh} m (au lieu de 10 m et 150 m), en moyenne sur les heures travaillées, avec le maximum entre parenthèses.
- « Workable hours » = nombre d'heures entre {wl} où tous les seuils sont respectés (ex. 5 h / 12).
- Critère MOSS : Hs prévue en % de la Hs maximale d'exploitation pour la période Tp prévue (Table 18), dans la synthèse et le tableau horaire.
- Température de l'eau ajoutée.
- Éphémérides : lever / coucher du soleil et durée du jour.
- Lisibilité : légende « How to read » en page 1, légende complète en tête du tableau horaire (plus de dernière page isolée), en-têtes de colonnes répétés pour chaque jour, heures et dates sur les graphiques, direction du vent écrite en clair, logo corrigé.
- Méthode d'extrapolation du vent en hauteur détaillée dans la légende.

Seuils inchangés : Hs 1,5 m · rafale 15 m/s (désormais à {zl} m) · vent moyen 10 m/s (désormais à {zh} m) · foudre MED/HIGH.

PAGE WEB EN DIRECT
{site}
Accessible depuis un ordinateur ou un téléphone, sans identifiant. Elle se met à jour toutes les 30 minutes et reprend les mêmes calculs que le rapport : carte animée (vent, rafales, vagues, houle, orages…), bandeau des 72 prochaines heures, synthèse sur 7 jours (verdict, heures exploitables, Hs, MOSS, vent 20 m / 120 m, température de l'eau), comparaison des modèles météo et une rubrique « Comment lire cette page ».

Ce rapport et cette page sont automatiques et ne remplacent pas l'avis d'un météorologue ; la décision de sortie reste au coordinateur marine.

Cette version est en test jusqu'au {end} : merci d'envoyer vos remarques à {feedback}.

Bien cordialement,
Nicolas Dalmon
"""

def send_announcement_v3(pdf_path, preview_only=False):
    fmt = dict(zl=CONFIG["h_low"], zh=CONFIG["h_high"], wl=work_label(), end=test_end_label(), feedback=CONFIG["feedback_to"],
               site=CONFIG["website_url"])
    subject, body = ANNOUNCE_V3_SUBJECT.format(**fmt), ANNOUNCE_V3_BODY.format(**fmt)
    if preview_only:
        with open(pdf_path.replace(".pdf", "_annonce_v3.txt"), "w", encoding="utf-8") as f:
            f.write(subject + "\n\n" + body)
        print("  Aperçu de l'annonce v3 écrit (non envoyé)")
        return
    msg = MIMEMultipart()
    msg["From"] = CONFIG["email_from"]
    msg["To"] = ", ".join(CONFIG["email_to"])
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "pdf"); part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment; filename=%s" % os.path.basename(pdf_path))
        msg.attach(part)
    with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"], timeout=30) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(CONFIG["email_from"], CONFIG["email_password"])
        s.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
    print("  ✅ Annonce v3 envoyée à %d destinataires — %s" % (len(CONFIG["email_to"]), subject))

def send_announcement(pdf_path):
    start = date.today().strftime("%d/%m/%Y")
    fmt = dict(start=start, end=test_end_label(), feedback=CONFIG["feedback_to"])
    msg = MIMEMultipart()
    msg["From"] = CONFIG["email_from"]
    msg["To"] = ", ".join(CONFIG["email_to"])
    msg["Subject"] = ANNOUNCE_SUBJECT.format(**fmt)
    msg.attach(MIMEText(ANNOUNCE_BODY.format(**fmt), "plain", "utf-8"))
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "pdf"); part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment; filename=%s" % os.path.basename(pdf_path))
        msg.attach(part)
    with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"], timeout=30) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(CONFIG["email_from"], CONFIG["email_password"])
        s.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
    print("  ✅ Annonce envoyée à %d destinataires — %s" % (len(CONFIG["email_to"]), msg["Subject"]))

def send_text_message(path):
    """Envoie un mail texte simple (1re ligne du fichier = objet) à CONFIG['email_to']."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    subject, body = lines[0].strip(), "\n".join(lines[1:]).lstrip("\n")
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = CONFIG["email_from"]
    msg["To"] = ", ".join(CONFIG["email_to"])
    msg["Subject"] = subject
    with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"], timeout=30) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(CONFIG["email_from"], CONFIG["email_password"])
        s.sendmail(CONFIG["email_from"], CONFIG["email_to"], msg.as_string())
    print("  ✅ Message envoyé à %d destinataires — %s" % (len(CONFIG["email_to"]), subject))

# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-only", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--announce", action="store_true",
                        help="envoie le mail d'annonce de la phase de test (avec ce rapport en PJ) au lieu du mail habituel")
    parser.add_argument("--announce-v3", action="store_true",
                        help="envoie le mail d'annonce de la v3 (avec ce rapport en PJ) au lieu du mail habituel")
    parser.add_argument("--output", default=CONFIG["output_dir"])
    parser.add_argument("--preview-email", action="store_true", help="écrit le corps du mail en HTML à côté du PDF, sans envoi")
    parser.add_argument("--send-text", default="", help="envoie un mail texte (1re ligne = objet) puis s'arrête")
    parser.add_argument("--to", default="", help="destinataires (séparés par des virgules) remplaçant la liste pour cet envoi")
    args = parser.parse_args()
    if args.to.strip():
        CONFIG["email_to"] = [x.strip() for x in args.to.split(",") if x.strip()]
    if args.send_text:
        send_text_message(args.send_text)
        return

    issue_time = datetime.now()
    print("\nEolMed Weather Report — %s" % issue_time.strftime("%d/%m/%Y %H:%M"))
    print("=" * 60)
    lat, lon, tz = CONFIG["latitude"], CONFIG["longitude"], CONFIG["timezone"]

    if args.test:
        print("  Mode TEST — données simulées")
        sources, base_times = mock_sources()
        marine = mock_marine(base_times)
        strikes = None
    else:
        print("  [1/3] Atmosphère — AROME HD / AROME / ECMWF (Open-Meteo)...")
        sources = fetch_atmosphere(lat, lon, tz)
        print("  [2/3] Vagues — Open-Meteo Marine (MFWAM)...")
        marine = fetch_marine(lat, lon, tz)
        if fetch_ifremer_waves:
            print("  [2b/3] Vagues — Ifremer WAVEWATCH III (MARC)...")
            wav = fetch_ifremer_waves(lat, lon, tz, sorted(marine.get("wave_height", {}).keys()))
            if wav:
                for k in ("wave_height", "wave_peak_period", "wave_direction"):
                    marine.setdefault(k, {}).update(wav[k])
                global WAVES_LABEL
                WAVES_LABEL = "Ifremer WAVEWATCH III (MARC, 6 days), then Météo-France MFWAM"
        print("  [3/3] Foudre — Blitzortung...")
        strikes = fetch_blitzortung(lat, lon, CONFIG["lightning_radius_km"])
        print("  %s" % ("✅ Blitzortung : %d impact(s)" % strikes if strikes is not None
                        else "⚠ Blitzortung non disponible → CAPE / codes OMM"))

    times, atm, src, ec_ws = merge_atmosphere(sources)
    key = issue_time.strftime("%Y-%m-%dT%H:00")
    start_idx = times.index(key) if key in times else 0
    rows = build_rows(times, atm, src, ec_ws, marine, start_idx)
    days = daily_summary(rows, lat, lon)

    # foudre courante et prochaine
    ltg_now = rows[0]["ltg"] if rows else "NIL"
    if strikes and strikes > 5 and ltg_now == "NIL":
        ltg_now = "LOW"
    if strikes and strikes > 20 and LTG_RANK[ltg_now] < 2:
        ltg_now = "MED"
    nxt = max(rows[:48], key=lambda r: LTG_RANK[r["ltg"]]) if rows else None
    ltg_next = (nxt["ltg"], nxt["dt"] if nxt and nxt["ltg"] != "NIL" else None) if nxt else ("NIL", None)

    used = []
    for m, _ in sources:
        lab = MODEL_LABELS.get(m, (m, m))[1]
        if lab not in used:
            used.append(lab)
    short_rows = [r for r in rows if r["src"] in CONFIG["models_short"]]
    short_until = short_rows[-1]["dt"] if short_rows else None
    shorts = [MODEL_LABELS[m][0] for m, _ in sources if m in CONFIG["models_short"]]
    longs = [MODEL_LABELS[m][0] for m, _ in sources if m in CONFIG["models_long"]]
    h = issue_time.hour
    round_h = "05:30" if h < 8 else "11:00" if h < 13 else "16:00" if h < 18 else "21:00"
    meta = {
        "issue_time": issue_time,
        "issue_label": "%s run · issued %s %s" % (round_h, issue_time.strftime("%a %d %b %Y %H:%M"), tz_label(issue_time.date())),
        "strikes": strikes, "ltg_now": ltg_now, "ltg_next": ltg_next,
        "models_used_txt": ", ".join(used),
        "models_short_txt": "%s (0–%s h) · %s (to %d d) · waves: %s" % (
            "/".join(shorts) or "—",
            str(short_rows[-1]["lead"]) if short_rows else "0",
            "/".join(longs) or "BM", CONFIG["forecast_days"],
            "Ifremer WW3 + MFWAM" if WAVES_LABEL.startswith("Ifremer") else "MFWAM"),
        "short_until": short_until,
    }

    os.makedirs(args.output, exist_ok=True)
    fname = "EolMed_Weather_%s.pdf" % issue_time.strftime("%Y%m%d_%H%M")
    outpath = os.path.join(args.output, fname)
    print("  Génération PDF : %s ..." % fname)
    build_pdf(rows, days, meta, outpath)
    print("  ✅ PDF : %s" % outpath)
    print("  ⚡ Lightning : %s" % ltg_now)
    print("  Modèles : %s" % meta["models_used_txt"])

    if args.preview_email:
        text, html = email_summary(issue_time, ltg_now, days, meta, in_test_phase(issue_time.date()))
        with open(outpath.replace(".pdf", "_email.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(text)
        send_announcement_v3(outpath, preview_only=True)
    if args.announce_v3 and not args.test:
        print("  Envoi de l'annonce v3...")
        send_announcement_v3(outpath)
    elif args.announce and not args.test:
        print("  Envoi de l'annonce phase de test...")
        send_announcement(outpath)
    elif not args.pdf_only and not args.test:
        print("  Envoi email...")
        send_email(outpath, issue_time, ltg_now, days, meta)
    else:
        print("  Email non envoyé (mode pdf-only ou test)")
    print("Terminé.\n")

if __name__ == "__main__":
    main()
