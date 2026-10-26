from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import pandas as pd
import os
import io
import contextlib
import tempfile
import subprocess
import shutil
from pathlib import Path
import re
import datetime
from pyocto import OctoAssociator, VelocityModel1D
import threading
import pyproj

# Ortak projeksiyon — Hazal'ın offline tez pipeline'ı ile BİREBİR: UTM Zone 36 / WGS84.
# NOT: UTM metre üretir; PyOcto'ya km verildiği için forward'da /1000, inverse'de *1000 uygulanır.
# (Eski aeqd lat0=39/lon0=35 projeksiyonu kendi içinde tutarlıydı ama offline ile aynı değildi.)
proj = pyproj.Proj(proj='utm', zone=36, ellps='WGS84')


def lonlat_to_km(lon, lat):
    """Enlem/boylam (derece) -> UTM-36 km (offline 02/04_*.py ile aynı)."""
    x, y = proj(lon, lat)
    return x / 1000.0, y / 1000.0


def km_to_lonlat(x_km, y_km):
    """UTM-36 km -> (lon, lat) derece."""
    lon, lat = proj(x_km * 1000.0, y_km * 1000.0, inverse=True)
    return lon, lat

app = FastAPI(title="PyOcto + HypoInverse Cataloging API")
# C# ana sayfası (quakemlab.sakarya.edu.tr) tarayıcıdan /live_events çekebilsin diye CORS açık.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "afad-sismik-veri-1")


def _prefixes_for(bolge: str = "ege"):
    """Bölge -> okunacak GCS katalog ön eki(leri). Birden fazla bölge tek görünümde
    birleştirilebilsin diye LİSTE döner.
      'all' / 'hepsi' / 'tumu'  -> her iki bölge (ana sayfa tek harita bunu kullanır)
      'ege'    (varsayılan)     -> ['live_events']            (eski site geriye uyumlu)
      'marmara' / diğer         -> ['live_events_<bolge>']
    Ege = live_events/ (mevcut); diğer bölge = live_events_<bolge>/ (worker EVENTS_DIR ile aynı)."""
    b = (bolge or "ege").strip().lower()
    if b in ("all", "hepsi", "tumu", "tümü"):
        return ["live_events", "live_events_marmara"]
    if b == "ege":
        return ["live_events"]
    return [f"live_events_{b}"]


@app.get("/live_events")
def live_events(hours: int = 24, bolge: str = "ege"):
    """Son `hours` saatteki canlı tespitleri JSON döndürür (ana sayfa haritası için).
    bolge: 'ege' (varsayılan), 'marmara' veya 'all' (her iki bölge birleştirilir)."""
    import csv as _csv
    from google.cloud import storage
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=max(1, hours))
    out = []
    updated = None
    err = None
    bucket = storage.Client().bucket(GCS_BUCKET_NAME)
    for prefix in _prefixes_for(bolge):
        # Bu ön ekin bölge kodu (olay hangi katalogtan geldi): live_events -> ege, live_events_X -> X
        ev_region = "ege" if prefix == "live_events" else prefix.replace("live_events_", "")
        try:
            blob = bucket.blob(f"{prefix}/LIVE_CATALOG.csv")
            # KRİTİK: reload() ile GÜNCEL generation alınır; ardından indirme o generation'a
            # sabitlenir. Aksi halde (herkese açık bucket + varsayılan 1 saatlik nesne önbelleği)
            # instance'lar Google kenar önbelleğinden ESKİ kopya alıp yeni olayı hiç göstermiyordu.
            blob.reload()
            text = blob.download_as_text()
            if getattr(blob, "updated", None) and (updated is None or blob.updated.isoformat() > updated):
                updated = blob.updated.isoformat()
        except Exception as e:
            err = str(e)[:200]
            continue
        for r in _csv.DictReader(io.StringIO(text)):
            try:
                raw = (r.get("origin_time") or "").strip().replace("Z", "")
                ot = datetime.datetime.fromisoformat(raw)
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if ot < cutoff:
                continue

            def _f(k):
                v = (r.get(k) or "").strip()
                try:
                    return float(v)
                except Exception:
                    return None
            out.append({
                "eventId": r.get("event_id", ""),
                "lat": _f("lat"), "lon": _f("lon"), "depthKm": _f("depth"),
                "originTime": r.get("origin_time", ""), "location": r.get("loc_name", ""),
                "magnitude": _f("mag"), "rms": _f("rms"),
                # Spektral moment magnitüdü (SourceSpec). Yalnız ML >= MW_MIN_ML olan olaylarda
                # hesaplanır, olay kaydedildikten sonra arka planda dolar -> çoğu satırda None.
                # Ana sayfa tablosu varsa "3.5 (ML) - 3.9 (Mw)" biçiminde ikisini birden gösterir.
                "mw": _f("mw"), "mwErr": _f("mw_err"), "mwN": _f("mw_n"), "fc": _f("fc"),
                "bolge": ev_region,   # olayın bölgesi (ana sayfa "Bölge" sütununda yedek etiket için)
            })
    out.sort(key=lambda e: e["originTime"], reverse=True)
    if not out and err:
        return {"events": [], "error": err}
    # catalogUpdated: tazelik teşhisi (bayat okuma olursa hemen görülsün)
    return {"events": out, "catalogUpdated": updated}


# ---- QuakeMLab <-> bağımsız kataloglar karşılaştırması (ana sayfa tablosu) ----
# Tüm kaynaklar taranır; KONUM FARKI EN KÜÇÜK olan referans seçilir (eşitlikte aşağıdaki
# _SRC_PRIORITY sırası belirleyicidir). Seçilen kaynağın kısaltması "Kaynak" sütununda görünür.
# İki servis sorgulanır:
#   1) seismicportal (EMSC-RTS): tek çatı altında EMSC/AFAD/NOA/KOERI/THE(=AUTH) çözümleri
#   2) eida.gein.noa.gr (NOA kendi kataloğu): Yunanistan yerel küçük olaylarında EMSC-RTS'ten
#      çok daha zengin (ölçüm: 2 günde 202 olay vs 75)
# NOT: VolcanoDiscovery ayrı bir kaynak DEĞİL — kendi sitesinde belirttiği gibi verisini
# NOA/AUTH/EMSC'den derliyor; üçü de burada doğrudan sorgulandığı için ek katkısı olmaz.
_EMSC_CMP_CACHE = {}  # hours -> (fetch_time, payload); 5 dk TTL
_SRC_PRIORITY = ["EMSC", "NOA", "AUTH", "AFAD", "KOERI"]
_AUTHOR_TO_SRC = {"EMSC": "EMSC", "NOA": "NOA", "THE": "AUTH", "AUTH": "AUTH",
                  "AFAD": "AFAD", "DDA": "AFAD", "KOERI": "KOERI", "ISK": "KOERI"}


def _fetch_catalog_rows(url, params, source_hint=None):
    """FDSN event 'text' formatını ayrıştırır -> [{id,time,lat,lon,depth,mag,magType,region,source}]."""
    import requests as _rq
    out = []
    try:
        r = _rq.get(url, params=params, timeout=45)
        if r.status_code != 200:
            return out
        for ln in r.text.splitlines():
            if not ln or ln.startswith("#"):
                continue
            p = ln.split("|")
            if len(p) < 13:
                continue
            try:
                t = pd.to_datetime(p[1], utc=True).to_pydatetime()
                src = source_hint or _AUTHOR_TO_SRC.get((p[5] or "").strip().upper())
                if not src:
                    continue  # tanınmayan yazar -> atla (kaynak etiketi veremeyiz)
                out.append({"id": p[0], "time": t, "lat": float(p[2]), "lon": float(p[3]),
                            "depth": float(p[4]) if p[4] else None,
                            "magType": p[9], "mag": float(p[10]) if p[10] else None,
                            "region": p[12], "source": src})
            except Exception:
                continue
    except Exception as e:
        print(f"[CMP] {url} hata: {str(e)[:100]}")
    return out

def _hav_km(la1, lo1, la2, lo2):
    import math
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


@app.get("/catalog_compare")
@app.get("/emsc_compare")   # geriye uyumluluk (eski yayınlanmış site bu adı çağırıyor)
def emsc_compare(hours: int = 24, bolge: str = "ege"):
    """QuakeMLab canlı tespitlerini BAĞIMSIZ kataloglarla eşleştirir.
    Eşleşme ölçütü: |Δt| <= 90 s VE konum farkı <= 150 km (yalnız zaman yeterli değil;
    aynı dakikada başka bölgede deprem olabiliyor).
    Tüm kaynaklar (EMSC, NOA, AUTH, AFAD, KOERI) taranır; KONUM FARKI EN KÜÇÜK olan
    referans döner, kısaltması 'source' alanında verilir. 5 dk önbellek."""
    hours = max(1, min(int(hours), 24 * 30))
    now = datetime.datetime.now(datetime.timezone.utc)
    ckey = (bolge or "ege").strip().lower(), hours
    cached = _EMSC_CMP_CACHE.get(ckey)
    if cached and (now - cached[0]).total_seconds() < 300:
        return cached[1]

    qm_events = live_events(hours=hours, bolge=bolge).get("events", [])
    rows = []
    ext = []
    if qm_events:
        start = (now - datetime.timedelta(hours=hours + 1)).strftime("%Y-%m-%dT%H:%M:%S")
        # Kutu Marmara'yi da kapsar (40.25-41.15 K): eski 40 ust siniri Marmara olaylarini
        # referanssiz birakiyordu. Genisletmek Ege eslesmelerini etkilemez; eslesme olcutu
        # zaten |dt|<=90 s VE <=150 km.
        box = {"minlatitude": 33, "maxlatitude": 42, "minlongitude": 20, "maxlongitude": 32}
        # 1) EMSC-RTS çatısı (EMSC/AFAD/NOA/KOERI/THE yazarları)
        ext += _fetch_catalog_rows("https://www.seismicportal.eu/fdsnws/event/1/query",
                                   {"format": "text", "starttime": start, "orderby": "time", **box})
        # 2) NOA kendi kataloğu (Yunanistan yerel olaylarında çok daha kapsamlı)
        ext += _fetch_catalog_rows("https://eida.gein.noa.gr/fdsnws/event/1/query",
                                   {"format": "text", "starttime": start, **box},
                                   source_hint="NOA")

    for ev in qm_events:
        try:
            qt = pd.to_datetime(ev["originTime"], utc=True).to_pydatetime()
        except Exception:
            continue
        # Aday eşleşmeleri topla; seçim ölçütü: KONUM FARKI EN KÜÇÜK olan referans.
        # (Önceden kaynak önceliği (EMSC>NOA>...) ile seçiliyordu; farklı kurumların bölgesel
        # doğruluğu değiştiği için en yakın çözümü göstermek daha anlamlı. Hangi kaynağın
        # eşleştiği "Kaynak" sütununda açıkça yazdığı için şeffaflık korunur.
        # Eşit/ölçülemeyen mesafede sırasıyla kaynak önceliği ve zaman farkı belirleyici olur.)
        cands = []
        for em in ext:
            dt = abs((em["time"] - qt).total_seconds())
            if dt > 90:
                continue
            dist = None
            if ev.get("lat") is not None and ev.get("lon") is not None:
                dist = _hav_km(ev["lat"], ev["lon"], em["lat"], em["lon"])
                if dist > 150.0:
                    continue
            cands.append((_SRC_PRIORITY.index(em["source"]) if em["source"] in _SRC_PRIORITY else 99,
                          dt, em, dist))
        row = {"quakemlab": ev, "emsc": None, "dtSeconds": None, "distKm": None, "source": None}
        if cands:
            # (mesafe, kaynak önceliği, |Δt|) — mesafe ölçülemiyorsa sona at
            cands.sort(key=lambda c: (c[3] if c[3] is not None else 9e9, c[0], c[1]))
            _, best_dt, best, dist = cands[0]
            row["emsc"] = {"id": best["id"], "time": best["time"].isoformat(),
                           "lat": best["lat"], "lon": best["lon"], "depth": best["depth"],
                           "mag": best["mag"], "magType": best["magType"],
                           "region": best["region"], "source": best["source"]}
            row["source"] = best["source"]
            row["dtSeconds"] = round(best_dt, 1)
            row["distKm"] = round(dist, 1) if dist is not None else None
        rows.append(row)

    payload = {"rows": rows, "hours": hours}
    _EMSC_CMP_CACHE[ckey] = (now, payload)
    return payload


@app.get("/live_stats")
def live_stats(hours: int = 24, bolge: str = "ege"):
    """Ana sayfa sayacı: seçili dönemde olay sayısı, P/S faz sayıları, medyan işlem süresi
    ve o an veri akıtan istasyon sayısı (worker'ın yazdığı STATUS.json'dan)."""
    import csv as _csv
    import json as _json
    import statistics as _stats
    from google.cloud import storage

    hours = max(1, min(int(hours), 24 * 30))
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=hours)
    out = {"hours": hours, "eventCount": 0, "pPhases": 0, "sPhases": 0,
           "medianProcSec": None, "stationsActive": None, "stationsTotal": None,
           "statusUpdatedAt": None}
    try:
        bucket = storage.Client().bucket(GCS_BUCKET_NAME)
        procs, ids = [], []
        sta_active, sta_total = None, None   # birden fazla bölge -> topla

        # Bölge(ler): 'all' ise her iki katalog birleştirilir (ana sayfa tek görünüm).
        for prefix in _prefixes_for(bolge):
            # Katalog: olay sayısı + P/S + işlem süresi (kolonlar 2026-08-03'ten sonra dolu)
            try:
                blob = bucket.blob(f"{prefix}/LIVE_CATALOG.csv")
                blob.reload()
                catalog_text = blob.download_as_text()
            except Exception:
                catalog_text = ""
            for r in _csv.DictReader(io.StringIO(catalog_text)):
                try:
                    ot = pd.to_datetime((r.get("origin_time") or "").strip(), utc=True).to_pydatetime()
                except Exception:
                    continue
                if ot < cutoff:
                    continue
                ids.append(r.get("event_id"))

                def _i(k):
                    try:
                        return int(float((r.get(k) or "").strip()))
                    except Exception:
                        return None
                out["pPhases"] += _i("n_p") or 0
                out["sPhases"] += _i("n_s") or 0
                # Medyan NET işlem süresinden hesaplanır: gecikmeli (sadece-FDSN) istasyon kullanan
                # olaylarda toplam sürenin ~5 dk'sı veri temini beklemesidir, bizim işlem süremiz değil.
                _ps = _i("proc_net_s")
                if _ps is None:
                    _ps = _i("proc_s")
                if _ps is not None:
                    procs.append(_ps)

            # İstasyon sağlığı (worker her heartbeat'te yazar) — bölgeler arası TOPLANIR
            sb = bucket.blob(f"{prefix}/STATUS.json")
            if sb.exists():
                sb.reload()
                js = _json.loads(sb.download_as_text())
                if js.get("stationsActive") is not None:
                    sta_active = (sta_active or 0) + js["stationsActive"]
                if js.get("stationsTotal") is not None:
                    sta_total = (sta_total or 0) + js["stationsTotal"]
                # en yeni STATUS zamanını göster
                if js.get("updatedAt") and (out["statusUpdatedAt"] is None
                                            or js["updatedAt"] > out["statusUpdatedAt"]):
                    out["statusUpdatedAt"] = js["updatedAt"]

        out["eventCount"] = len(ids)
        if procs:
            out["medianProcSec"] = int(round(_stats.median(procs)))
        out["stationsActive"] = sta_active
        out["stationsTotal"] = sta_total
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


@app.on_event("startup")
def startup_event():
    print("Hız Modeli (Velocity Model) oluşturuluyor veya yükleniyor...")
    get_velocity_model()
    print("Hız Modeli başarıyla yüklendi ve sunucu hazır!")

# Global variables for caching
global_velocity_model = None
global_associator = None

class Pick(BaseModel):
    station: str
    phase: str
    time: float
    probability: float

class Station(BaseModel):
    id: str
    x: float
    y: float
    z: float

class AssociateRequest(BaseModel):
    picks: List[Pick]
    stations: List[Station]
    # Canlı mod: seyrek ağ (2-4 istasyon) için gevşetilmiş asosiasyon eşikleri kullanılır
    # (n_picks>=4, n_p>=2; S ve P&S şartı korunur). Tarihsel/toplu istekler (varsayılan
    # False) sıkı eşiklerle aynen devam eder. Eski istemciler alanı göndermez -> False.
    live: bool = False
    # Pick'ler ZATEN ilişkilendirilmişse (canlı worker kendi asosiyatörüyle olayı kurmuştur)
    # PyOcto adımını atla ve doğrudan HypoInverse çalıştır. Yeniden ilişkilendirme, S pick'i
    # olmayan/az pick'li canlı olaylarda reddedilip RMS/ERH/ERZ üretilememesine yol açıyordu.
    skip_association: bool = False
    trial_lat: Optional[float] = None
    trial_lon: Optional[float] = None
    trial_depth: Optional[float] = None
    # BOLGE: hiz modelini secer. "ege" = Helen dalma-batma bolgesi icin DERIN model
    # (0-210 km, projenin PyOcto_deneme/hypo_run/model.crh'si). "marmara" = KABUK modeli
    # (0-40 km, seismic_mission_2/hyp_run/model.crh). Marmara olaylarini derin Ege modeliyle
    # cozmek konumu bozar. Alani gondermeyen eski istemciler "ege" alir -> davranis degismez.
    bolge: str = "ege"

def get_velocity_model():
    global global_velocity_model
    if global_velocity_model is None:
        model_path = "velocity_model"

        # Hazal'ın offline tez pipeline'ı ile BİREBİR: DERİN (Moho + Pn) 1B model.
        # Eski shallow crust-only model (0-40 km, max Vp 7.80, manto yok) bölgesel
        # Pn (manto ~8 km/s) ilk-varışlarını temsil edemiyordu; uzak istasyonlarda
        # seyahat süresi çok yavaş tahmin edilip odak kara-istasyonu azimutuna (kuzeye)
        # çekiliyordu. Offline velocity_model.csv değerlerinin aynısı:
        model_df = pd.DataFrame({
            "depth": [0.0, 20.0, 35.0, 77.5, 120.0, 165.0, 210.0],
            "vp":    [5.80, 6.50, 8.04, 8.04, 8.05, 8.17, 8.30],
            "vs":    [3.31, 3.71, 4.48, 4.48, 4.50, 4.51, 4.52]
        })

        VelocityModel1D.create_model(model_df, delta=1.0, xdist=800.0, zdist=300.0, path=model_path)
        global_velocity_model = VelocityModel1D(model_path, tolerance=2.0)
    return global_velocity_model

association_lock = threading.Lock()

# --- HYPOINVERSE YARDIMCILARI ---
def deg_to_deg_min(value):
    deg = int(abs(value))
    minutes = (abs(value) - deg) * 60
    return deg, minutes

def weight_code(prob):
    try:
        p = float(prob)
    except:
        return "1"
    if p >= 0.80: return "0"
    if p >= 0.60: return "1"
    if p >= 0.40: return "2"
    return "3"

def hyp71_station_line(sta, lat, lon, elev_m):
    sta = str(sta).strip().split('.')[-1][:4].ljust(4)
    lat_deg, lat_min = deg_to_deg_min(lat)
    lon_deg, lon_min = deg_to_deg_min(lon)
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    elev = int(round(float(elev_m)))
    return (
        f"  {sta}"
        f"{lat_deg:02d}{lat_min:05.2f}{ns}"
        f"{lon_deg:03d}{lon_min:05.2f}{ew}"
        f"{elev:4d} "
        f" 0.00    0.00   0.00 0 1.00   1.00"
    )

def phase_line(sta, dt_ref, p_sec=None, s_sec=None, p_prob=None, s_prob=None):
    sta = str(sta).strip().split('.')[-1][:4].ljust(4)
    yy, mm, dd = dt_ref.year % 100, dt_ref.month, dt_ref.day
    hh, mi = dt_ref.hour, dt_ref.minute
    date = f"{yy:02d}{mm:02d}{dd:02d}{hh:02d}{mi:02d}"

    if p_sec is not None:
        prem, pwt, psec = "IP", weight_code(p_prob), f"{p_sec:5.2f}"
    else:
        prem, pwt, psec = "  ", "4", "     "

    if s_sec is not None:
        srem, swt, ssec = "ES", weight_code(s_prob), f"{s_sec:5.2f}"
    else:
        srem, swt, ssec = "  ", "4", "     "

    line = (
        sta + prem + " " + pwt + " " + date + psec +
        " " + " " * 6 + ssec + srem + " " + swt + " "
    )
    return line.ljust(85)

def run_hypoinverse(picks_df, stations_df, trial_lat=None, trial_lon=None, trial_depth=10.0,
                    bolge="ege"):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            
            # 1. STATION FILE
            sta_lines = []
            for _, r in stations_df.iterrows():
                # stations_df: id, x(lon), y(lat), z(elev km)
                elev_m = r["z"] * 1000.0 if "z" in r else 0.0
                sta_lines.append(hyp71_station_line(r["id"], r["y"], r["x"], elev_m))
            
            sta_file = base / "stations.sta"
            sta_file.write_text("\n".join(sta_lines) + "\n", encoding="utf-8")
            # 2. MODEL FILE — Hazal'ın offline DEEP modeli ile aynı (5.80 -> 8.30, 0-210 km).
            # Hiz modeli BOLGEYE gore secilir; ikisi de projenin kendi .crh dosyalari:
            #   ege     -> PyOcto_deneme/hypo_run/model.crh      (derin, Helen yayi, 0-210 km)
            #   marmara -> seismic_mission_2/hyp_run/model.crh   (kabuk, 0-40 km)
            if str(bolge).lower() == "marmara":
                mod_lines = [
                    "MARMARA_CRUST",
                    " 5.20   0.00",
                    " 5.80   5.00",
                    " 6.20  10.00",
                    " 6.80  20.00",
                    " 7.40  30.00",
                    " 7.80  40.00",
                    ""
                ]
            else:
                mod_lines = [
                    "TURKEY_TEST_MODEL",
                    " 5.80   0.00",
                    " 6.50  20.00",
                    " 8.04  35.00",
                    " 8.04  77.50",
                    " 8.05 120.00",
                    " 8.17 165.00",
                    " 8.30 210.00",
                    ""
                ]
            # NOT: Offline pipeline ≤4 istasyonda HypoInverse'i atlamıyor; MIN 3 alt sınırı
            # zaten koruyor ve başarısız olursa associate() PyOcto konumuna düşüyor.
            mod_file = base / "model.crh"
            mod_file.write_text("\n".join(mod_lines), encoding="utf-8")
            
            # 3. PHS FILE
            phs_lines = []
            # Normalize times so minimum is 0 (fixes negative relative times from Streamlit ordering)
            min_t = picks_df["time"].min()
            picks_df = picks_df.copy()
            picks_df["time"] = picks_df["time"] - min_t
            
            base_time = datetime.datetime(2025, 1, 1, 0, 0, 0)
            
            for sta, g in picks_df.groupby("station"):
                g_p = g[g["phase"].str.upper() == "P"]
                g_s = g[g["phase"].str.upper() == "S"]
                
                p_time_f = g_p["time"].min() if len(g_p) else None
                s_time_f = g_s["time"].min() if len(g_s) else None
                p_prob = g_p["probability"].max() if len(g_p) else None
                s_prob = g_s["probability"].max() if len(g_s) else None
                
                p_dt = base_time + datetime.timedelta(seconds=float(p_time_f)) if p_time_f is not None else None
                s_dt = base_time + datetime.timedelta(seconds=float(s_time_f)) if s_time_f is not None else None
                
                dt_ref = p_dt if p_dt else s_dt
                if not dt_ref: continue
                
                p_sec = p_dt.second + p_dt.microsecond / 1e6 if p_dt else None
                s_sec = s_dt.second + s_dt.microsecond / 1e6 if s_dt else None
                
                if p_dt is not None and s_dt is not None:
                    if (p_dt.year, p_dt.month, p_dt.day, p_dt.hour, p_dt.minute) != (
                        s_dt.year, s_dt.month, s_dt.day, s_dt.hour, s_dt.minute
                    ):
                        phs_lines.append(phase_line(sta, p_dt, p_sec=p_sec, p_prob=p_prob))
                        phs_lines.append(phase_line(sta, s_dt, s_sec=s_sec, s_prob=s_prob))
                    else:
                        phs_lines.append(phase_line(sta, dt_ref, p_sec, s_sec, p_prob, s_prob))
                elif p_dt is not None:
                    phs_lines.append(phase_line(sta, p_dt, p_sec=p_sec, p_prob=p_prob))
                else:
                    phs_lines.append(phase_line(sta, s_dt, s_sec=s_sec, s_prob=s_prob))
            
            phs_lines.append("")
            phs_file = base / "phases.phs"
            phs_file.write_text("\n".join(phs_lines) + "\n", encoding="utf-8")
            
            # 4. RUN FILE — Hazal'ın offline pipeline'ının ürettiği MİNİMAL run.hyp temel alındı.
            # Offline'da RMS/DIS/TRO YOK: DIS 2000/3000 yanlış-ilişkilendirilmiş uzak pick'leri
            # tam ağırlıkta tutup kötü PyOcto trial'ında (TRO) kilitlenmeye yol açıyordu.
            # ANCAK başlangıç DERİNLİĞİ ayrı mesele: sabit 10 km, Helen dalma-batma zonundaki
            # 60-150 km'lik olaylarda çözümü yüzeye çekiyordu (LIVE_20260806_133743: NOA 63.7 km
            # iken bizde 0.02 km çıktı). Artık çağıran kaba derinliği verirse ZTR onu kullanır.
            # ÖLÇÜM (2026-08-06, LIVE_20260806_133743, NOA referansı 63.7 km):
            #   ZTR 10  -> derinlik  0.02 km, RMS 0.28, ERZ 13.3, NOA'ya 48.3 km  (yerel minimum)
            #   ZTR 80  -> derinlik 80.23 km, RMS 0.01, ERZ  4.2, NOA'ya 15.9 km  (doğru dal)
            # Yani doğru başlangıç noktası yalnız derinliği değil episantrı da düzeltiyor.
            # Değer tam sayı yazılıyor (ZTR 80 F); HypoInverse'in beklediği biçim bu.
            _ztr = 10.0
            try:
                if trial_depth is not None and 0.0 < float(trial_depth) <= 200.0:
                    _ztr = float(trial_depth)
            except Exception:
                pass
            cmd = f"""200 F 2000 0
H71 2 2 2
CRH 1 '{mod_file.as_posix()}'
POS 1.750
STA '{sta_file.as_posix()}'
PRT '{(base / "hypo.prt").as_posix()}'
SUM '{(base / "hypo.sum").as_posix()}'
ARC '{(base / "hypo.arc").as_posix()}'
COP 1
PHS '{phs_file.as_posix()}'
FIL
MIN 3
ZTR {int(round(_ztr))} F
LOC
STO
"""
            cmd_file = base / "run.hyp"
            cmd_file.write_text(cmd, encoding="utf-8")
            
            # 5. RUN HYPOINVERSE
            try:
                with open(cmd_file, "r") as stdin:
                    res = subprocess.run(["hyp1.40"], stdin=stdin, cwd=str(base), capture_output=True, timeout=15)
            except FileNotFoundError:
                return {"success": False, "error": "hyp1.40 executable not found on the system."}
            except Exception as e:
                return {"success": False, "error": f"HypoInverse execution failed: {e}"}
                
            # 6. PARSE SUM FILE
            sum_file = base / "hypo.sum"
            if not sum_file.exists():
                prt_file = base / "hypo.prt"
                prt_content = ""
                if prt_file.exists():
                    with open(prt_file, "r", encoding="utf-8", errors="ignore") as f:
                        prt_content = f.read()[-2000:]
                out_str = res.stdout.decode('utf-8')[-2000:]
                err_str = res.stderr.decode('utf-8')[-2000:]
                return {"success": False, "error": f"HypoInverse output not found.\nStdout:\n{out_str}\nStderr:\n{err_str}\nPRT:\n{prt_content}"}
                
            with open(sum_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            # Try parsing hypo.sum - handle both 2-digit and 4-digit year formats
            # When using '200 F 2000 0', HypoInverse may write 4-digit years
            for line in content.splitlines():
                line = line.rstrip()
                if not line.strip():
                    continue
                # Try 4-digit year format first (from '200 F 2000 0' command)
                m4 = re.search(
                    r"^(\d{4})(\d{2})(\d{2})\s+"
                    r"(\d{2})(\d{2})\s+([\d.]+)\s+"
                    r"(\d{1,2})\s+([\d.]+)\s+"
                    r"(\d{1,3})([EW])\s*([\d.]+)\s+"
                    r"([-]?[\d.]+)\s+([-]?[\d.]+)\s+"
                    r"(\d+)\s+(\d+)\s+([\d.]+)\s+"
                    r"([\d.]+)\s+([\d.]+)\s+([\d.]+)",
                    line
                )
                # Try 2-digit year format  
                m2 = re.search(
                    r"^(\d{2})(\d{2})(\d{2})\s+"
                    r"(\d{2})(\d{2})\s+([\d.]+)\s+"
                    r"(\d{1,2})\s+([\d.]+)\s+"
                    r"(\d{1,3})([EW])\s*([\d.]+)\s+"
                    r"([-]?[\d.]+)\s+([-]?[\d.]+)\s+"
                    r"(\d+)\s+(\d+)\s+([\d.]+)\s+"
                    r"([\d.]+)\s+([\d.]+)\s+([\d.]+)",
                    line
                ) if not m4 else None
                m = m4 or m2
                if m:
                    lat = float(m.group(7)) + float(m.group(8)) / 60.0
                    lon = float(m.group(9)) + float(m.group(11)) / 60.0
                    if m.group(10).upper() == "W": lon = -lon
                    
                    depth = float(m.group(12))
                    # H71 özet sırası: ... MAG(13) NO(14) GAP(15) DMIN(16) RMS(17) ERH(18) ERZ(19)
                    gap = float(m.group(15))
                    rms = float(m.group(17))
                    erh = float(m.group(18))
                    erz = float(m.group(19))

                    return {
                        "success": True,
                        "lat": lat, "lon": lon, "depth": depth,
                        "rms": rms, "erh": erh, "erz": erz, "gap": gap
                    }
                    
            # hypo.sum is empty or nothing was parsed - include PRT and phase file for debugging
            prt_file = base / "hypo.prt"
            prt_content = ""
            if prt_file.exists():
                with open(prt_file, "r", encoding="utf-8", errors="ignore") as pf:
                    prt_content = pf.read()[-1000:]
            phs_content = phs_file.read_text(encoding="utf-8", errors="ignore")[:500] if phs_file.exists() else "no phs"
            return {"success": False, "error": f"hypo.sum empty (Lines:{len(content.splitlines())}). PRT:{prt_content} PHS:{phs_content}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/associate")
def associate(req: AssociateRequest):
    if len(req.picks) < 3:
        raise HTTPException(status_code=400, detail="Geçerli pick < 3")
        
    pick_df = pd.DataFrame([p.dict() for p in req.picks])
    station_df = pd.DataFrame([s.dict() for s in req.stations])
    
    # Drop any NaNs and ensure types
    pick_df = pick_df.dropna(subset=["station", "phase", "time", "probability"])
    
    # Sadece belli bir güvenin (ör. 0.40) üzerindeki tüm pickleri tut
    # (Hazal'ın çevrimdışı sistemindeki gibi sınırlandırma yapmadan tümünü kullan)
    pick_df = pick_df[pick_df["probability"] >= 0.40].copy()
    
    if len(pick_df) < 3:
        return {"success": False, "error": "Filtreleme sonrası yeterli sayıda güvenilir pick kalmadı."}
        
    station_df = station_df.dropna(subset=["id", "x", "y", "z"])
    
    # 2. Filtreleme: Aynı istasyondaki aynı tip (P veya S) birden çok pick varsa 
    # SADECE en yüksek probability'ye sahip olanı bırak (Hayalet pickleri önler)
    pick_df = pick_df.loc[pick_df.groupby(["station", "phase"])["probability"].idxmax()]
    
    # İndeksleri sıfırla ki PyOcto id'leri doğru eşlesin
    pick_df = pick_df.reset_index(drop=True)
    
    if len(pick_df) < 3:
        raise HTTPException(status_code=400, detail="Geçerli pick < 3 (Filtreleme sonrası)")

    # Zaman normalizasyonu: en erken pick'i referans al (negatif zamanları önler)
    min_t = pick_df["time"].min()
    pick_df["time"] = pick_df["time"] - min_t

    # --- ZATEN İLİŞKİLENDİRİLMİŞ PICK'LER: PyOcto'yu atla, doğrudan HypoInverse ---
    # Canlı worker olayı kendi asosiyatörüyle kurdu; burada yeniden ilişkilendirmek
    # (özellikle S pick'i olmayan olaylarda) çözümü tamamen engelliyordu.
    if req.skip_association and req.trial_lat is not None and req.trial_lon is not None:
        # ÇOKLU BAŞLANGIÇ DERİNLİĞİ (multi-start): HypoInverse yinelemeli iniş yapar ve
        # başlangıç derinliğine (ZTR) göre FARKLI yerel minimuma düşer. LIVE_20260806_133743
        # bunu ölçümle gösterdi:
        #   ZTR 10 -> derinlik  0.02 km, RMS 0.28  (yüzeye yapışmış; bekçi kuralı reddeder)
        #   ZTR 80 -> derinlik 80.23 km, RMS 0.01  (NOA'nın 63.7 km'sine 15.9 km — doğru dal)
        # Tek denemeyle yetinmek Helen dalma-batma bölgesindeki derin olayları kaybettiriyor.
        def _solve(z):
            try:
                r = run_hypoinverse(pick_df, station_df,
                                    trial_lat=float(req.trial_lat), trial_lon=float(req.trial_lon),
                                    trial_depth=float(z), bolge=req.bolge)
            except Exception:
                return None
            if not r.get("success"):
                return None
            if not (1.0 <= float(r.get("depth") or 0.0) <= 200.0):
                return None      # yüzeye/tabana yapışmış: yerel minimum işareti
            return r

        def _err(r):
            """Toplam konum belirsizliği. Yalnız RMS'e bakmak yanıltıyor: 0.45 ile 0.48
            arasında pratik fark yok ama ERZ 20.6 ile 3.0 arasında var."""
            h = r.get("erh")
            v = r.get("erz")
            return ((h if h is not None else 99.0) ** 2 + (v if v is not None else 99.0) ** 2) ** 0.5

        _seed = max(1.0, float(req.trial_depth or 8.0))
        _first = _solve(_seed)
        cands = [_first] if _first else []
        # İlk çözüm zaten sağlamsa merdiveni çalıştırma — canlı gecikmeyi boşuna arttırmayalım.
        if not (_first and (_first.get("rms") or 9.0) <= 0.6 and _err(_first) <= 15.0):
            for z in (5.0, 10.0, 20.0, 40.0, 60.0, 80.0, 100.0, 120.0):
                if abs(z - _seed) < 0.5:
                    continue
                r = _solve(z)
                if r:
                    cands.append(r)
        if not cands:
            return {"success": False, "error": "HypoInverse çözemedi (tüm başlangıç derinlikleri)"}
        # RMS farkı 0.1 sn'nin altındaysa anlamsız; o grubun içinden en küçük belirsizliği seç.
        _rms = lambda c: (c.get("rms") if c.get("rms") is not None else 9.0)
        _lim = min(_rms(c) for c in cands) + 0.1
        best = min([c for c in cands if _rms(c) <= _lim], key=_err)
        return {"success": True, "data": {
            "lat": best["lat"], "lon": best["lon"], "depth": best["depth"],
            "rms": best["rms"], "erh": best["erh"], "erz": best["erz"],
            "gap": best.get("gap"), "source": "HypoInverse", "ztr_tried": len(cands)}}

    vm = get_velocity_model()
    
    try:
        # PyOcto'nun doğru uzaklık/hız hesabı yapabilmesi için istasyon koordinatlarını 
        # Enlem/Boylam'dan (derece) -> km'ye (Cartesian) çevirmemiz ŞARTTIR.
        station_df_pyocto = station_df.copy()
        station_df_pyocto["x"], station_df_pyocto["y"] = lonlat_to_km(station_df["x"].values, station_df["y"].values)
        # PyOcto derinlik konvansiyonu: z pozitif-aşağı km (offline 00_convert_stations.py: z=-elev/1000).
        # Yükseklik büyüklüğünü negatif (yüzey üstü) veriyoruz; payload z işaretinden bağımsız güvenli.
        station_df_pyocto["z"] = -station_df["z"].astype(float).abs()
        
        # Hazal'ın çevrimdışı sistemindeki gibi sadece aktif istasyonların etrafını (-/+ 20km) alıyoruz
        active_stations = pick_df["station"].unique()
        active_station_df = station_df_pyocto[station_df_pyocto["id"].isin(active_stations)]
        if len(active_station_df) > 0:
            min_x = active_station_df["x"].min() - 20.0
            max_x = active_station_df["x"].max() + 20.0
            min_y = active_station_df["y"].min() - 20.0
            max_y = active_station_df["y"].max() + 20.0
        else:
            min_x, max_x, min_y, max_y = -1000.0, 1000.0, -1000.0, 1000.0
            
        # Offline tez (03_run_pyocto.py) ile BİREBİR association parametreleri.
        # Kritik: n_s_picks=1 + n_p_and_s_picks=1 -> her event en az 1 S ve 1 (P&S) istasyonu
        # gerektirir; yalnız-P gevşek ilişkilendirmenin ürettiği hayalet event'leri engeller.
        # Canlı modda (req.live) n_picks/n_p gevşetilir; S ve P&S şartı derinlik kısıtı için korunur.
        _n_picks, _n_p = (4, 2) if req.live else (5, 3)
        associator = OctoAssociator(
            xlim=(min_x, max_x), ylim=(min_y, max_y), zlim=(0.0, 200.0),
            velocity_model=vm, time_before=45.0,
            min_node_size=20.0, min_node_size_location=5.0,
            pick_match_tolerance=2.0, min_interevent_time=5.0,
            n_picks=_n_picks, n_p_picks=_n_p, n_s_picks=1, n_p_and_s_picks=1,
            refinement_iterations=1, time_slicing=300.0, n_threads=2
        )
        
        with association_lock:
            with contextlib.redirect_stdout(io.StringIO()):
                events, assignments = associator.associate(pick_df, station_df_pyocto)
            
        if len(events) > 0:
            # Offline tez mantığı (03_run_pyocto.py:153): en ÇOK pick içeren event'i seç.
            # Web'in eski `events.iloc[0]` (pozisyonel) seçimi, büyütülmüş çok-modelli havuzdan
            # doğan küçük/hayalet event'i kapıp konumu ışınlayabiliyordu.
            if "picks" in events.columns:
                sel_event = events.sort_values("picks", ascending=False).iloc[0]
            else:
                _pc = assignments.groupby("event_idx").size()
                sel_event = events[events["idx"] == int(_pc.idxmax())].iloc[0]
            best_event_idx = sel_event["idx"]

            # Seçilen depreme ait pickleri filtrele
            assigned_picks = assignments[assignments["event_idx"] == best_event_idx]

            # PyOcto assignments tablosunda genellikle 'pick_idx' bulunur (orijinal pick_df satır indeksi)
            if "pick_idx" in assigned_picks.columns:
                assigned_full = pick_df.iloc[assigned_picks["pick_idx"]].copy()
            else:
                # Fallback: PyOcto versiyon farklılığı varsa ortak alanlarla birleştir
                assigned_full = pd.merge(assigned_picks, pick_df, on=["station", "phase"], how="inner")
                assigned_full = assigned_full.drop_duplicates(subset=["station", "phase"])

            # PyOcto konumu (yalnız HypoInverse başarısız olursa fallback için; trial ENJEKTE EDİLMİYOR)
            pyocto_x_ev = sel_event["x"]
            pyocto_y_ev = sel_event["y"]
            pyocto_z_ev = float(sel_event["z"])
            trial_lon_ev, trial_lat_ev = km_to_lonlat(pyocto_x_ev, pyocto_y_ev)

            # HypoInverse için PyOcto'nun atadığı pick'leri kullan (assigned_full),
            # ancak başlangıç noktası olarak PyOcto lokasyonunu ver
            hypo_res = run_hypoinverse(assigned_full, station_df,
                trial_lat=trial_lat_ev, trial_lon=trial_lon_ev, trial_depth=max(1.0, pyocto_z_ev),
                                       bolge=req.bolge)
            
            if hypo_res.get("success"):
                result = {
                    "lat": hypo_res["lat"],
                    "lon": hypo_res["lon"],
                    "depth": hypo_res["depth"],
                    "rms": hypo_res["rms"],
                    "erh": hypo_res["erh"],
                    "erz": hypo_res["erz"],
                    "gap": hypo_res.get("gap"),
                    "source": "HypoInverse"
                }
            else:
                # HypoInverse başarısız olursa seçilen PyOcto event'inin lokasyonuna düş (Fallback)
                fallback_lon, fallback_lat = km_to_lonlat(pyocto_x_ev, pyocto_y_ev)

                result = {
                    "lat": fallback_lat,
                    "lon": fallback_lon,
                    "depth": pyocto_z_ev,
                    "source": "PyOcto",
                    "warning": hypo_res.get("error")
                }
                
            return {"success": True, "data": result}
        else:
            return {"success": False, "error": "Hesaplamada birleşmedi"}
    except Exception as e:
        return {"success": False, "error": f"Sistem hatası: {str(e)}"}

@app.post("/debug_hypo")
def debug_hypo(req: AssociateRequest):
    if len(req.picks) < 3:
        raise HTTPException(status_code=400, detail="Geçerli pick < 3")
        
    pick_df = pd.DataFrame([p.dict() for p in req.picks])
    station_df = pd.DataFrame([s.dict() for s in req.stations])
    pick_df = pick_df.dropna(subset=["station", "phase", "time", "probability"])
    station_df = station_df.dropna(subset=["id", "x", "y", "z"])
    
    associator = get_associator()
    events, assignments = associator.associate(pick_df.copy(), station_df.copy())
    
    if len(events) > 0:
        assigned_picks = assignments[assignments["event_idx"] == events.iloc[0]["idx"]]
        if "pick_idx" in assigned_picks.columns:
            assigned_full = pick_df.iloc[assigned_picks["pick_idx"]].copy()
        else:
            assigned_full = pd.merge(assigned_picks, pick_df, on=["station", "phase"], how="inner")
            assigned_full = assigned_full.drop_duplicates(subset=["station", "phase"])
            
        base = Path("/tmp") / f"hypo_run_{uuid.uuid4().hex}"
        base.mkdir(parents=True, exist_ok=True)
        # Sadece dosyaları oluştur ve çalıştır, sonra her şeyi oku
        # run_hypoinverse kullanmıyoruz, manuel yapıyoruz ki dosyaları okuyabilelim
        hypo_res = run_hypoinverse(assigned_full, station_df)
        
        # Orijinal klasör yolunu bulmamız lazım, run_hypoinverse /tmp altında kendi oluşturuyor
        # Fakat run_hypoinverse klasörü silmiyor. Ancak adını uuid ile yapıyor, biz alamayız.
        # Bu yüzden run_hypoinverse'i modifiye etmek istemiyorsak, return dict içine ekleriz.
        return {"success": True, "hypo_res": hypo_res}
    return {"success": False, "error": "No event"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("pyocto_api:app", host="0.0.0.0", port=8000, reload=False)
