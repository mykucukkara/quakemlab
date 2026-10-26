import streamlit as st
import pandas as pd
import os
import obspy
from obspy import read
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import warnings
import plotly.colors as pc
from google.cloud import storage
import io
from obspy.clients.fdsn import Client

warnings.filterwarnings("ignore")

# ==========================================
# 1. AYARLAR
# ==========================================
st.set_page_config(page_title="Sismik Veri İnceleme", layout="wide", page_icon="🌊")

# --- Dil Ayarları ---
query_params = st.query_params
lang = query_params.get("lang", "tr")
is_en = (lang == "en")
page = query_params.get("page", "phase_picking")
deep_event = query_params.get("event")  # ana sayfa haritası / Telegram'dan derin bağlantı (canlı event'e git)

def T(tr_str, en_str):
    return en_str if is_en else tr_str

# --- GCS Ayarları ---
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "your-bucket")
MSEED_ROOT_DIR = "Sismolojik_Veri"  
MODEL_RESULTS_CSV = f"gs://{GCS_BUCKET_NAME}/TUM_SONUCLAR_BIRLESIK_CLEAN.csv"
MANUAL_PICKS_FILE = f"gs://{GCS_BUCKET_NAME}/manual_picks.xlsx"
EVENT_LOCATION_FILE = f"gs://{GCS_BUCKET_NAME}/27-30D_40-41.5K_01.01.2015_21.05.2025_M3-10.xlsx"
EMSC_CATALOG_FILE = "EMSC_katalog_marmara.rtf"  # yerel (imaja gömülü) EMSC kataloğu

# --- FDSN Ayarları ---
FDSN_URL = "http://eida.koeri.boun.edu.tr" 

@st.cache_resource
def get_gcs_client():
    return storage.Client()

def get_bucket():
    client = get_gcs_client()
    return client.bucket(GCS_BUCKET_NAME)

def norm_mag_type(t, default="M"):
    """AFAD/EMSC magnitüd tipini gösterim için normalize eder (MW->Mw, boşsa default).
    Bizim AI pipeline'ımız her zaman Wood-Anderson ML üretir; AFAD/EMSC ise event bazında
    kendi tipini (ML/Mw/Md...) taşır."""
    t = str(t).strip() if t is not None else ""
    u = t.upper()
    if u.startswith("MW"): return "Mw"
    if u == "ML": return "ML"
    if u in ("MD", "MC"): return "Md"
    if u == "MB": return "mb"
    if u == "MS": return "Ms"
    return t if t else default

def _haversine_km(la1, lo1, la2, lo2):
    import math
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))

def loc_reliability(loc, afad_lat=None, afad_lon=None):
    """AI konum çözümünün güven düzeyini değerlendirir. Döner: dict(level, reasons, depth_ok).
    level ∈ {'high','med','low'}.
    - Geçmiş event'te AFAD gerçeği varsa: AFAD'a uzaklık = doğrudan doğruluk ölçüsü
      (≤15 km 'high', ≤50 km 'med', >50 km 'low'). Bu, 'kendinden emin ama yanlış'
      (düşük RMS'li ama uzak) bölgesel M>5 çözümlerini de yakalar.
    - Canlı event'te (AFAD yok): yalnız HypoInverse iç hata metrikleri (rms/erh/erz) +
      kaynak/derinlik mantık kontrolü. Bu metrikler sistematik hatayı GARANTİ edemez.
    Ayrıca çöp derinlik (PyOcto-fallback / >60 km / büyük erz) depth_ok=False ile işaretlenir."""
    depth = loc.get("depth"); rms = loc.get("rms")
    erh = loc.get("erh"); erz = loc.get("erz"); src = loc.get("source")
    reasons = []
    depth_ok = True
    if src == "PyOcto":
        reasons.append(T("hassas çözüm üretilemedi (kaba konum)", "precise solution failed (coarse location)"))
        depth_ok = False
    # "derinlik > 60 km => güvenilmez" kuralı kaldırıldı (bölgede derin odaklı deprem normaldir).
    # Derinlik güvenilirliği artık yalnız çözüm kalitesinden okunur: kaba çözüm / büyük ERZ.
    if erz is not None and erz > 20:
        reasons.append(T(f"derinlik hatası ±{erz:.0f} km", f"depth error ±{erz:.0f} km")); depth_ok = False
    if rms is not None and rms > 1.3:
        reasons.append(T(f"yüksek RMS ({rms:.1f} s)", f"high RMS ({rms:.1f} s)"))
    if erh is not None and erh > 8:
        reasons.append(T(f"yatay hata ±{erh:.0f} km", f"horizontal error ±{erh:.0f} km"))
    dist = None
    if afad_lat is not None and afad_lon is not None and loc.get("lat") is not None:
        dist = _haversine_km(afad_lat, afad_lon, loc["lat"], loc["lon"])
        if dist > 50:
            reasons.append(T(f"AFAD'dan {dist:.0f} km uzakta", f"{dist:.0f} km from AFAD"))
    if reasons:
        return {"level": "low", "reasons": reasons, "depth_ok": depth_ok, "dist": dist}
    if dist is not None:
        return {"level": "high" if dist <= 15 else "med", "reasons": [], "depth_ok": depth_ok, "dist": dist}
    return {"level": "med", "reasons": [], "depth_ok": depth_ok, "dist": dist}

_REL_BADGE = {"high": "🟢", "med": "🟡", "low": "🔴"}


# NOT: ML->Mw proxy dönüşümü (K&K 2016) hoca kararıyla kaldırıldı — Mw yalnız M>5 olaylar için
# SourceSpec tabanlı spektral hesapla eklenecek (bkz. bellek/rapor).

# ==========================================
# 2. VERİ YÜKLEME
# ==========================================
def _fresh_download_text(blob):
    """Bayat okuma önleyici: GCS nesnesinin GÜNCEL generation'ını alıp indirmeyi ona sabitler.
    Bucket herkese açık olduğu için nesneler varsayılan olarak ~1 saat kenar önbelleğinde
    kalabiliyor; generation'a sabitlenmiş indirme her zaman en son sürümü getirir."""
    blob.reload()
    return blob.download_as_text()


def _live_extra_cols(row):
    """LIVE_CATALOG'un opsiyonel kalite kolonlarını güvenli ayrıştırır (eski satırlarda yok -> None).
    Kolonlar: n_sta, erh, erz, gap, n_p, n_s, proc_s, model, origin_time."""
    def _f(k):
        try:
            return float(row[k]) if k in row and not pd.isna(row[k]) and str(row[k]).strip() != "" else None
        except Exception:
            return None
    def _i(k):
        v = _f(k)
        return int(v) if v is not None else None
    model = None
    if 'model' in row and not pd.isna(row['model']) and str(row['model']).strip():
        model = str(row['model']).strip()
    origin = None
    if 'origin_time' in row and not pd.isna(row['origin_time']):
        try:
            origin = pd.to_datetime(str(row['origin_time']), utc=True).to_pydatetime()
        except Exception:
            origin = None
    return {"n_sta": _i('n_sta'), "erh": _f('erh'), "erz": _f('erz'), "gap": _f('gap'),
            "n_p": _i('n_p'), "n_s": _i('n_s'), "proc_s": _i('proc_s'),
            "delayed": bool(_i('delayed')), "proc_net_s": _i('proc_net_s'),
            "mw": _f('mw'), "mw_err": _f('mw_err'), "mw_n": _i('mw_n'), "fc": _f('fc'),
            "model": model, "origin": origin}


# Canlı katalog ön ekleri: Ege (live_events/) + Marmara (live_events_marmara/). Olay ID formatı
# iki bölgede de aynı (LIVE_...) olduğundan bölge ID'den anlaşılmaz; bu yüzden katalog/pick/mseed
# okumaları HER İKİ ön ekten yapılır ki ana sayfadan gelen Marmara olayı da açılabilsin.
LIVE_PREFIXES = ("live_events", "live_events_marmara")


@st.cache_data(ttl=30)
def load_event_locations():
    # 30 sn TTL: canlı katalog (LIVE_CATALOG) tazelensin. AFAD xlsx küçük olduğu için maliyet düşük.
    info_map = {}
    try:
        df_locations = pd.read_excel(EVENT_LOCATION_FILE)
        has_cols = {'EventID', 'Location', 'Magnitude'}.issubset(df_locations.columns)
        if has_cols:
            for _, row in df_locations.iterrows():
                event_id = str(row['EventID']).strip()
                location = str(row['Location'])
                mag_raw = str(row['Magnitude'])
                try:
                    mag_val = float(str(mag_raw).replace(',', '.'))
                except Exception:
                    mag_val = None
                    
                lat_val, lon_val = None, None
                for lat_col in ['Latitude', 'Enlem', 'Lat']:
                    if lat_col in df_locations.columns and not pd.isna(row[lat_col]):
                        try: lat_val = float(str(row[lat_col]).replace(',', '.'))
                        except: pass
                for lon_col in ['Longitude', 'Boylam', 'Lon']:
                    if lon_col in df_locations.columns and not pd.isna(row[lon_col]):
                        try: lon_val = float(str(row[lon_col]).replace(',', '.'))
                        except: pass
                        
                depth_val = None
                for d_col in ['Depth', 'Derinlik']:
                    if d_col in df_locations.columns and not pd.isna(row[d_col]):
                        try: depth_val = float(str(row[d_col]).replace(',', '.'))
                        except: pass

                # Üst bilgi paneli için ek alanlar: tarih/saat, magnitüd tipi, RMS
                date_val = None
                for dt_col in ['Date', 'Tarih', 'DateTime', 'Origin Time', 'OriginTime', 'Zaman']:
                    if dt_col in df_locations.columns and not pd.isna(row[dt_col]):
                        date_val = str(row[dt_col]).strip(); break
                mag_type = None
                for mt_col in ['Type', 'MagType', 'Tip', 'Mag Type']:
                    if mt_col in df_locations.columns and not pd.isna(row[mt_col]):
                        mag_type = str(row[mt_col]).strip(); break
                rms_val = None
                for r_col in ['Rms', 'RMS']:
                    if r_col in df_locations.columns and not pd.isna(row[r_col]):
                        try:
                            rms_val = float(str(row[r_col]).replace(',', '.')); break
                        except: pass

                info_map[event_id] = {"location": location, "magnitude": mag_val,
                                      "lat": lat_val, "lon": lon_val, "depth": depth_val,
                                      "date": date_val, "mag_type": mag_type, "rms": rms_val}
        else:
            st.warning(T("Location Excel beklenen kolonları içermiyor: EventID, Location, Magnitude", "Location Excel is missing expected columns: EventID, Location, Magnitude"))
    except Exception as e:
        st.warning(T(f"Location Excel verisi GCS'den okunamadı: {e}", f"Could not read Location Excel from GCS: {e}"))
        
    try:
        bucket = get_bucket()
        from io import StringIO
        for _prefix in LIVE_PREFIXES:
            blob_cat = bucket.blob(f"{_prefix}/LIVE_CATALOG.csv")
            if not blob_cat.exists():
                continue
            content = _fresh_download_text(blob_cat)
            df_live = pd.read_csv(StringIO(content))
            for _, row in df_live.iterrows():
                eid = str(row['event_id'])
                lat = float(row['lat'])
                lon = float(row['lon'])
                depth = float(row['depth'])
                loc_name = str(row['loc_name']) if 'loc_name' in row and not pd.isna(row['loc_name']) else ''
                loc_name_str = f" ({loc_name})" if loc_name else ""
                mag = float(row['mag']) if 'mag' in row and not pd.isna(row['mag']) else None
                rms = float(row['rms']) if 'rms' in row and not pd.isna(row['rms']) else None
                extra = _live_extra_cols(row)
                m_str = f" | ML {mag:.1f}" if mag is not None else ""
                r_str = f" | RMS {rms:.2f}" if rms is not None else ""
                loc_str = f"🔴 CANLI | {lat:.2f}°N {lon:.2f}°E{loc_name_str}{m_str} | Derinlik: {depth:.1f}km{r_str}"
                info_map[eid] = {"location": loc_str, "magnitude": mag, "lat": lat, "lon": lon,
                                 "depth": depth, "rms": rms, **extra}
    except Exception as e:
        import os
        if os.path.exists("LIVE_CATALOG.csv"):
            try:
                df_live = pd.read_csv("LIVE_CATALOG.csv")
                for _, row in df_live.iterrows():
                    eid = str(row['event_id'])
                    lat = float(row['lat'])
                    lon = float(row['lon'])
                    depth = float(row['depth'])
                    loc_name = str(row['loc_name']) if 'loc_name' in row and not pd.isna(row['loc_name']) else ''
                    loc_name_str = f" ({loc_name})" if loc_name else ""
                    mag = float(row['mag']) if 'mag' in row and not pd.isna(row['mag']) else None
                    rms = float(row['rms']) if 'rms' in row and not pd.isna(row['rms']) else None
                    extra = _live_extra_cols(row)
                    m_str = f" | ML {mag:.1f}" if mag is not None else ""
                    r_str = f" | RMS {rms:.2f}" if rms is not None else ""
                    loc_str = f"🔴 CANLI | {lat:.2f}°N {lon:.2f}°E{loc_name_str}{m_str} | Derinlik: {depth:.1f}km{r_str}"
                    info_map[eid] = {"location": loc_str, "magnitude": mag, "lat": lat, "lon": lon,
                                     "depth": depth, "rms": rms, **extra}
            except Exception:
                pass
            
    return info_map


@st.cache_data(ttl=300)
def load_emsc_catalog():
    """EMSC RTF kataloğunu ayrıştırır -> [{time(UTC), lat, lon, depth, mag, magtype}].
    Önce GCS'ten okur (imaj-bundling'e bağımlı değil), olmazsa yerel dosyaya düşer."""
    import re, os
    from datetime import datetime
    raw = None
    _src = "yok"
    try:
        blob = get_bucket().blob("EMSC_katalog_marmara.rtf")
        _ex = bool(blob.exists())
        if _ex:
            raw = blob.download_as_bytes().decode("utf-8", errors="ignore")
            _src = "gcs"
        print(f"[EMSC] gcs exists={_ex} bytes={len(raw) if raw else 0}", flush=True)
    except Exception as e:
        print(f"[EMSC] gcs hata: {e}", flush=True)
        raw = None
    if raw is None and os.path.exists(EMSC_CATALOG_FILE):
        try:
            raw = open(EMSC_CATALOG_FILE, "r", encoding="utf-8", errors="ignore").read()
            _src = "local"
        except Exception as e:
            print(f"[EMSC] local hata: {e}", flush=True)
            raw = None
    if not raw:
        print("[EMSC] raw bos -> [] (ne gcs ne local)", flush=True)
        return []
    t = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)      # \'xx hex kaçışları
    t = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", t)          # RTF kontrol kelimeleri
    t = t.replace("{", "").replace("}", "")
    out = []
    for line in t.splitlines():
        line = line.strip().rstrip("\\").strip()
        if "|" not in line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 11:
            continue
        # SADECE Author=EMSC olayları (AFAD/ISK/DDA/KOERI vb. hariç) — bağımsız EMSC çözümü.
        if parts[5].strip().upper() != "EMSC":
            continue
        try:
            # NOT: datetime.fromisoformat Python 3.9'da (container) 2-haneli saniye kesri/Z'yi
            # ayrıştıramıyor -> pandas kullan (sürümden bağımsız, robust).
            dt = pd.to_datetime(parts[1].strip(), utc=True).to_pydatetime()
            out.append({"time": dt, "lat": float(parts[2]), "lon": float(parts[3]),
                        "depth": float(parts[4]), "mag": float(parts[10]),
                        "magtype": parts[9].strip()})
        except Exception:
            continue
    print(f"[EMSC] kaynak={_src} events={len(out)}", flush=True)
    return out


def emsc_match(afad_date_str, tol_sec=15):
    """AFAD event tarihine origin-zamanı en yakın EMSC event'i (±tol_sec içinde), yoksa None."""
    cat = load_emsc_catalog()
    if not cat or not afad_date_str:
        return None
    ts = pd.to_datetime(afad_date_str, dayfirst=True, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    target = ts.to_pydatetime()
    best = min(cat, key=lambda e: abs((e["time"] - target).total_seconds()))
    return best if abs((best["time"] - target).total_seconds()) <= tol_sec else None


def _norm_comp(c):
    c = str(c).strip().upper()
    if len(c) == 1: return 'H' + c
    if len(c) == 3: return 'H' + c[-1]
    return c


def _transform_model_df(df):
    # Model pick DataFrame'ine ortak dönüşümler (hem geçmiş hem canlı için).
    df = df.copy()
    df['pick_time'] = pd.to_datetime(df['pick_time'], errors='coerce').dt.tz_localize(None)
    df['event_id'] = df['event_id'].astype(str).str.split('.').str[0].str.strip()
    # Sadece phasenet ve eqtransformer
    df = df[df['model'].astype(str).str.lower().isin(['phasenet', 'eqtransformer'])].copy()
    df['model_display'] = df.apply(
        lambda row: f"{row['model']}_{row['pretrained']}" if pd.notnull(row.get('pretrained')) and row.get('pretrained') != "" else row['model'],
        axis=1
    )
    if 'component' in df.columns:
        df['component'] = df['component'].apply(_norm_comp)
    return df


@st.cache_data
def _load_historical_model():
    # Ağır statik geçmiş veri (GCS TUM_SONUCLAR ~254MB) — bir kez okunup dönüştürülür, KALICI cache.
    df_model = pd.read_csv(MODEL_RESULTS_CSV, dtype={'event_id': str, 'confidence': float})
    return _transform_model_df(df_model)


@st.cache_data(ttl=30)
def _load_live_model():
    # Canlı pick'ler — 30 sn TTL ile tazelenir (yeni tespitler arayüzde görünsün diye).
    import os
    from io import StringIO
    try:
        bucket = get_bucket()
        parts = []
        for _prefix in LIVE_PREFIXES:
            blob_picks = bucket.blob(f"{_prefix}/LIVE_PICKS.csv")
            if not blob_picks.exists():
                continue
            content = _fresh_download_text(blob_picks)
            dfp = pd.read_csv(StringIO(content), dtype={'event_id': str, 'station': str})
            if not dfp.empty:
                parts.append(dfp)
        if parts:
            df = pd.concat(parts, ignore_index=True)
            return _transform_model_df(df)
    except Exception:
        pass
    try:
        if os.path.exists("LIVE_PICKS.csv"):
            df = pd.read_csv("LIVE_PICKS.csv", dtype={'event_id': str, 'station': str})
            if not df.empty:
                return _transform_model_df(df)
    except Exception:
        pass
    return pd.DataFrame()


@st.cache_data
def _load_manual():
    try:
        df_manual = pd.read_excel(MANUAL_PICKS_FILE)
        df_manual.columns = df_manual.columns.str.strip().str.lower()
        df_manual['event_id'] = df_manual['event_id'].astype(str).str.split('.').str[0].str.strip()
        df_manual['station'] = df_manual['station'].astype(str).str.upper().str.strip()
        if 'component' in df_manual.columns:
            df_manual['component'] = df_manual['component'].astype(str).str.upper().str.strip().apply(_norm_comp)
        df_manual['manuel_pick'] = pd.to_datetime(df_manual['manuel_pick'], errors='coerce').dt.tz_localize(None)
        return df_manual
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_metadata():
    # Geçmiş (kalıcı cache) + canlı (30 sn TTL) birleştirilir; canlı veri artık DONMUYOR.
    data = {}
    try:
        df_hist = _load_historical_model()
    except Exception as e:
        st.error(T(f"Model CSV GCS'den okunamadı: {e}", f"Could not read Model CSV from GCS: {e}"))
        df_hist = pd.DataFrame()

    df_live = _load_live_model()
    if df_live is not None and not df_live.empty:
        data['model'] = pd.concat([df_hist, df_live], ignore_index=True)
    else:
        data['model'] = df_hist

    data['manual'] = _load_manual()
    return data

def find_event_prefix_gcs(event_id, root_dir):
    try:
        bucket = get_bucket()
        iterator = bucket.list_blobs(prefix=f"{root_dir}/", delimiter='/')
        list(iterator)
        prefixes = iterator.prefixes
        
        for prefix in prefixes:
            folder_name = prefix.rstrip('/').split('/')[-1]
            if folder_name.startswith(str(event_id)):
                return prefix
    except Exception as e:
        st.error(T(f"GCS klasör arama hatası: {e}", f"GCS folder search error: {e}"))
    return None

# ==========================================
@st.cache_data
def load_station_coordinates():
    import os, re
    # Kaynak önceliği (_prio): çakışan çıplak istasyon kodlarında BÜYÜK _prio KAZANIR.
    # pyocto_stations.csv = Hazal'ın offline-otoritatif seti -> en yüksek öncelik (3).
    # Böylece FDSN dump'ındaki (station.xml/test_payload) kod çakışmaları offline
    # koordinatını ezmez (ör. ALT: TU 41.09/28.74 vs KO 39.05/30.11 -> geometri bozulması).
    stations = []
    try:
        with open("tr_stations.md", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("|") and not line.startswith("|---"):
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 7:
                        sta = re.sub(r'[^A-Z0-9]', '', parts[3])
                        try:
                            lat = float(parts[5])
                            lon = float(parts[6])
                            stations.append({"id": sta, "x": lon, "y": lat, "z": 0.0, "_prio": 0})
                        except: pass
    except: pass

    try:
        import xml.etree.ElementTree as ET
        if os.path.exists("station.xml"):
            tree = ET.parse("station.xml")
            root = tree.getroot()
            ns = {'ns': 'http://www.fdsn.org/xml/station/1'}
            for station in root.findall('.//ns:Station', ns):
                sta = station.get('code')
                lat_elem = station.find('ns:Latitude', ns)
                lon_elem = station.find('ns:Longitude', ns)
                if sta and lat_elem is not None and lon_elem is not None:
                    try:
                        lat = float(lat_elem.text)
                        lon = float(lon_elem.text)
                        stations.append({"id": sta, "x": lon, "y": lat, "z": 0.0, "_prio": 1})
                    except: pass
    except: pass

    # Tüm eksik istasyonları test_payload.json içerisindeki kapsamlı listeden yükle
    try:
        import json
        if os.path.exists("test_payload.json"):
            with open("test_payload.json", "r", encoding="utf-8") as f:
                payload_data = json.load(f)
                for srec in payload_data.get("stations", []):
                    stations.append({"id": srec["id"], "x": srec["x"], "y": srec["y"],
                                     "z": srec.get("z", 0.0), "_prio": 2})
    except: pass

    # En öncelikli: AI modellerinin eğitildiği Kandilli/PyOcto istasyon koordinatları (offline ile aynı).
    # Çıplak kodu KISALTMADAN (eski [:4] 5-harfli kodlarda eşleşmeyi bozuyordu) tam haliyle anahtarla.
    # z = elevation_m/1000 (km, pozitif-yukarı); API tarafı HypoInverse için *1000, PyOcto için -abs alır.
    try:
        if os.path.exists("pyocto_stations.csv"):
            import pandas as pd
            df_pyocto = pd.read_csv("pyocto_stations.csv")
            for _, row in df_pyocto.iterrows():
                sta = str(row['id']).strip().split('.')[-1].strip()
                try:
                    lon = float(row['longitude'])
                    lat = float(row['latitude'])
                    elev_km = float(row.get('elevation_m', 0.0)) / 1000.0
                    stations.append({"id": sta, "x": lon, "y": lat, "z": elev_km, "_prio": 3})
                except: pass
    except: pass

    # EN YÜKSEK ÖNCELİK: canlı ağın 10 istasyonu (live_worker.STATIONS ile senkron).
    # Gömülü tutulur ki CSV imaja girmese/eskise bile canlı olay haritası istasyonları
    # HER ZAMAN çizebilsin (koordinatı bulunmayan istasyon haritada hiç görünmüyordu).
    for _sid, (_la, _lo) in {
        # Ege ağı
        "KTHA": (36.2566, 23.0621), "APE": (37.0689, 25.5306), "KARP": (35.5471, 27.1610),
        "ARG": (36.2135, 28.1212), "IDI": (35.2880, 24.8900),
        "AMGA": (36.8316, 25.8938), "ASTA": (36.5455, 26.3529), "NISR": (36.6106, 27.1310),
        "ZKR": (35.1147, 26.2170),
        # Marmara ağı (TU / AFAD) — live_worker STATION_COORDS_MARMARA ile senkron
        "BAND": (40.3553, 28.0992), "IMRA": (40.563915, 28.528517), "LAPS": (40.252167, 26.838573),
        "MADA": (40.589211, 27.55401), "ELBA": (41.1469, 28.4307), "MURA": (41.150955, 27.485836),
        "SARY": (41.134497, 29.027277), "TKOY": (40.67519, 27.182943),
    }.items():
        stations.append({"id": _sid, "x": _lo, "y": _la, "z": 0.0, "_prio": 4})

    df_st = pd.DataFrame(stations)
    if not df_st.empty:
        # Önceliğe göre stabil sırala, sonra id başına SON (en yüksek _prio'lu) kaydı tut.
        df_st = df_st.sort_values("_prio", kind="stable").drop_duplicates(subset=["id"], keep="last")
        df_st = df_st.drop(columns=["_prio"])
    return df_st

@st.cache_resource
def get_pyocto_associator():
    try:
        import os
        from pyocto import OctoAssociator, VelocityModel1D
        model_path = "/tmp/linux_velocity_model"
        if not os.path.exists(model_path):
            import pandas as pd
            model_df = pd.DataFrame({
                "depth": [0.0, 20.0, 35.0, 77.5, 120.0, 165.0, 210.0],
                "vp": [5.80, 6.50, 8.04, 8.04, 8.05, 8.17, 8.30],
                "vs": [3.31, 3.71, 4.48, 4.48, 4.50, 4.51, 4.52]
            })
            VelocityModel1D.create_model(model_df, delta=1.0, xdist=800.0, zdist=300.0, path=model_path)
        velocity_model = VelocityModel1D(model_path, tolerance=2.0)
        associator = OctoAssociator(
            xlim=(20.0, 45.0),
            ylim=(34.0, 43.0),
            zlim=(0.0, 200.0),
            velocity_model=velocity_model,
            time_before=45.0,
            n_picks=3,
            n_p_picks=1,
            n_s_picks=0,
            n_p_and_s_picks=0,
            n_threads=1,
        )
        return associator
    except Exception as e:
        import traceback
        st.error(f"PYOCTO INIT ERROR: {traceback.format_exc()}")
        return None

# 3. ARAYÜZ
# ==========================================
st.sidebar.header(T("🔍 Ayarlar & Filtreler", "🔍 Settings & Filters"))

ds_gcs_tr = "GCS (Geçmiş Veri)"
ds_fdsn_tr = "Canlı Veri (Live Stream)"
ds_gcs_en = "GCS (Historical Data)"
ds_fdsn_en = "Live Stream"

data_source = st.sidebar.radio(
    T("Veri Kaynağı", "Data Source"),
    [T(ds_gcs_tr, ds_gcs_en), T(ds_fdsn_tr, ds_fdsn_en)],
    index=(1 if (deep_event and str(deep_event).startswith("LIVE_")) else 0)  # canlı event deep-link'inde Canlı kaynağa geç
)

data_store = load_metadata()
location_map = load_event_locations()

if data_store and 'model' in data_store and not data_store['model'].empty:
    df_model = data_store['model']
    df_manual = data_store.get('manual', pd.DataFrame())

    unique_events_all = sorted(df_model['event_id'].unique(), reverse=True)

    is_live_selected = data_source in [ds_fdsn_tr, ds_fdsn_en]
    if is_live_selected:
        # Canlı event listesi = pick DataFrame'i (df_model) ∪ LIVE_CATALOG (location_map).
        # Worker restart'ında LIVE_PICKS.csv sıfırlanıp yalnız son tespitin pick'leri kaldığından,
        # pick'i düşmüş geçmiş canlı tespitler sadece katalogda kalıyordu → listede tek event görünüyordu.
        # Bu birleşim ile TÜM geçmiş canlı tespitler listede görünür (katalog konumu zaten LIVE_CATALOG'dan gelir).
        live_ids = {str(ev) for ev in unique_events_all if str(ev).startswith("LIVE_")}
        live_ids |= {str(eid) for eid in location_map.keys() if str(eid).startswith("LIVE_")}
        unique_events = sorted(live_ids, reverse=True)  # LIVE_YYYYMMDD_HHMMSS → en yeni üstte
    else:
        unique_events = [ev for ev in unique_events_all if not str(ev).startswith("LIVE_")]
    event_display_options = []
    event_to_id_map = {}
    for event_id in unique_events:
        info = location_map.get(event_id, {}) or {}
        location = info.get("location", T("🔴 CANLI TESPİT (Lokasyon Hesaplanıyor...)", "🔴 LIVE DETECTION (Calculating Location...)") if event_id.startswith("LIVE_") else T("Lokasyon bilinmiyor", "Unknown Location"))
        
        if event_id.startswith("LIVE_"):
            mag_txt = "AUTO"
        else:
            mag_val = info.get("magnitude")
            mag_txt = f"{norm_mag_type(info.get('mag_type'))}{mag_val:.1f}" if mag_val is not None else "M?"
        display_text = f"{event_id} | {mag_txt} | {location}"
        event_display_options.append(display_text)
        event_to_id_map[display_text] = event_id
    
    if not event_display_options:
        st.info(T("Bu veri kaynağı için gösterilecek deprem bulunamadı.", "No events found for this data source."))
        st.stop()

    _ev_index = 0  # deep-link (?event=...) varsa o event'i ön-seç
    if deep_event:
        for _i, _disp in enumerate(event_display_options):
            if event_to_id_map.get(_disp) == deep_event:
                _ev_index = _i
                break
    selected_event_display = st.sidebar.selectbox(T("1. Deprem (Event ID):", "1. Event (ID):"),
                                                  event_display_options, index=_ev_index)
    selected_event = event_to_id_map[selected_event_display]

    if page == "phase_picking":
        stations_in_event = sorted(df_model[df_model['event_id'] == selected_event]['station'].unique())
        selected_station = st.sidebar.selectbox(T("2. İstasyon:", "2. Station:"), stations_in_event)
    
        comps_model = df_model[(df_model['event_id'] == selected_event) & (df_model['station'] == selected_station)]['component'].unique()
        comps_manual = []
        if not df_manual.empty:
            comps_manual = df_manual[(df_manual['event_id'] == selected_event) & (df_manual['station'] == selected_station)]['component'].unique()
        # Bileşen listesi pick tablosuna bağlı DEĞİL: mseed her zaman 3 bileşeni içerir ama
        # pick'ler tek kanala etiketlenebiliyor (ör. canlı worker hepsini HHZ yazıyordu) —
        # bu yüzden Z/N/E daima sunulur; pick tablosundaki farklı etiketler sona eklenir.
        _std_axes = ["HZ", "HN", "HE"]
        _extra = sorted((set(comps_model) | set(comps_manual)) - set(_std_axes))
        all_comps = _std_axes + _extra
        
        selected_comps = st.sidebar.multiselect(T("3. Bileşen:", "3. Component:"), all_comps, default=all_comps)
        phases = ['P', 'S']
        selected_phases = st.sidebar.multiselect(T("4. Faz:", "4. Phase:"), phases, default=phases)
    
        station_models = df_model[
            (df_model['event_id'] == selected_event) & 
            (df_model['station'] == selected_station)
        ]
    
        if not station_models.empty:
            top_3_models = station_models.groupby('model_display')['confidence'].mean().nlargest(3).index.tolist()
            available_models = sorted(top_3_models)
        else:
            available_models = []
        
        selected_models = st.sidebar.multiselect(T("5. Modeller (En İyi 3):", "5. Models (Top 3):"), available_models, default=available_models)
    else:
        selected_station = ""
        selected_comps = []
        selected_phases = []
        selected_models = []
else:
    st.info(T("Veriler şu an GCS'den çekilemiyor.", "Data cannot be fetched from GCS currently."))
    st.stop()

# ==========================================
# 4. GÖRSELLEŞTİRME
# ==========================================
def fetch_from_fdsn(station, starttime, endtime, network="KO"):
    try:
        client = Client(FDSN_URL)
        st_obspy = client.get_waveforms(network, station, "*", "*", starttime, endtime)
        return st_obspy
    except Exception as e:
        st.error(T(f"FDSN API'den veri çekerken hata: {e}", f"Error fetching from FDSN API: {e}"))
        return None

def plot_waveform_mseed(event_id, station, components, model_filter, phase_filter, source):
    
    st_obspy = None
    is_gcs = source == T(ds_gcs_tr, ds_gcs_en)
    
    if event_id.startswith("LIVE_"):
        buffer_file = os.path.join("live_events", f"{event_id}_{station}.mseed")

        # Eğer lokalde yoksa GCS'den indirmeyi dene — olay hangi bölgeye aitse orada bulunur,
        # ID'den bölge anlaşılmadığı için HER İKİ ön ek denenir (Ege + Marmara).
        if not os.path.exists(buffer_file):
            try:
                bucket = get_bucket()
                os.makedirs("live_events", exist_ok=True)
                for _prefix in LIVE_PREFIXES:
                    blob = bucket.blob(f"{_prefix}/mseed/{event_id}_{station}.mseed")
                    if blob.exists():
                        blob.download_to_filename(buffer_file)
                        break
            except Exception:
                pass

        if os.path.exists(buffer_file):
            try:
                st_obspy = obspy.read(buffer_file)
            except Exception as e:
                st.error(f"Live event okuma hatası: {e}")
                return None
        else:
            st.warning(T("Canlı tespit sinyali GCS'de veya lokalde bulunamadı.", "Live signal not found on GCS or locally."))
            return None
    elif is_gcs:
        event_prefix = find_event_prefix_gcs(event_id, MSEED_ROOT_DIR)
        if not event_prefix:
            st.error(T(f"❌ '{event_id}' ID'li deprem için klasör bulunamadı (GCS).", f"❌ Folder not found for event '{event_id}' (GCS)."))
            return None

        try:
            bucket = get_bucket()
            st_obspy = obspy.Stream()
            files_found = False
            
            blobs = bucket.list_blobs(prefix=event_prefix)
            for blob in blobs:
                if blob.name.lower().endswith(('.mseed', '.miniseed', '.sac')):
                    try:
                        data_bytes = blob.download_as_bytes()
                        st_obspy += obspy.read(io.BytesIO(data_bytes))
                        files_found = True
                    except Exception:
                        continue
            
            if not files_found:
                st.warning(T(f"⚠️ Klasörde okunabilir veri bulunamadı: {event_prefix}", f"⚠️ No readable data found in: {event_prefix}"))
                return None
                
        except Exception as e:
            st.error(T(f"GCS Okuma hatası: {e}", f"GCS Read error: {e}"))
            return None
            
    else:
        # Eski FDSN mantığı (kullanılmıyor, is_gcs dışındaki durumlar için)
        relevant_preds = df_model[(df_model['event_id'] == event_id) & (df_model['station'] == station)]
        if not relevant_preds.empty:
            ref_time = relevant_preds['pick_time'].min()
            t_start = obspy.UTCDateTime(ref_time) - 120 
            t_end = obspy.UTCDateTime(ref_time) + 120   
            st_obspy = fetch_from_fdsn(station, t_start, t_end)
        else:
            st.error(T("API'den hangi zaman aralığının çekileceğini belirlemek için pick zamanı bulunamadı.", "Could not find pick time to determine FDSN time range."))
            return None
            
        if not st_obspy:
            return None

    # İşleme
    try:
        st_obspy.merge(fill_value=0)
        st_obspy.detrend("demean")
        st_obspy.filter("bandpass", freqmin=1.0, freqmax=45.0)
        
        st_station = st_obspy.select(station=station)
        if len(st_station) == 0:
            st.warning(T(f"⚠️ İlgili kaynakta '{station}' istasyonu verisi bulunamadı.", f"⚠️ No data found for station '{station}' in the selected source."))
            return None
    except Exception as e:
        st.error(T(f"Sinyal işleme hatası: {e}", f"Signal processing error: {e}"))
        return None

    fig = make_subplots(rows=len(components), cols=1, shared_xaxes=True, 
                        subplot_titles=[f"{station} - {c}" for c in components],
                        vertical_spacing=0.08)

    palette = pc.qualitative.Dark24 + pc.qualitative.Light24
    model_color_map = {m: palette[i % len(palette)] for i, m in enumerate(sorted(model_filter))}
    manual_color = 'black'
    
    added_legends = set()

    for i, comp_target in enumerate(components, start=1):
        
        trace = None
        target_orientation = comp_target[-1].upper() 
        candidates = []
        for tr in st_station:
            if len(tr.stats.channel) > 0:
                if tr.stats.channel[-1].upper() == target_orientation:
                    candidates.append(tr)
        
        if len(candidates) > 0:
            candidates.sort(key=lambda x: (x.stats.channel[:2] != 'HH', x.stats.channel[:2] != 'BH'))
            trace = candidates[0]
        
        if trace:
            times_dt = [t.datetime for t in trace.times("utcdatetime")]
            fig.add_trace(go.Scatter(
                x=times_dt, y=trace.data,
                mode='lines', name=f"{T('Sinyal', 'Signal')} ({trace.stats.channel})",
                line=dict(color='lightgray', width=1),
                showlegend=False, hoverinfo='skip'
            ), row=i, col=1)

            # MANUEL PICKLER
            if not df_manual.empty:
                manual_picks = df_manual[
                    (df_manual['event_id'] == event_id) &
                    (df_manual['station'] == station) &
                    (df_manual['phase'].isin(phase_filter))
                ]
                for _, row in manual_picks.iterrows():
                    if pd.isnull(row['manuel_pick']): continue
                    is_same_comp = (row['component'] == comp_target)
                    line_width = 3 if is_same_comp else 1
                    line_dash = "solid" if is_same_comp else "dot"
                    opacity = 1.0 if is_same_comp else 0.5
                    fig.add_vline(x=row['manuel_pick'], line_width=line_width, line_dash=line_dash, 
                                  line_color=manual_color, opacity=opacity, row=i, col=1)
                                  
                    if is_same_comp and "Manuel Pick" not in added_legends:
                        fig.add_trace(go.Scatter(x=[row['manuel_pick']], y=[0], mode='lines', line=dict(color=manual_color, dash='solid', width=3), name='Manuel Pick', showlegend=True), row=i, col=1)
                        added_legends.add("Manuel Pick")

            # MODEL TAHMİNLERİ
            if not df_model.empty:
                relevant_preds = df_model[
                    (df_model['event_id'] == event_id) &
                    (df_model['station'] == station) &
                    (df_model['model_display'].isin(model_filter)) &
                    (df_model['phase'].isin(phase_filter))
                ]
                for _, row in relevant_preds.iterrows():
                    if pd.isnull(row['pick_time']): continue
                    is_same_comp = (row['component'] == comp_target)
                    phase = row['phase']
                    model_disp = row['model_display']
                    color = model_color_map.get(model_disp, 'blue')
                    line_style = "dash" if phase == 'P' else "dot"
                    opacity = 0.9 if is_same_comp else 0.3
                    width = 2 if is_same_comp else 1
                    fig.add_vline(x=row['pick_time'], line_width=width, line_dash=line_style, line_color=color, opacity=opacity, row=i, col=1)
                    
                    if is_same_comp:
                        legend_key = f"{model_disp} ({phase})"
                        if legend_key not in added_legends:
                            fig.add_trace(go.Scatter(
                                x=[row['pick_time']], y=[0], mode='lines',
                                line=dict(color=color, dash=line_style, width=2),
                                name=legend_key, legendgroup=model_disp, showlegend=True
                            ), row=i, col=1)
                            added_legends.add(legend_key)
                            
                        fig.add_trace(go.Scatter(
                            x=[row['pick_time']], y=[0], mode='markers',
                            marker=dict(size=8, symbol="triangle-down", color=color),
                            name=model_disp, legendgroup=model_disp, showlegend=False,
                            hovertemplate=f"<b>{model_disp}</b><br>{T('Faz', 'Phase')}: {phase}<br>{T('Zaman', 'Time')}: {row['pick_time']}<extra></extra>"
                        ), row=i, col=1)
        else:
            fig.add_annotation(x=0.5, y=0.5, text=f"{T('Veri Yok', 'No Data')} ({comp_target})", showarrow=False, row=i, col=1)

    fig.update_layout(
        title=f"{T('Deprem', 'Event')}: {event_id} | {T('İstasyon', 'Station')}: {station} | {T('Kaynak', 'Source')}: {source}",
        height=350 * len(components) + 100,
        template="plotly_white",
        xaxis_title=T("Zaman (UTC)", "Time (UTC)"),
        legend=dict(orientation="h", y=-0.1, x=0.5, xanchor="center", yanchor="top"),
        hovermode="x unified",
        margin=dict(t=80, b=100)
    )
    return fig

# ==========================================
# 5. EKRAN
# ==========================================
# ---------- Magnitüd: Wood-Anderson yerel magnitüdü (ML) ----------
# 661816 (AFAD MW 4.0) üzerinde doğrulandı -> medyan ML=3.93. ML ≈ Mw (M<~5 aralığında).
# Yöntem: cihaz yanıtı (StationXML/GCS) kaldır -> Wood-Anderson simüle et -> yatay tepe genliği (mm)
#         -> ML = log10(A) + Hutton-Boore(1987) mesafe düzeltmesi (hiposantral).
PAZ_WA = {"poles": [-6.283185 - 4.712389j, -6.283185 + 4.712389j],
          "zeros": [0 + 0j], "gain": 1.0, "sensitivity": 2080.0}
WA_PRE_FILT = [0.05, 0.1, 45.0, 50.0]
_MAG_NETS = ["KO", "TU", "TL", "GE", "M2", "PZ", "TB", "GZ", "KJ", "GF", "DD", "TK"]


def _haversine_km(la1, lo1, la2, lo2):
    import math
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


@st.cache_data(ttl=300)
def load_precomputed_results():
    """precompute_ai_results.py'nin GCS'e yazdığı HAZIR AI sonuçları (AI_RESULTS.csv).
    Olay sayfası artık senkron /associate + WA-ML hesabı YAPMAZ; yalnız bu tabloyu okur.
    Yeni event eklenince webApp klasöründen `python precompute_ai_results.py` koşulmalı."""
    from io import StringIO
    try:
        blob = get_bucket().blob("AI_RESULTS.csv")
        if blob.exists():
            return pd.read_csv(StringIO(_fresh_download_text(blob)), dtype={'event_id': str})
    except Exception:
        pass
    return pd.DataFrame()


@st.cache_data(show_spinner=False)
def _station_xml_bytes(net_sta):
    # StationXML (cihaz yanıtı) — istasyon bazında GCS'ten indirilir, sunucu ömrü boyunca cache.
    try:
        blob = get_bucket().blob(f"stationxml/{net_sta}.xml")
        if blob.exists():
            return blob.download_as_bytes()
    except Exception:
        pass
    return None


@st.cache_data(show_spinner=False)
def measure_wa_amplitudes(event_id):
    """Event dalga formundan her istasyonun Wood-Anderson tepe genliğini (mm) + koordinatını ölçer.
    Ağır (mseed indir + cihaz yanıtı kaldır); event bazında cache'lenir.
    {station: {'amp': mm, 'lat':, 'lon':}} döner."""
    import io as _io
    import numpy as _np
    from obspy import read as _oread, read_inventory as _rinv, Stream as _St
    prefix = find_event_prefix_gcs(event_id, MSEED_ROOT_DIR)
    if not prefix:
        return {}
    bucket = get_bucket()
    st_all = _St()
    for blob in bucket.list_blobs(prefix=prefix):
        if blob.name.lower().endswith(('.mseed', '.miniseed', '.sac')):
            try:
                st_all += _oread(_io.BytesIO(blob.download_as_bytes()))
            except Exception:
                continue
    if len(st_all) == 0:
        return {}
    out = {}
    for sta in sorted(set(tr.stats.station for tr in st_all)):
        try:
            sel0 = st_all.select(station=sta)
            net0 = sel0[0].stats.network if len(sel0) else ""
            inv = None
            for net in ([net0] + _MAG_NETS):
                if not net:
                    continue
                b = _station_xml_bytes(f"{net}.{sta}")
                if b:
                    inv = _rinv(_io.BytesIO(b)); break
            if inv is None:
                continue
            horiz = _St([tr for tr in sel0 if tr.stats.channel[-1].upper() in ("E", "N", "1", "2")]).copy()
            if len(horiz) == 0:
                continue
            horiz.merge(fill_value=0); horiz.detrend("demean"); horiz.taper(0.05)
            resp = inv.get_response(horiz[0].id, horiz[0].stats.starttime)
            paz = resp.get_paz()
            paz_sta = {"poles": list(paz.poles), "zeros": list(paz.zeros),
                       "gain": paz.normalization_factor,
                       "sensitivity": resp.instrument_sensitivity.value}
            horiz.simulate(paz_remove=paz_sta, paz_simulate=PAZ_WA, water_level=10, pre_filt=WA_PRE_FILT)
            amp_mm = max(float(_np.max(_np.abs(tr.data))) for tr in horiz) * 1000.0
            if amp_mm <= 0:
                continue
            c = inv.get_coordinates(horiz[0].id, horiz[0].stats.starttime)
            out[str(sta).upper()] = {"amp": amp_mm, "lat": float(c["latitude"]), "lon": float(c["longitude"])}
        except Exception:
            continue
    return out


def estimate_ml(stations, ev_lat, ev_lon, ev_depth, amp_map):
    """Bir konfigürasyonun istasyonlarından medyan Wood-Anderson ML (≈ Mw, M<~5)."""
    import math, statistics
    if not amp_map or ev_lat is None or ev_lon is None:
        return None
    dep = ev_depth if ev_depth else 8.0
    mls = []
    for sta in stations:
        info = amp_map.get(str(sta).upper())
        if not info or info["amp"] <= 0:
            continue
        d = _haversine_km(ev_lat, ev_lon, info["lat"], info["lon"])
        r = math.sqrt(d * d + dep * dep)
        if r <= 0:
            continue
        corr = 1.110 * math.log10(r / 100.0) + 0.00189 * (r - 100.0) + 3.0
        mls.append(math.log10(info["amp"]) + corr)
    if len(mls) < 1:
        return None
    return round(statistics.median(mls), 2)


def render_event_info(info):
    """Geçmiş deprem için üst bilgi paneli (AFAD): büyüklük, tarih/saat, lokasyon, derinlik, RMS."""
    if not info:
        return
    mag = info.get("magnitude")
    mtype = norm_mag_type(info.get("mag_type"), default="")
    mag_str = f"{mtype} {mag:.1f}".strip() if mag is not None else "—"
    loc = info.get("location") or ""
    date = info.get("date") or ""
    lat = info.get("lat"); lon = info.get("lon")
    depth = info.get("depth"); rms = info.get("rms")
    try:
        box = st.container(border=True)
    except TypeError:
        box = st.container()  # eski Streamlit sürümleri için geri düşüş
    with box:
        header = f"**📍 {loc}**" if loc else ""
        if date:
            header += f" &nbsp;·&nbsp; 🕒 {date} (UTC)"
        if header:
            st.markdown(header, unsafe_allow_html=True)
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric(T("Büyüklük (AFAD)", "Magnitude (AFAD)"), mag_str)
        c2.metric(T("Enlem", "Latitude"), f"{lat:.4f}" if lat is not None else "—")
        c3.metric(T("Boylam", "Longitude"), f"{lon:.4f}" if lon is not None else "—")
        c4.metric(T("Derinlik", "Depth"), f"{depth:.1f} km" if depth is not None else "—")
        c5.metric("RMS", f"{rms:.2f}" if rms is not None else "—")


st.title(f"📊 {T('Deprem Analizi', 'Earthquake Analysis')}: {selected_event}")
# Geçmiş depremler için üst bilgi paneli (Event ID'nin altında, lokasyon karşılaştırmasının üstünde).
if not is_live_selected:
    render_event_info(location_map.get(selected_event, {}))
# İstasyon/Bileşenler bilgisi yalnızca Faz Belirleme sayfasında anlamlı; Kataloglama'da gizle.
if page == "phase_picking":
    c1, c2 = st.columns(2)
    c1.info(f"**{T('İstasyon', 'Station')}:** {selected_station}")
    c2.info(f"**{T('Bileşenler', 'Components')}:** {', '.join(selected_comps)}")


if page == "phase_picking":
    if st.button(T("📈 Grafiği Çiz / Güncelle", "📈 Plot / Update Chart"), type="primary"):
        with st.spinner(T('Veriler Buluttan/API\'den çekiliyor ve çiziliyor...', 'Fetching data from Cloud/API and plotting...')):
            fig = plot_waveform_mseed(selected_event, selected_station, selected_comps, selected_models, selected_phases, data_source)
            if fig:
                st.plotly_chart(fig, use_container_width=True)


elif page == "catalog":
    st.subheader(T("📍 Lokasyon Karşılaştırması", "📍 Location Comparison"))

    if not is_live_selected:
        afad_info = location_map.get(selected_event, {})
        afad_lat = afad_info.get("lat")
        afad_lon = afad_info.get("lon")
        afad_depth = afad_info.get("depth")
        # EMSC kataloğu: AFAD event'ine origin-zamanıyla eşleşen kayıt (2. referans katalog)
        emsc = emsc_match(afad_info.get("date"))
        emsc_lat = emsc["lat"] if emsc else None
        emsc_lon = emsc["lon"] if emsc else None
        emsc_depth = emsc["depth"] if emsc else None
        emsc_mag = emsc["mag"] if emsc else None
        emsc_mtype = norm_mag_type(emsc.get("magtype")) if emsc else None
        # EMSC eşleşmesi yoksa (ör. Marmara dışı) EMSC tamamen gizli — işaret/sütun/uyarı gösterilmez.
        ai_locations = []
        station_df = load_station_coordinates()
    
        # ÖN-HESAPLANMIŞ AI SONUÇLARI (GCS AI_RESULTS.csv) — senkron /associate + WA-ML hesabı
        # KALDIRILDI. Hesaplama offline yapılır (precompute_ai_results.py); sayfa hazır sonucu okur.
        _pre = load_precomputed_results()
        if not _pre.empty:
            for _, _r in _pre[_pre['event_id'].astype(str) == str(selected_event)].iterrows():
                def _pv(k, cast=None):
                    v = _r.get(k)
                    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan"):
                        return None
                    return cast(v) if cast else v
                _d = {
                    "model": str(_r['model']), "base_model": _pv('base_model'),
                    "lat": _pv('lat', float), "lon": _pv('lon', float), "depth": _pv('depth', float),
                    "rms": _pv('rms', float), "erh": _pv('erh', float), "erz": _pv('erz', float),
                    "gap": _pv('gap', float), "source": _pv('source'), "warning": _pv('warning'),
                    "error": _pv('error'),
                    "stations": ([s for s in str(_r['stations']).split(';') if s] if _pv('stations') else []),
                    "mag": _pv('mag', float),
                }
                if _d["lat"] is None and not _d["error"]:
                    _d["error"] = T("Sonuç yok", "No result")
                ai_locations.append(_d)
        if not ai_locations and not df_model.empty and not df_model[df_model['event_id'] == selected_event].empty:
            st.info(T("Bu olayın AI sonuçları henüz ön-hesaplanmadı (toplu hesaplama sürüyor olabilir); hazır olunca otomatik görünecek.",
                      "AI results for this event are not precomputed yet (batch may be running); they will appear automatically once ready."))

        has_any_location = (afad_lat is not None) or any(loc["lat"] is not None for loc in ai_locations)
        if has_any_location:
            import plotly.graph_objects as go
            fig_map = go.Figure()
        
            if afad_lat is not None and afad_lon is not None:
                depth_text = f" | Derinlik: {afad_depth:.1f}km" if afad_depth is not None else ""
                fig_map.add_trace(go.Scattermapbox(
                    lat=[afad_lat], lon=[afad_lon], mode='markers+text',
                    marker=go.scattermapbox.Marker(size=14, color='blue'),
                    text=[T("AFAD Katalog", "AFAD Catalog")], textposition="bottom right",
                    name=T("AFAD Manuel", "AFAD Manual"),
                    hoverinfo="text",
                    hovertext=f"AFAD Manuel Lokasyonu<br>Enlem: {afad_lat:.4f}, Boylam: {afad_lon:.4f}{depth_text}"
                ))

            if emsc_lat is not None and emsc_lon is not None:
                _edt = f" | Derinlik: {emsc_depth:.1f}km" if emsc_depth is not None else ""
                _emg = f" | {emsc_mtype} {emsc_mag:.1f}" if emsc_mag is not None else ""
                fig_map.add_trace(go.Scattermapbox(
                    lat=[emsc_lat], lon=[emsc_lon], mode='markers+text',
                    marker=go.scattermapbox.Marker(size=14, color='#FFD700'),
                    text=["EMSC"], textposition="top left",
                    name="EMSC Katalog",
                    hoverinfo="text",
                    hovertext=f"EMSC Katalog<br>Enlem: {emsc_lat:.4f}, Boylam: {emsc_lon:.4f}{_edt}{_emg}"
                ))

            # Her AI konfigürasyonuna haritada AYRI renk (ör. PhaseNet'in 4 kurgusu farklı renklerde).
            # AFAD (mavi) ve istasyonlar (gri) ile çakışmayan ayırt edici bir palet.
            ai_palette = ["#e41a1c", "#ff7f00", "#4daf4a", "#984ea3",
                          "#f781bf", "#a65628", "#17becf", "#bcbd22"]
        
            # Tüm yapay zeka konfigürasyonlarının kullandığı istasyonları tek bir havuzda topla ve sadece bir kere çiz
            all_used_stations = set()
            for loc in ai_locations:
                if loc.get("stations"):
                    all_used_stations.update(loc["stations"])
            
            if all_used_stations:
                st_df = station_df[station_df["id"].isin(all_used_stations)]
                if not st_df.empty:
                    fig_map.add_trace(go.Scattermapbox(
                        lat=st_df["y"], lon=st_df["x"], mode='markers',
                        marker=go.scattermapbox.Marker(
                            size=10, 
                            color="gray", 
                            opacity=0.6
                        ),
                        name=T("Kullanılan İstasyonlar", "Used Stations"),
                        hoverinfo="text",
                        hovertext=st_df["id"]
                    ))

            for idx, loc in enumerate(ai_locations):
                if loc["lat"] is not None and loc["lon"] is not None:
                    color = ai_palette[idx % len(ai_palette)]
                    loc["color"] = color  # tabloda da aynı renk noktasını kullanmak için sakla
                    depth_text = f" | Derinlik: {loc['depth']:.1f}km" if loc['depth'] is not None else ""
                    rms_text = f" | RMS: {loc['rms']:.2f}" if loc.get("rms") is not None else ""
                    mag_text = f" | ML {loc['mag']:.1f}" if loc.get("mag") is not None else ""

                    # Sadece ilk eklenen AI modelinde label'ı sağ üste, diğerlerinde sağ alta vs koyalım
                    pos = "top right" if idx % 2 == 0 else "bottom left"
                    
                    warning_text = f"<br><b>UYARI:</b> {loc.get('warning')}" if loc.get("warning") else ""
                
                    fig_map.add_trace(go.Scattermapbox(
                        lat=[loc["lat"]], lon=[loc["lon"]], mode='markers+text',
                        marker=go.scattermapbox.Marker(size=14, color=color),
                        text=[loc["model"]], textposition=pos,
                        name=loc["model"],
                        hoverinfo="text",
                        hovertext=f"{loc['model']} Lokasyonu<br>Enlem: {loc['lat']:.4f}, Boylam: {loc['lon']:.4f}{depth_text}{mag_text}{rms_text}{warning_text}"
                    ))

            all_lats = [afad_lat, emsc_lat] + [loc["lat"] for loc in ai_locations if loc["lat"] is not None]
            all_lons = [afad_lon, emsc_lon] + [loc["lon"] for loc in ai_locations if loc["lon"] is not None]
            all_lats = [x for x in all_lats if x is not None]
            all_lons = [x for x in all_lons if x is not None]

            center_lat = sum(all_lats) / len(all_lats) if all_lats else 39.0
            center_lon = sum(all_lons) / len(all_lons) if all_lons else 35.0
            # Dinamik zoom: sadece lokasyon noktalarının (AFAD/EMSC/modeller; İSTASYONLAR HARİÇ) yayılımına
            # göre yakınlaştır ki 1-5 km'lik küçük farklar üst üste binmeyip görünür olsun.
            import math as _mz
            if len(all_lats) >= 2 and (max(all_lats) > min(all_lats) or max(all_lons) > min(all_lons)):
                _span = max(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons), 0.03)
                _zoom = max(4.0, min(12.5, 8.7 - _mz.log2(_span)))
            else:
                _zoom = 10.0

            fig_map.update_layout(
                mapbox_style="open-street-map",
                mapbox=dict(center=dict(lat=center_lat, lon=center_lon), zoom=_zoom),
                margin={"r":0,"t":0,"l":0,"b":0},
                height=400,
                showlegend=True,
                legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01)
            )
            st.plotly_chart(fig_map, use_container_width=True)
        
            # UI Feedback for AI Models
            if ai_locations:
                st.markdown("### 🤖 Yapay Zeka Kataloglama Durumu")
                cols = st.columns(len(ai_locations))
                for i, loc in enumerate(ai_locations):
                    with cols[i]:
                        if loc["lat"] is not None:
                            rel = loc_reliability(loc, afad_lat, afad_lon)
                            loc["rel"] = rel  # karşılaştırma tablosunda tekrar kullanılacak
                            badge = _REL_BADGE[rel["level"]]
                            if loc['depth'] is None:
                                depth_str = ""
                            elif rel["depth_ok"]:
                                depth_str = f" (Derinlik: {loc['depth']:.1f}km)"
                            else:
                                depth_str = f" (Derinlik: {T('belirsiz', 'unreliable')})"
                            mag_str = f"\n\n**Magnitüd (ML):** {loc['mag']:.1f}" if loc.get("mag") is not None else ""
                            rms_str = f"\n\n**RMS:** {loc['rms']:.2f} s" if loc.get("rms") is not None else ""
                            # İç kaynak adları arayüzde nötr gösterilir (yöntem adı sitede geçmesin)
                            _src_map = {"HypoInverse": T("hassas çözüm", "precise solution"),
                                        "PyOcto": T("kaba çözüm", "coarse solution"),
                                        "PyOcto(coarse)": T("kaba çözüm", "coarse solution")}
                            _src_raw = loc.get('source', 'Bilinmiyor')
                            src_str = f"\n\n**Kaynak:** {_src_map.get(_src_raw, _src_raw)}"
                            warn_str = f"\n\n⚠️ **UYARI:** {loc.get('warning')}" if loc.get("warning") else ""
                            body = f"**{loc['model']}** {badge}\n\n{loc['lat']:.2f}N, {loc['lon']:.2f}E{depth_str}{mag_str}{rms_str}{src_str}{warn_str}"
                            if rel["level"] == "low":
                                rz = "; ".join(rel["reasons"])
                                st.warning(f"{body}\n\n🔴 **{T('Düşük güven', 'Low confidence')}:** {rz}")
                            elif rel["level"] == "med":
                                st.info(body)
                            else:
                                st.success(body)
                        else:
                            st.error(f"**{loc['model']}**\n\n❌ {loc['error']}")

                # === KARŞILAŞTIRMA TABLOSU: AFAD vs Modeller (parametre başına AFAD'a en yakın = kalın) ===
                located = [l for l in ai_locations if l["lat"] is not None and l["lon"] is not None]
                if afad_lat is not None and afad_lon is not None and located:
                    import math

                    def _hav(la1, lo1, la2, lo2):
                        R = 6371.0
                        p1, p2 = math.radians(la1), math.radians(la2)
                        dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
                        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
                        return 2 * R * math.asin(min(1.0, math.sqrt(a)))

                    def _fmt(v, nd):
                        return "—" if v is None else f"{v:.{nd}f}"

                    def _fmt_p(v, nd, prefix):
                        if v is None:
                            return "—"
                        s = f"{v:.{nd}f}"
                        return f"{prefix} {s}" if prefix else s

                    st.markdown(f"### 📋 {T('Karşılaştırma (AFAD · EMSC · Modeller)', 'Comparison (AFAD · EMSC · Models)')}")

                    show_emsc = emsc is not None
                    dists = [_hav(afad_lat, afad_lon, l["lat"], l["lon"]) for l in located]
                    emsc_dist = _hav(afad_lat, afad_lon, emsc_lat, emsc_lon) if emsc_lat is not None else None
                    # Magnitüd satırında her hücre kendi tipini taşır: AFAD=katalog tipi (ML/Mw),
                    # EMSC=kendi tipi, AI modelleri=Wood-Anderson ML.
                    _afad_mt = norm_mag_type(afad_info.get("mag_type"))
                    # (label, afad_v, emsc_v, [model_vals], nd, prefixes=(afad,emsc,model) | None)
                    param_rows = [
                        (T("Magnitüd", "Magnitude"), afad_info.get("magnitude"), emsc_mag, [l.get("mag") for l in located], 1, (_afad_mt, emsc_mtype, "ML")),
                        (T("Enlem (°N)", "Latitude (°N)"), afad_lat, emsc_lat, [l["lat"] for l in located], 4, None),
                        (T("Boylam (°E)", "Longitude (°E)"), afad_lon, emsc_lon, [l["lon"] for l in located], 4, None),
                        (T("Derinlik (km)", "Depth (km)"), afad_depth, emsc_depth,
                         [(l["depth"] if l.get("rel", {}).get("depth_ok", True) else None) for l in located], 1, None),
                    ]

                    header = ("<th style='text-align:left;padding:6px 10px'>" + T("Parametre", "Parameter") + "</th>"
                              + "<th style='padding:6px 10px'><span style='color:#1f77b4'>●</span> AFAD</th>")
                    if show_emsc:
                        header += "<th style='padding:6px 10px'><span style='color:#FFD700'>●</span> EMSC</th>"
                    for l in located:
                        c = l.get("color", "#333")
                        _b = _REL_BADGE.get(l.get("rel", {}).get("level", "med"), "")
                        header += (f"<th style='padding:6px 10px;white-space:nowrap'>"
                                   f"<span style='color:{c}'>●</span> {l['model']} {_b}</th>")

                    body = ""
                    for label, afad_v, emsc_v, vals, nd, prefixes in param_rows:
                        ap, ep, mp = prefixes if prefixes else (None, None, None)
                        best_i = None
                        if afad_v is not None:
                            diffs = [(abs(v - afad_v), i) for i, v in enumerate(vals) if v is not None]
                            if diffs:
                                best_i = min(diffs)[1]
                        cells = f"<td style='text-align:center;padding:6px 10px'>{_fmt_p(afad_v, nd, ap)}</td>"
                        if show_emsc:
                            cells += f"<td style='text-align:center;padding:6px 10px'>{_fmt_p(emsc_v, nd, ep)}</td>"
                        for i, v in enumerate(vals):
                            txt = _fmt_p(v, nd, mp)
                            cells += ("<td style='text-align:center;padding:6px 10px'>"
                                      + (f"<b>{txt}</b>" if i == best_i else txt) + "</td>")
                        body += f"<tr><td style='text-align:left;padding:6px 10px'><b>{label}</b></td>{cells}</tr>"

                    # AFAD'a uzaklık satırı (referans AFAD = —; modeller arası en küçük = kalın)
                    best_d = min(range(len(dists)), key=lambda k: dists[k]) if dists else None
                    dcells = "<td style='text-align:center;padding:6px 10px'>—</td>"
                    if show_emsc:
                        dcells += f"<td style='text-align:center;padding:6px 10px'>{(f'{emsc_dist:.1f}' if emsc_dist is not None else '—')}</td>"
                    for i, d in enumerate(dists):
                        dcells += ("<td style='text-align:center;padding:6px 10px'>"
                                   + (f"<b>{d:.1f}</b>" if i == best_d else f"{d:.1f}") + "</td>")
                    body += ("<tr><td style='text-align:left;padding:6px 10px'><b>"
                             + T("AFAD’a Uzaklık (km)", "Distance to AFAD (km)") + f"</b></td>{dcells}</tr>")

                    # RMS satırı (HypoInverse konum çözüm kalitesi; DÜŞÜK = daha iyi -> en küçük kalın)
                    afad_rms = afad_info.get("rms")
                    rms_vals = [l.get("rms") for l in located]
                    _valid_rms = [(v, i) for i, v in enumerate(rms_vals) if v is not None]
                    best_r = min(_valid_rms)[1] if _valid_rms else None
                    rcells = f"<td style='text-align:center;padding:6px 10px'>{_fmt(afad_rms, 2)}</td>"
                    if show_emsc:
                        rcells += "<td style='text-align:center;padding:6px 10px'>—</td>"
                    for i, v in enumerate(rms_vals):
                        txt = _fmt(v, 2)
                        rcells += ("<td style='text-align:center;padding:6px 10px'>"
                                   + (f"<b>{txt}</b>" if i == best_r else txt) + "</td>")
                    body += f"<tr><td style='text-align:left;padding:6px 10px'><b>RMS (s)</b></td>{rcells}</tr>"

                    # Güven satırı: geçmiş event'te AFAD'a uzaklık (gerçek doğruluk) + QC metrikleri
                    lvl_txt = {"high": T("Yüksek", "High"), "med": T("Orta", "Medium"), "low": T("Düşük", "Low")}
                    gcells = "<td style='text-align:center;padding:6px 10px'>—</td>"
                    if show_emsc:
                        gcells += "<td style='text-align:center;padding:6px 10px'>—</td>"
                    for l in located:
                        lv = l.get("rel", {}).get("level", "med")
                        gcells += (f"<td style='text-align:center;padding:6px 10px'>"
                                   f"{_REL_BADGE.get(lv, '')} {lvl_txt[lv]}</td>")
                    body += (f"<tr><td style='text-align:left;padding:6px 10px'><b>"
                             + T("Güven", "Confidence") + f"</b></td>{gcells}</tr>")

                    st.markdown(
                        "<div style='overflow-x:auto'><table style='border-collapse:collapse;"
                        "width:100%;font-size:0.9rem'>"
                        f"<thead><tr style='border-bottom:2px solid #888'>{header}</tr></thead>"
                        f"<tbody>{body}</tbody></table></div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(T("Kalın = her satırda en iyi model (Enlem/Boylam/Derinlik: AFAD'a en yakın; Uzaklık/RMS: en düşük). "
                                 "Magnitüd: AI modelleri Wood-Anderson ML üretir; AFAD/EMSC ise katalog tipini gösterir (ML/Mw). "
                                 "Güven 🟢≤15km / 🟡≤50km / 🔴>50km AFAD'a uzaklık (+ RMS/hata/derinlik kontrolü); güvenilmez derinlik gizlenir. "
                                 "Bölgesel/büyük (M>5, seyrek ağ) depremlerde AI konumu daha az güvenilirdir; AFAD referans alınmalıdır.",
                                 "Bold = best model per row (Lat/Lon/Depth: closest to AFAD; Distance/RMS: lowest). "
                                 "Magnitude: AI models yield Wood-Anderson ML; AFAD/EMSC show their catalog type (ML/Mw). "
                                 "Confidence 🟢≤15km / 🟡≤50km / 🔴>50km from AFAD (+ RMS/error/depth checks); unreliable depth is hidden. "
                                 "For regional/large (M>5, sparse network) events AI location is less reliable; treat AFAD as reference."))
        else:
            st.info(T("Bu deprem için AFAD veya Yapay Zeka lokasyon verisi bulunmuyor.", "No AFAD or AI location data found for this event."))
    else:
        # Canlı event: live_worker'ın /associate (PyOcto + HypoInverse) ile hesapladığı lokasyon.
        st.subheader(T("🔴 Canlı Tespit Lokasyonu", "🔴 Live Detection Location"))
        live_info = location_map.get(selected_event, {}) or {}
        llat = live_info.get("lat")
        llon = live_info.get("lon")
        ldepth = live_info.get("depth")
        if llat is not None and llon is not None:
            import plotly.graph_objects as go
            station_df = load_station_coordinates()
            fig_live = go.Figure()
            fig_live.add_trace(go.Scattermapbox(
                lat=[llat], lon=[llon], mode='markers+text',
                marker=go.scattermapbox.Marker(size=16, color='red'),
                text=[selected_event], textposition="top right",
                name=T("Canlı Konum", "Live Location"),
                hoverinfo="text",
                hovertext=f"{llat:.4f}N {llon:.4f}E" + (f" | {T('Derinlik','Depth')}: {ldepth:.1f}km" if ldepth is not None else "")
            ))
            live_stations = sorted(df_model[df_model['event_id'] == selected_event]['station'].unique())
            if not station_df.empty:
                sdf = station_df[station_df["id"].isin(live_stations)]
                if not sdf.empty:
                    fig_live.add_trace(go.Scattermapbox(
                        lat=sdf["y"], lon=sdf["x"], mode='markers',
                        marker=go.scattermapbox.Marker(size=9, color='gray', opacity=0.6),
                        name=T("İstasyonlar", "Stations"), hoverinfo="text", hovertext=sdf["id"]
                    ))
            fig_live.update_layout(
                mapbox_style="open-street-map",
                mapbox=dict(center=dict(lat=llat, lon=llon), zoom=6),
                margin={"r": 0, "t": 0, "l": 0, "b": 0}, height=400, showlegend=True,
                legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01)
            )
            st.plotly_chart(fig_live, use_container_width=True)
            lmag = live_info.get("magnitude")
            lrms = live_info.get("rms")
            ln_sta = live_info.get("n_sta")
            lerh, lerz, lgap = live_info.get("erh"), live_info.get("erz"), live_info.get("gap")
            ln_p, ln_s = live_info.get("n_p"), live_info.get("n_s")
            lproc = live_info.get("proc_s")
            lmodel = live_info.get("model")
            lorigin = live_info.get("origin")
            # Eski katalog satırlarında P/S ve istasyon kolonları yok -> pick tablosundan türet
            _ev_picks = df_model[df_model['event_id'] == selected_event]
            if (ln_p is None or ln_s is None) and len(_ev_picks) > 0 and 'phase' in _ev_picks.columns:
                _ph = _ev_picks['phase'].astype(str).str.upper()
                ln_p = int((_ph == 'P').sum()) if ln_p is None else ln_p
                ln_s = int((_ph == 'S').sum()) if ln_s is None else ln_s
            if ln_sta is None and len(_ev_picks) > 0:
                ln_sta = int(_ev_picks['station'].nunique())

            if lorigin is not None:
                from datetime import timedelta as _td
                _tr_t = lorigin + _td(hours=3)
                st.markdown(f"**Origin time:** {_tr_t.strftime('%d.%m.%Y %H:%M:%S')} TSİ "
                            f"({lorigin.strftime('%H:%M:%S')} UTC)")

            # Canlıda AFAD referansı yok -> güven yalnız QC (RMS / S varlığı) ile.
            # NOT: eski "derinlik > 60 km => belirsiz" kuralı KALDIRILDI. Güney Ege'de Helen
            # dalma-batma zonu nedeniyle 100-180 km derinlikli olaylar NORMALDİR; kural gerçek
            # derin depremleri (ör. LIVE_20260804_204355: bizde 136 km, NOA'da 112 km) haksız
            # yere "belirsiz" işaretliyordu. S fazı yoksa derinlik hâlâ kısıtlanamaz -> belirsiz.
            live_depth_ok = not ((lrms is not None and lrms > 1.3)
                                 or (ln_s is not None and ln_s == 0))
            _lmw, _lmw_err = live_info.get("mw"), live_info.get("mw_err")
            r1 = st.columns(6 if _lmw is not None else 5)
            r1[0].metric(T("Magnitüd", "Magnitude"), f"{lmag:.1f}" if lmag is not None else "—")
            r1[1].metric(T("Magnitüd türü", "Magnitude type"), "ML" if lmag is not None else "—",
                         help=T("Wood-Anderson simülasyonlu yerel magnitüd (istasyon medyanı)",
                                "Wood-Anderson simulated local magnitude (station median)"))
            if _lmw is not None:
                r1[2].metric("Mw", f"{_lmw:.2f}" + (f" ±{_lmw_err:.2f}" if _lmw_err else ""),
                             help=T("Spektral moment magnitüd: P/S yer değiştirme spektrumlarından "
                                    "sismik moment (M0) çözülerek hesaplanır (SourceSpec). "
                                    "Dönüşüm/bağıntı değil, doğrudan ölçümdür.",
                                    "Spectral moment magnitude from displacement-spectra inversion "
                                    "for seismic moment (SourceSpec) — a direct measurement, "
                                    "not a converted proxy."))
            _sh = 1 if _lmw is not None else 0   # Mw sütunu eklendiyse kalanlar bir sağa kayar
            r1[2 + _sh].metric(T("Enlem", "Latitude"), f"{llat:.4f}")
            r1[3 + _sh].metric(T("Boylam", "Longitude"), f"{llon:.4f}")
            r1[4 + _sh].metric(T("Derinlik", "Depth"),
                               (f"{ldepth:.1f} km" if live_depth_ok else T("belirsiz", "unreliable")) if ldepth is not None else "—")
            r2 = st.columns(6)
            r2[0].metric("RMS", f"{lrms:.2f} s" if lrms is not None else "—")
            r2[1].metric("ERH", f"{lerh:.1f} km" if lerh is not None else "—")
            r2[2].metric("ERZ", f"{lerz:.1f} km" if lerz is not None else "—")
            r2[3].metric("Gap", f"{lgap:.0f}°" if lgap is not None else "—")
            r2[4].metric(T("İstasyon", "Stations"), f"{ln_sta}" if ln_sta is not None else "—")
            r2[5].metric(T("P / S fazı", "P / S phases"),
                         f"{ln_p if ln_p is not None else '—'} / {ln_s if ln_s is not None else '—'}")
            def _dur(sec):
                return f"{sec} sn" if sec < 60 else f"{sec // 60} dk {sec % 60} sn"

            _foot = []
            if lproc is not None:
                _delayed = live_info.get("delayed")
                _net = live_info.get("proc_net_s")
                # Yıldız YALNIZCA süre gerçekten veri gecikmesi içeriyorsa (net != toplam).
                if _delayed and _net is not None and _net != lproc:
                    # Gecikmeli (sadece-FDSN) istasyon kullanıldı: toplam sürenin ~5 dk'sı
                    # veri temini beklemesi. Yıldızla işaretle ve net süreyi göster.
                    _foot.append(T(f"İşlem süresi: {_dur(_net)} \\*  (toplam {_dur(lproc)})",
                                   f"Processing time: {_dur(_net)} \\*  (total {_dur(lproc)})"))
                else:
                    _foot.append(T(f"İşlem süresi (deprem → kayıt): {_dur(lproc)}",
                                   f"Processing time (event → record): {_dur(lproc)}"))
            if lmodel:
                _foot.append(T(f"Model: {lmodel}", f"Model: {lmodel}"))
            if _foot:
                st.caption(" · ".join(_foot))
            if live_info.get("delayed") and live_info.get("proc_net_s") not in (None, lproc):
                st.caption("\\* " + T(
                    "Bu olayın çözümünde verisi ~5 dakika gecikmeli yayınlanan bir istasyon "
                    "kullanılmıştır; toplam süre bu veri temini beklemesini içerir. "
                    "İşlem süresi = toplam süre − veri gecikmesi.",
                    "A station whose data is published with a ~5-minute delay contributed to this "
                    "solution; the total duration includes that data-acquisition wait. "
                    "Processing time = total duration − data latency."))
            if ln_sta is not None and ln_sta < 3:
                st.warning("⚠️ " + T(f"Bu olay yalnız {ln_sta} istasyonla tespit edildi (<3): düşük güvenilirlik — "
                                     "sadece kataloğa kaydedildi, bildirim gönderilmedi.",
                                     f"This event was detected with only {ln_sta} stations (<3): low confidence — "
                                     "recorded to catalog only, no notification was sent."))
            if live_info.get("location"):
                st.caption(live_info["location"])
            if not live_depth_ok or (lrms is not None and lrms > 1.3):
                st.caption("⚠️ " + T("Yüksek RMS/derinlik hatası — konum düşük güvenilirlikte olabilir.",
                                     "High RMS/depth error — location may be low-confidence."))
        else:
            st.info(T("Bu canlı deprem için henüz hesaplanmış lokasyon yok.",
                      "No computed location for this live event yet."))