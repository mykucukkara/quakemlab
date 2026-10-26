"""Yapılandırma katmanı — bölgeyi (region) YAML'dan yükler ve pipeline'ın ihtiyaç
duyduğu tüm türetilmiş nesneleri/sabitleri kurar.

Tasarım: bölgeye özel HER ŞEY `regions/<bolge>.yaml` içindedir (istasyonlar,
koordinatlar, hız modeli, arama kutusu, eşikler, cihaz-yanıtı kaynağı). Kod hiç
değişmeden yeni bölge eklenebilir: yeni bir YAML + BOLGE=<ad>.

`engine.py` bu modülü `from .config import *` ile içe aktarır; bu yüzden burada
tanımlı adlar (STATIONS, station_df, associator, XLIM/YLIM, eşikler...) motorda
doğrudan görünür. Çalışma-zamanı DEĞİŞEN durum burada DEĞİL, `engine.py`/`state`
içindedir (import sabittir; yalnızca okunur).
"""
import os
import pathlib

import pandas as pd
import pyproj
import yaml

try:
    import seisbench
    from pyocto import OctoAssociator, VelocityModel1D
except ImportError:  # pragma: no cover
    raise SystemExit("Lütfen 'pip install seisbench pyocto' çalıştırın.")


# ---------------------------------------------------------------------------
# Genel (bölgeden bağımsız) ortam yapılandırması
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
X_API_KEY = os.environ.get("X_API_KEY")
X_API_SECRET = os.environ.get("X_API_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET")
X_TWEET_LINK = os.environ.get("X_TWEET_LINK", "0") == "1"
WEB_APP_URL = os.environ.get("WEB_APP_URL", "http://localhost:8501")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "afad-sismik-veri-1")
PYOCTO_API_URL = os.environ.get(
    "PYOCTO_API_URL", "https://sismik-api-600261694418.europe-west3.run.app")

MODEL_STR = f"PhaseNet (instance) / SeisBench {getattr(seisbench, '__version__', '?')}"

# SeedLink (birincil) ve FDSN (yedek) zamanlama sabitleri — bölgeden bağımsız.
SL_FRAME = 520
SL_RECONNECT_SEC = 15
SL_MAX_FAILS = 3
SL_SOCK_TIMEOUT = 60
FETCH_WINDOW = 300
FDSN_OVERLAP = 15
FDSN_END_LAG = 15
FDSN_POLL_SEC = 60
HEARTBEAT_INTERVAL = 300
POOL_RETENTION_SEC = 900
STALE_LAG_SEC = 600
BUFFER_SIZE_SECONDS = 900
REFINE_MIN_AGE_SEC = 480
REFINE_MAX_COUNT = 2

# FDSN sağlayıcı taban adresleri (dataselect + station). ObsPy'ın servis keşfi
# konteynerde takıldığı için doğrudan HTTP kullanılır.
FDSN_BASE = {
    "NOA":    "https://eida.gein.noa.gr",
    "GEOFON": "https://geofon.gfz.de",
    "INGV":   "https://webservices.ingv.it",
    "IRIS":   "https://service.earthscope.org",
}

# Wood-Anderson simülasyon parametreleri (ML için).
PAZ_WA = {"poles": [-6.283185 - 4.712389j, -6.283185 + 4.712389j],
          "zeros": [0 + 0j], "gain": 1.0, "sensitivity": 2080.0}
WA_PRE_FILT = [0.05, 0.1, 45.0, 50.0]

PICKS_CSV = "LIVE_PICKS.csv"
CATALOG_CSV = "LIVE_CATALOG.csv"
_CATALOG_HEADER = ("event_id,lat,lon,depth,origin_time,loc_name,mag,rms,n_sta,erh,erz,gap,"
                   "n_p,n_s,proc_s,model,delayed,proc_net_s,mw,mw_err,mw_n,fc")


# ---------------------------------------------------------------------------
# Bölge YAML'ını bul ve yükle
# ---------------------------------------------------------------------------
BOLGE = os.environ.get("BOLGE", "ege").strip().lower()


def _find_region_file(bolge):
    """Bölge YAML'ını sırayla arar: QUAKEMLAB_REGION_FILE, QUAKEMLAB_REGIONS_DIR,
    paket köküne göre regions/. Bulunamazsa hata."""
    env_file = os.environ.get("QUAKEMLAB_REGION_FILE")
    if env_file:
        return pathlib.Path(env_file)
    candidates = []
    env_dir = os.environ.get("QUAKEMLAB_REGIONS_DIR")
    if env_dir:
        candidates.append(pathlib.Path(env_dir) / f"{bolge}.yaml")
    here = pathlib.Path(__file__).resolve()
    # src/quakemlab_core/config.py -> repo kökü iki üst; ayrıca CWD/regions ve /app/regions
    candidates += [
        here.parent.parent.parent / "regions" / f"{bolge}.yaml",
        pathlib.Path.cwd() / "regions" / f"{bolge}.yaml",
        pathlib.Path("/app/regions") / f"{bolge}.yaml",
        here.parent / "regions" / f"{bolge}.yaml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise SystemExit(
        f"Bölge dosyası bulunamadı: '{bolge}'. Aranan yerler: "
        + ", ".join(str(c) for c in candidates)
        + ". QUAKEMLAB_REGION_FILE ile açıkça verebilirsiniz.")


REGION_FILE = _find_region_file(BOLGE)
with open(REGION_FILE, "r", encoding="utf-8") as _f:
    REGION = yaml.safe_load(_f)

_disp = REGION.get("display_name", {})
BOLGE_ADI = _disp.get("tr", BOLGE.title()) if isinstance(_disp, dict) else str(_disp or BOLGE.title())
EVENTS_DIR = REGION.get("events_dir", f"live_events_{BOLGE}" if BOLGE != "ege" else "live_events")

print(f"=== BOLGE: {BOLGE.upper()} | katalog: {EVENTS_DIR}/ | config: {REGION_FILE.name} ===")

# İstasyonlar: YAML'daki [host, port] listesi -> (host, port) tuple; sl:null -> None.
STATIONS = []
for s in REGION["stations"]:
    sl = s.get("sl")
    cfg = {"net": s["net"], "sta": s["sta"],
           "sl": (tuple(sl) if sl else None), "fdsn": s.get("fdsn")}
    if "fdsn_window" in s:
        cfg["fdsn_window"] = s["fdsn_window"]
    STATIONS.append(cfg)

# İstasyon koordinatları -> DataFrame (id, x=lon, y=lat, z).
_coords = REGION["station_coords"]
station_df = pd.DataFrame(
    [{"id": k, "x": float(v["x"]), "y": float(v["y"]), "z": float(v.get("z", 0.0))}
     for k, v in _coords.items()])

# Cihaz-yanıtı kaynağı (ML/Mw): fdsn grupları veya yerel StationXML klasörü.
_resp = REGION.get("response", {}) or {}
RESPONSE_SOURCE = _resp.get("source", "fdsn")
FDSN_RESPONSE_GROUPS = _resp.get("fdsn_groups", [])
STATION_XML_DIR = os.environ.get(
    "QUAKEMLAB_STATION_XML_DIR",
    str((REGION_FILE.parent.parent / "data" / "station_xml" / BOLGE)))

# Eşikler: YAML varsayılanı, env değişkeni verilirse env kazanır.
_TH = REGION.get("thresholds", {}) or {}


def _th(env_key, yaml_key, default, cast=float):
    v = os.environ.get(env_key)
    if v is not None:
        return cast(v)
    return cast(_TH.get(yaml_key, default))


PICK_THRESHOLD = _th("PICK_THRESHOLD", "pick_threshold", 0.65)
S_PICK_THRESHOLD = _th("S_PICK_THRESHOLD", "s_pick_threshold", 0.45)
MIN_P_STATIONS = _th("MIN_P_STATIONS", "min_p_stations", 3, int)
MIN_S_PICKS = _th("MIN_S_PICKS", "min_s_picks", 1, int)
SNR_MIN = _th("SNR_MIN", "snr_min", 4.0)
MIN_CLEAN_P_STATIONS = _th("MIN_CLEAN_P_STATIONS", "min_clean_p_stations", 2, int)
MW_MIN_ML = _th("MW_MIN_ML", "mw_min_ml", 3.0)
DELAY_OFFSET_SEC = _th("DELAY_OFFSET_SEC", "delay_offset_sec", 300, int)
DELAYED_STATIONS = {c["sta"] for c in STATIONS if c.get("sl") is None}

# ---------------------------------------------------------------------------
# Projeksiyon + arama kutusu (PyOcto km-Kartezyen düzlemde çalışır)
# ---------------------------------------------------------------------------
proj = pyproj.Proj(proj="utm", zone=int(REGION.get("utm_zone", 36)), ellps="WGS84")
station_df_km = station_df.copy()
_xk, _yk = proj(station_df["x"].values, station_df["y"].values)
station_df_km["x"] = _xk / 1000.0
station_df_km["y"] = _yk / 1000.0
PAD_KM = float(REGION.get("pad_km", 50.0))
XLIM = (float(station_df_km["x"].min() - PAD_KM), float(station_df_km["x"].max() + PAD_KM))
YLIM = (float(station_df_km["y"].min() - PAD_KM), float(station_df_km["y"].max() + PAD_KM))

# id -> (lat, lon) — magnitüd mesafe hesabında kullanılır.
_STA_LATLON = {r["id"]: (float(r["y"]), float(r["x"])) for _, r in station_df.iterrows()}


def to_lonlat(x_km, y_km):
    lon, lat = proj(x_km * 1000.0, y_km * 1000.0, inverse=True)
    return float(lon), float(lat)


# ---------------------------------------------------------------------------
# Hız modeli + PyOcto associator
# ---------------------------------------------------------------------------
_vm = REGION["velocity_model"]
model_df = pd.DataFrame({"depth": _vm["depth"], "vp": _vm["vp"], "vs": _vm["vs"]})
ZLIM = tuple(REGION.get("zlim", [0.0, 200.0]))
MODEL_XDIST = float(REGION.get("model_xdist", 800.0))
MODEL_ZDIST = float(REGION.get("model_zdist", 300.0))

model_path = f"live_velocity_model_{BOLGE}"
if not os.path.exists(model_path):
    VelocityModel1D.create_model(model_df, delta=1.0, xdist=MODEL_XDIST,
                                 zdist=MODEL_ZDIST, path=model_path)
velocity_model = VelocityModel1D(model_path, tolerance=2.0)

# DETEKSİYON için associator (canlı seyrek ağda hassasiyet; xlim/ylim KM).
associator = OctoAssociator(
    xlim=XLIM, ylim=YLIM, zlim=ZLIM,
    velocity_model=velocity_model,
    time_before=45.0,
    min_node_size=20.0,
    min_node_size_location=5.0,
    pick_match_tolerance=2.0,
    min_interevent_time=5.0,
    n_picks=3, n_p_picks=1, n_s_picks=0, n_p_and_s_picks=0,
    refinement_iterations=1,
    time_slicing=300.0,
    n_threads=4,
)


def ensure_catalog_files():
    """CWD'de PICKS/CATALOG CSV'lerini (yoksa) oluşturur ve eski katalog başlığını migre eder.
    Import zamanında değil, worker main() içinde çağrılır (import yan etkisi olmasın)."""
    os.makedirs(EVENTS_DIR, exist_ok=True)
    if not os.path.exists(PICKS_CSV):
        with open(PICKS_CSV, "w", encoding="utf-8") as f:
            f.write("event_id,station,component,phase,pick_time,confidence,model,pretrained\n")
    if not os.path.exists(CATALOG_CSV):
        with open(CATALOG_CSV, "w", encoding="utf-8") as f:
            f.write(_CATALOG_HEADER + "\n")
        return
    try:
        with open(CATALOG_CSV, "r", encoding="utf-8") as f:
            _lines = f.read().splitlines()
        if _lines and _lines[0].strip() != _CATALOG_HEADER:
            _data = [ln for ln in _lines[1:] if ln.strip()]
            with open(CATALOG_CSV, "w", encoding="utf-8") as f:
                f.write(_CATALOG_HEADER + "\n")
                for ln in _data:
                    f.write(ln + "\n")
    except Exception:
        pass
