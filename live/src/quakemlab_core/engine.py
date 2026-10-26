"""QuakeMLab canlı tespit MOTORU (engine).

Bu modül gerçek-zamanlı boru hattının tamamını içerir: SeedLink çok-istasyon
edinim -> PhaseNet pikleme -> PyOcto ilişkilendirme -> (API üzerinden) HypoInverse
lokasyon -> Wood-Anderson ML + spektral Mw -> GCS katalog -> Telegram bildirim.

Bu parçalar tek bir sıkı paylaşılan çalışma-zamanı durumunu (canlı akış tamponları,
model, envanter) kullandığından bilinçli olarak TEK uyumlu modülde tutulur; bölgeye
özel yapılandırma `config.py` (regions/<bolge>.yaml), saf yardımcılar `geocode.py`,
spektral Mw `spectral_mw.py`, bildirim `notify.py` modüllerindedir.

Giriş: `python -m quakemlab_core.engine` yerine `services/worker/worker_service.py`
bunu arka planda başlatır (Cloud Run sağlık endpoint'i ile).
"""
import os
import socket
import threading
import time
import datetime
import io
import json
import tempfile
import contextlib
import zipfile
import math
import csv
import statistics
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import obspy
from obspy.clients.fdsn import Client as FDSNClient
import requests
import pyproj
from google.cloud import storage

import seisbench.models as sbm

# Bölgeye özel yapılandırma + türetilmiş nesneler (STATIONS, station_df, associator,
# XLIM/YLIM, eşikler, sabitler...) — hepsi buradan gelir.
from .config import *          # noqa: F401,F403
from . import config
# `import *` alt çizgiyle başlayan adları almaz; motorun kullandıkları açıkça:
from .config import _STA_LATLON, _CATALOG_HEADER

# Saf/bağımsız yardımcılar (globalsiz) ayrı modüllerde.
from .geocode import hav_km as _hav_km, azimuthal_gap as _azimuthal_gap, reverse_geocode
from .notify import post_to_x

try:
    from .spectral_mw import compute_spectral_mw
except Exception as _e:      # SourceSpec kurulu değilse sistem Mw'siz çalışır
    compute_spectral_mw = None
    print(f"[MW] spektral Mw devre dışı: {str(_e)[:80]}")

# ---------------------------------------------------------------------------
# Çalışma-zamanı (mutable) durum — import sabit değil, koşarken değişir.
# ---------------------------------------------------------------------------
model = None                      # PhaseNet; main()'de tembel yüklenir
pick_pool = []
pool_lock = threading.Lock()
global_streams = {}               # tüm sunucuların akışları (mseed kaydı için)
streams_lock = threading.Lock()
analysis_lock = threading.Lock()  # analiz tek thread'de (yarış/çift-kayıt önlenir)
last_analysis_ts = 0.0
reported_events = []              # debounce + iyileştirme kararları
reported_lock = threading.Lock()
station_inventory = None          # cihaz yanıtları (main'de arka planda yüklenir)

# _hav_km, _azimuthal_gap, reverse_geocode -> geocode.py (başta import edildi)

# FDSN uçları: obspy Client'ın "servis keşfi" adımı konteynerde zaman aşımına uğrayıp
# istemciyi kalıcı bozuk bırakıyor ("does not have a dataselect service"). Bu yüzden
# dataselect/station sorgularını DOĞRUDAN HTTP ile yapıyoruz (keşif adımı yok).


def _http_inventory(prov, net, stas):
    """Ham HTTP FDSN station sorgusu -> Inventory (cihaz yanıtı dahil). Hata olursa None."""
    base = FDSN_BASE.get(prov)
    if not base:
        return None
    r = requests.get(f"{base}/fdsnws/station/1/query",
                     params={"net": net, "sta": stas, "cha": "?H?",
                             "level": "response", "format": "xml", "nodata": "204"},
                     timeout=90)
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"HTTP {r.status_code}")
    return obspy.read_inventory(io.BytesIO(r.content))


def fetch_response_inventory():
    """Canlı istasyonların cihaz yanıtını yükler (ML/Mw için). Başarısızsa None.
    Ege: FDSN'den (GEOFON/NOA/IRIS/INGV) çekilir.
    Marmara: AFAD'ın açık FDSN station servisi YOK -> imaja gömülü StationXML'lerden
    (station_xml_marmara/, SAKUN kaynaklı) okunur."""
    global station_inventory
    try:
        from obspy import Inventory
        inv = Inventory()
        if RESPONSE_SOURCE == "local":
            # Açık FDSN station servisi olmayan ağlar (ör. AFAD): gömülü StationXML.
            import glob as _glob
            files = sorted(_glob.glob(os.path.join(STATION_XML_DIR, "*.xml")))
            for f in files:
                try:
                    inv += obspy.read_inventory(f)
                except Exception as e:
                    print(f"[MAG XML] {os.path.basename(f)}: {str(e)[:80]}")
        else:
            # FDSN: bölge YAML'ındaki response.fdsn_groups
            for grp in FDSN_RESPONSE_GROUPS:
                prov, net, stas = grp["provider"], grp["net"], grp["stations"]
                try:
                    got = _http_inventory(prov, net, stas)
                    if got is not None:
                        inv += got
                except Exception as e:
                    print(f"[FDSN {prov}] {type(e).__name__}: {str(e)[:80]}")
        station_inventory = inv if len(inv) else None
        if station_inventory is not None:
            _n = sum(len(nw) for nw in station_inventory)
            print(f"[MAG] Cihaz yanıtı: {_n} istasyon yüklendi")
            return
    except Exception as e:
        print(f"[FDSN HATA] {e}")
        station_inventory = None
    print("[MAG] Cihaz yanıtı: YOK -> magnitüd devre dışı")


def _wa_amp_mm(stream_slice):
    """Yatay bileşenlerden Wood-Anderson tepe genliği (mm). Yoksa None."""
    if station_inventory is None:
        return None
    try:
        horiz = obspy.Stream([tr for tr in stream_slice
                              if tr.stats.channel[-1].upper() in ("E", "N", "1", "2")]).copy()
        if len(horiz) == 0:
            return None
        horiz.merge(fill_value=0); horiz.detrend("demean"); horiz.taper(0.05)
        resp = station_inventory.get_response(horiz[0].id, horiz[0].stats.starttime)
        paz = resp.get_paz()
        paz_sta = {"poles": list(paz.poles), "zeros": list(paz.zeros),
                   "gain": paz.normalization_factor,
                   "sensitivity": resp.instrument_sensitivity.value}
        horiz.simulate(paz_remove=paz_sta, paz_simulate=PAZ_WA, water_level=10, pre_filt=WA_PRE_FILT)
        amp = max(float(np.max(np.abs(tr.data))) for tr in horiz) * 1000.0
        return amp if amp > 0 else None
    except Exception:
        return None


def _median_ml(wa_amps, ev_lat, ev_lon, ev_depth):
    """İstasyon genliklerinden medyan ML (Hutton-Boore 1987, hiposantral)."""
    dep = ev_depth if ev_depth else 8.0
    mls = []
    for sta, amp in wa_amps.items():
        if not amp or amp <= 0:
            continue
        ll = _STA_LATLON.get(sta)
        if not ll:
            continue
        d = _hav_km(ev_lat, ev_lon, ll[0], ll[1])
        r = math.sqrt(d * d + dep * dep)
        if r <= 0:
            continue
        corr = 1.110 * math.log10(r / 100.0) + 0.00189 * (r - 100.0) + 3.0
        mls.append(math.log10(amp) + corr)
    return round(statistics.median(mls), 2) if mls else None


def get_location_from_api(assigned_df, trial=None):
    """Atanmış pick'leri /associate API'sine yollar (hassas konum + RMS/ERH/ERZ/Gap).
    trial=(lat, lon, depth) verilirse API yeniden İLİŞKİLENDİRME YAPMAZ; pick'ler zaten
    bu worker tarafından ilişkilendirildiği için doğrudan HypoInverse çalıştırılır.
    Başarılıysa {lat, lon, depth, rms, erh, erz, gap, source} döner, aksi halde None."""
    try:
        picks_payload = [
            {"station": r["station"], "phase": r["phase"],
             "time": float(r["time"]), "probability": float(r["probability"])}
            for _, r in assigned_df.iterrows()
        ]
        stations_payload = station_df[["id", "x", "y", "z"]].to_dict(orient="records")
        _payload = {"picks": picks_payload, "stations": stations_payload, "live": True,
                    "bolge": BOLGE}   # API HypoInverse hiz modelini buna gore secer
        if trial is not None:
            # Yeniden ilişkilendirme atlanır; kaba konum HypoInverse'e başlangıç noktası olur.
            _payload.update({"skip_association": True, "trial_lat": float(trial[0]),
                             "trial_lon": float(trial[1]), "trial_depth": float(trial[2] or 8.0)})
        resp = requests.post(f"{PYOCTO_API_URL}/associate", json=_payload, timeout=120)
        if resp.status_code == 200:
            d = resp.json()
            if d.get("success"):
                data = d["data"]
                def _fv(k):
                    v = data.get(k)
                    return float(v) if v is not None else None
                return {"lat": float(data["lat"]), "lon": float(data["lon"]),
                        "depth": float(data.get("depth") or 0.0),
                        "rms": _fv("rms"), "erh": _fv("erh"), "erz": _fv("erz"),
                        "gap": _fv("gap"),
                        "source": data.get("source", "API")}
            else:
                print(f"[API] birleşmedi: {d.get('error')}")
    except Exception as e:
        print(f"[API HATA] {e}")
    return None


def _ingest_stream(key, incoming):
    """FDSN'den çekilen akışı global_streams[key]'e ekle: birleştir (interpolate) + buffer'a kırp.
    Örtüşen ardışık çekişler merge ile tekilleştirilir; çakışırsa en taze pencereyle sıfırlanır.
    Dönen: buffer'ın son endtime'ı (yoksa None)."""
    if incoming is None or len(incoming) == 0:
        return None
    with streams_lock:
        if key not in global_streams:
            global_streams[key] = obspy.Stream()
        global_streams[key] += incoming
        try:
            global_streams[key].merge(fill_value='interpolate')
        except Exception:
            # Örtüşen segmentler çakıştıysa (nadiren) birikimi önlemek için taze pencereye dön
            try:
                incoming.merge(fill_value='interpolate')
            except Exception:
                pass
            global_streams[key] = incoming
        t_end = global_streams[key][-1].stats.endtime
        global_streams[key].trim(starttime=t_end - BUFFER_SIZE_SECONDS)
    return t_end


def maybe_analyze():
    """Analizi 30 sn'de bir, tek thread'de çalıştırır (yarış önleme)."""
    global last_analysis_ts
    if not analysis_lock.acquire(blocking=False):
        return
    try:
        if time.time() - last_analysis_ts < 30:
            return
        last_analysis_ts = time.time()
        analyze_streams()
    except Exception as e:
        print(f"[ANALYZE HATA] {e}")
    finally:
        analysis_lock.release()


# S eşiği AYRI ve daha düşük: PhaseNet'te S olasılıkları sistematik olarak P'den zayıf çıkıyor;
# 0.65'te çoğu olay S'siz kalıyordu (ör. LIVE_20260803_231144: 7 P, 0 S). S olmadan HypoInverse
# derinliği ve origin zamanını kısıtlayamıyor -> çözüm boylamda kayıyor, kalite bekçisi reddediyor,
# RMS/ERH/ERZ boş kalıyor. API zaten pick'leri >=0.40 güvenle süzüyor, bu yüzden 0.45 güvenli aralık.

# Aynı sahadaki istasyonlar (istasyon sayısı hesabında TEK sayılır).
# THERA çıkarıldığı için şu an eşleşme yok; ileride co-located istasyon eklenirse buraya yazılır.
_SITE_ALIAS = {}

# Kataloglama eşiği (bunun altındaki çözüm hiç kaydedilmez): en az MIN_P_STATIONS ayrı
# sahada P fazı VE en az MIN_S_PICKS adet S fazı. Bildirim kuralı (n_sta>=3) ayrıca geçerli.
# KALİBRASYON (2026-08-04, o günün 19 olayı): eşik 3 olunca bağımsız kataloglarla
# DOĞRULANMIŞ 3 olayın üçü de eleniyordu (hepsinin 2 P sahası vardı); 2 olunca 19 olayın
# 11'i (asıl kirlilik: "1 P + 2 S" tipi çözümler) elenip doğrulanmış olaylar korunuyor.
# Yeniden derlemeden ayarlanabilir: gcloud run services update --update-env-vars MIN_P_STATIONS=3
# Gecikmeli (sadece-FDSN) istasyonlar STATIONS listesinden türetilir. Bir olayda bunlardan
# biri kullanıldıysa toplam süre ~5 dk VERİ TEMİNİ beklemesi içerir; bu bizim işlem süremiz
# değildir. Katalogda 'delayed=1' işaretlenir ve 'proc_net_s' = toplam - DELAY_OFFSET yazılır
# (ana sayfadaki medyan bu net süreyi kullanır, yoksa gecikme medyanı şişiriyordu).


# --- SİNYAL/GÜRÜLTÜ KAPISI ---
# PhaseNet'in güven skoru SNR DEĞİLDİR: LIVE_20260806_145845'te ARG'nin tamamen düz
# kaydına 0.68 güven verdi ve o istasyon geçerli sayıldı. Eşik yalnız pick SAYIYORDU,
# sinyal olup olmadığına BAKMIYORDU -> tek istasyonun gördüğü olay 3 istasyonluk gibi
# yayınlandı (RMS 0.45, ERH 23 km, konum ~15 km hatalı).
# Kural: pick, olay öncesi gürültüye göre SNR_MIN katından zayıfsa o istasyon SAYILMAZ.
# GERİYE DÖNÜK TEST (13 olay, tüm katalog): 4 kat + 2 temiz istasyon -> referansla
# doğrulanmış 8 olayın hepsi korunur, yalnız 145845 elenir. Eşik 3/4/5 aynı sonucu
# verir (geniş plato). "3 temiz istasyon" istenirse EMSC'nin 10 km ve NOA'nın 1.9 km
# ile doğruladığı iki gerçek olay kaybediliyor -> 2'de kalıyoruz.
# NOT: Gürültülü pick'ler konum hesabından ÇIKARILMIYOR, yalnız istasyon sayımında
# sayılmıyor. Pick ayıklamanın çözümü nasıl etkileyeceği test edilmedi; kapı testli olan.


def _pick_snr(sta, pick_time, phase, p_time):
    """Pick'in olay ÖNCESİ gürültüye oranı. Ölçülemezse None (o zaman istasyon cezalandırılmaz).

    sinyal : pick'ten sonraki 3 sn RMS      gürültü: P'den 16..1 sn ÖNCESİ RMS
    P için düşey, S için yatay bileşen; 2-20 Hz (Nyquist'e göre kırpılır).
    S'yi kendi öncesiyle kıyaslamak yanıltıcı olurdu (pencere P kodasıyla dolu olur),
    bu yüzden gürültü penceresi HER ZAMAN aynı istasyonun P'sinden önce alınır.
    """
    try:
        with streams_lock:
            key = next((k for k in global_streams if k.split(".")[-1] == sta), None)
            if key is None:
                return None
            st = global_streams[key].copy()
    except Exception:
        return None
    try:
        st = _best_band(st)
        comp = "Z" if str(phase).upper() == "P" else "N"
        sel = st.select(component=comp)
        if not len(sel):
            sel = st.select(component="E") or st
        if not len(sel):
            return None
        tr = sel[0]
        n0, n1 = p_time - 16.0, p_time - 1.0
        if n0 < tr.stats.starttime or (pick_time + 3.0) > tr.stats.endtime:
            return None      # tampon pencereyi kapsamıyor -> ölçemiyoruz
        tr = tr.copy().detrend("demean")
        fmax = min(20.0, 0.45 * float(tr.stats.sampling_rate))
        if fmax <= 2.5:
            return None
        tr.filter("bandpass", freqmin=2.0, freqmax=fmax, corners=4, zerophase=True)
        noise = tr.slice(n0, n1).data
        sig = tr.slice(pick_time, pick_time + 3.0).data
        if len(noise) < 10 or len(sig) < 10:
            return None
        rn = float(np.sqrt(np.mean(noise.astype(float) ** 2)))
        rs = float(np.sqrt(np.mean(sig.astype(float) ** 2)))
        return rs / max(rn, 1e-9)
    except Exception:
        return None

# --- Spektral Mw (SourceSpec) ---
# Bu ML'in altındaki olaylarda hesap YAPILMAZ: küçük olaylarda köşe frekansı kullanılabilir
# bandın üstüne çıkar, bölgesel uzaklıkta soğurulma ile birlikte Mw güvenilmez olur.
# Doğrulama: M3.4 canlı olayda Mw 3.59±0.09 (NOA MLh 3.58) -> 3.0 eşiği güvenli görünüyor.
_INV_PATH = None  # tüm istasyonların cihaz yanıtını içeren StationXML (bir kez yazılır)

def _best_band(stream):
    """Tek örnekleme-hızlı, tek bant akış döndür (öncelik HH>BH>EH>SH>HN).
    KRİTİK: SeedLink '?H?' aboneliği BH(20Hz)+HH(100Hz)+LH(1Hz)+VH(0.1Hz) bantlarını
    birlikte getiriyor; bunları tek akışa karıştırmak PhaseNet annotate'i bozuyor
    (14 km'deki M4'te bile maxP≈0.06). Tek bant seçince aynı veri maxP≈0.96 veriyor."""
    for b in ("HH", "BH", "EH", "SH", "HN"):
        sub = stream.select(channel=b + "?")
        if len(sub):
            return sub
    return stream

def analyze_streams():
    global pick_pool
    if model is None:  # model henüz yüklenmediyse bekle
        return
    current_time_utc = obspy.UTCDateTime()

    # Tüm istasyon akışlarının anlık kopyası
    with streams_lock:
        snapshot = {k: v.copy() for k, v in global_streams.items()}

    for key, st in snapshot.items():
        try:
            net, sta = key.split('.')
        except ValueError:
            continue
        st = _best_band(st)  # KARIŞIK bantları (BH/HH/LH/VH) ele -> tek bant (yoksa PhaseNet pick vermiyor)
        if len(st) < 3:
            continue

        end_t = st[-1].stats.endtime
        st_slice = st.slice(starttime=end_t - 60, endtime=end_t).copy()
        if len(st_slice) == 0:
            continue

        try:
            preds = model.annotate(st_slice)
            p_sel = preds.select(channel="*P")
            s_sel = preds.select(channel="*S")
            if len(p_sel) == 0 or len(s_sel) == 0:
                continue
            p_tr, s_tr = p_sel[0], s_sel[0]
        except Exception:
            continue

        p_prob = float(np.max(p_tr.data))
        s_prob = float(np.max(s_tr.data))

        new_picks = []
        if p_prob > PICK_THRESHOLD:
            p_idx = int(np.argmax(p_tr.data))
            p_time = p_tr.stats.starttime + p_idx * p_tr.stats.delta
            new_picks.append({"station": sta, "phase": "P", "time": p_time, "probability": p_prob})
        if s_prob > S_PICK_THRESHOLD:
            s_idx = int(np.argmax(s_tr.data))
            s_time = s_tr.stats.starttime + s_idx * s_tr.stats.delta
            new_picks.append({"station": sta, "phase": "S", "time": s_time, "probability": s_prob})

        if new_picks:
            with pool_lock:
                pick_pool.extend(new_picks)

    with pool_lock:
        # Havuz penceresi: GEOFON ~5 dk gecikmeli olduğu için 300s yetmez (pick'ler association'dan
        # önce düşerdi). POOL_RETENTION_SEC gecikmeyi + olay süresini kapsar; debounce çift-raporu önler.
        pick_pool = [p for p in pick_pool if current_time_utc - p["time"] < POOL_RETENTION_SEC]
        pool_copy = list(pick_pool)

    if len(pool_copy) < 3:
        return

    ref_t = min(p["time"] for p in pool_copy)  # min referans (offline/API ile aynı)
    for p in pool_copy:
        p["time_rel"] = p["time"] - ref_t

    pick_df = pd.DataFrame(pool_copy)
    pick_df["time"] = pick_df["time_rel"]

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            events, assignments = associator.associate(
                pick_df[["station", "phase", "time", "probability"]], station_df_km)
    except Exception as e:
        print(f"[PYOCTO HATA] {e}")
        return

    if events is None or len(events) == 0:
        return

    ev_df = pd.DataFrame(events)
    assignments = pd.DataFrame(assignments)

    for _, ev in ev_df.iterrows():
        orig_t_abs = ref_t + ev["time"]

        # Bu event'e atanmış pick'ler (istasyon sayısı debounce/iyileştirme kararı için gerekli)
        ev_idx = ev.get("idx", 0)
        ev_ass = assignments[assignments["event_idx"] == ev_idx] if len(assignments) > 0 else assignments
        assigned_rows = []
        for _, a in ev_ass.iterrows():
            pinfo = pick_df.iloc[int(a["pick_idx"])]
            assigned_rows.append({
                "station": pinfo["station"], "phase": pinfo["phase"],
                "time": float(pinfo["time"]), "probability": float(pinfo["probability"]),
                "t_abs": ref_t + float(pinfo["time"]),
            })
        assigned_df = pd.DataFrame(assigned_rows)
        _n_sta_now = len({_SITE_ALIAS.get(a["station"], a["station"]) for a in assigned_rows})

        # KATALOG EŞİĞİ: en az MIN_P_STATIONS ayrı sahada P VE en az MIN_S_PICKS adet S fazı.
        # Altında kalan çözümler (2 istasyon / S'siz) konum ve derinlik açısından kısıtlanamıyordu;
        # kataloğu şişirip güvenilirliği düşürüyorlardı -> hiç kaydedilmez, bildirilmez.
        _p_sites = {_SITE_ALIAS.get(a["station"], a["station"])
                    for a in assigned_rows if str(a["phase"]).upper() == "P"}
        _s_count = sum(1 for a in assigned_rows if str(a["phase"]).upper() == "S")
        if len(_p_sites) < MIN_P_STATIONS or _s_count < MIN_S_PICKS:
            print(f"[SKIP] {orig_t_abs.datetime.strftime('%Y%m%d_%H%M%S')}: "
                  f"P'li istasyon {len(_p_sites)}/{MIN_P_STATIONS}, S {_s_count}/{MIN_S_PICKS} "
                  f"-> eşik altı, kataloğa yazılmadı")
            continue

        # SİNYAL/GÜRÜLTÜ KAPISI: istasyonu SAYMADAN ÖNCE gerçekten bir şey kaydetmiş mi diye bak.
        # Ölçülemeyen (tampon pencereyi kapsamıyor) pick istasyonu cezalandırmaz.
        _p_first = {}
        for a in assigned_rows:
            if str(a["phase"]).upper() == "P":
                _s0 = a["station"]
                if _s0 not in _p_first or a["t_abs"] < _p_first[_s0]:
                    _p_first[_s0] = a["t_abs"]
        _clean, _snr_txt = set(), []
        for _s0, _pt in _p_first.items():
            _v = _pick_snr(_s0, _pt, "P", _pt)
            _snr_txt.append(f"{_s0}={'?' if _v is None else format(_v, '.1f')}")
            if _v is None or _v >= SNR_MIN:
                _clean.add(_SITE_ALIAS.get(_s0, _s0))
        if len(_clean) < MIN_CLEAN_P_STATIONS:
            print(f"[SKIP-SNR] {orig_t_abs.datetime.strftime('%Y%m%d_%H%M%S')}: "
                  f"temiz P istasyonu {len(_clean)}/{MIN_CLEAN_P_STATIONS} "
                  f"(SNR: {', '.join(_snr_txt)}, eşik {SNR_MIN}) -> gürültü, kataloğa yazılmadı")
            continue
        print(f"[SNR] {orig_t_abs.datetime.strftime('%Y%m%d_%H%M%S')}: "
              f"{', '.join(_snr_txt)} -> temiz istasyon {len(_clean)}")

        # DEBOUNCE + İYİLEŞTİRME (refinement):
        # Aynı olay (|Δt|<30 sn) daha önce raporlandıysa normalde atlanır. ANCAK bu kez
        # DAHA ÇOK istasyonla bulunduysa (tipik: ~5 dk gecikmeli FDSN istasyonlarının pick'leri
        # sonradan havuza düştü) olay YENİDEN konumlandırılıp katalog satırı GÜNCELLENİR.
        # Yeni bildirim gönderilmez (mükerrer uyarı olmasın). Dalga formu penceresinin tam
        # olması için olay en az REFINE_MIN_AGE_SEC yaşında olmalı; değilse sonraki turda bakılır.
        is_refine = False
        prior_proc_s = None
        prior_quality = None
        with reported_lock:
            prior = next((r for r in reported_events if abs(orig_t_abs - r["orig_t"]) < 30.0), None)
            if prior is not None:
                if _n_sta_now <= prior["n_sta"] or prior["refines"] >= REFINE_MAX_COUNT:
                    continue                      # yeni bilgi yok -> sessizce atla
                if float(obspy.UTCDateTime() - orig_t_abs) < REFINE_MIN_AGE_SEC:
                    continue                      # gecikmeli veri henüz tamamlanmadı; sonraki tur
                is_refine = True
                event_id = prior["event_id"]
                prior_proc_s = prior.get("proc_s")
                prior_quality = {"rms": prior.get("rms"), "erz": prior.get("erz"),
                                 "gap": prior.get("gap"), "source": prior.get("source")}
                print(f"[REFINE] {event_id}: {prior['n_sta']} -> {_n_sta_now} istasyon "
                      f"ile yeniden konumlandırılıyor")
                prior["n_sta"] = _n_sta_now
                prior["refines"] += 1
            else:
                event_id = f"LIVE_{orig_t_abs.datetime.strftime('%Y%m%d_%H%M%S')}"
                reported_events.append({"event_id": event_id, "orig_t": orig_t_abs,
                                        "n_sta": _n_sta_now, "refines": 0, "proc_s": None})
                del reported_events[:-500]        # sınırsız büyümeyi engelle

        # Kaba lokasyon (düzeltilmiş projeksiyon, UTM-36 -> lon/lat)
        coarse_lon, coarse_lat = to_lonlat(ev["x"], ev["y"])
        coarse_depth = float(ev["z"])

        # Hassas lokasyon: kataloglamadaki ile AYNI PyOcto+HypoInverse API'si (RMS dahil)
        api_loc = (get_location_from_api(assigned_df, trial=(coarse_lat, coarse_lon, coarse_depth))
                   if len(assigned_df) > 0 else None)

        # KALİTE BEKÇİSİ: gevşetilmiş eşikle gelen HypoInverse çözümü ancak makul sınırlar
        # içindeyse kabul edilir; aksi halde kaba PyOcto konumuna düşülür (eski davranış).
        # Az pick'li çözümler kararsız olabilir — asla mevcut durumdan kötüye gitmeyelim.
        if api_loc and api_loc.get("source") == "HypoInverse":
            _rej = []
            if api_loc.get("rms") is not None and api_loc["rms"] > 1.5:
                _rej.append(f"RMS {api_loc['rms']:.2f}s>1.5")
            _ad = float(api_loc.get("depth") or 0.0)
            if not (0.0 <= _ad <= 150.0):
                _rej.append(f"derinlik {_ad:.0f}km")
            # YÜZEYE YAPIŞMA: derinlik kısıtlanamadığında (tek S / büyük gap) çözüm 0 km
            # sınırına oturuyor. 0.02 km fiziksel bir derinlik değil, çözücü artefaktıdır.
            if _ad < 1.0:
                _rej.append(f"derinlik {_ad:.2f}km (yüzeye yapışmış)")
            # KABA ÇÖZÜMDEN KOPMA: derinlikte 50 km'yi aşan sıçrama, derin olaylarda
            # çözümün yanlış dala kaydığının işareti (133743: 79.7 km -> 0.02 km).
            if abs(_ad - float(coarse_depth or 0.0)) > 50.0:
                _rej.append(f"derinlik kabadan {abs(_ad - float(coarse_depth or 0.0)):.0f}km sapma")
            _dc = _hav_km(api_loc["lat"], api_loc["lon"], coarse_lat, coarse_lon)
            if _dc > 75.0:
                _rej.append(f"kaba konumdan {_dc:.0f}km sapma")
            if _rej:
                print(f"[GUARD] HypoInverse çözümü reddedildi ({'; '.join(_rej)}) -> PyOcto(coarse)")
                api_loc = None

        if api_loc:
            lat, lon, depth, source = api_loc["lat"], api_loc["lon"], api_loc["depth"], api_loc["source"]
            rms = api_loc.get("rms")
            erh, erz, gap = api_loc.get("erh"), api_loc.get("erz"), api_loc.get("gap")
        else:
            lat, lon, depth, source = coarse_lat, coarse_lon, coarse_depth, "PyOcto(coarse)"
            rms = None
            erh = erz = gap = None
        if gap is None:
            gap = _azimuthal_gap(lat, lon, [a["station"] for a in assigned_rows])

        # İYİLEŞTİRME GERİ ADIM ATMASIN: daha çok istasyon HER ZAMAN daha iyi çözüm demek
        # değil (yeni istasyonlar tek yönde toplanırsa gap büyür, derinlik bozulur).
        # Yeni çözüm öncekinden belirgin kötüyse katalog GÜNCELLENMEZ, eski çözüm korunur.
        if is_refine and prior_quality:
            _worse = []
            if prior_quality.get("source") == "HypoInverse" and source != "HypoInverse":
                _worse.append("hassas çözüm kaybedildi")
            _pr, _pz, _pg = (prior_quality.get(k) for k in ("rms", "erz", "gap"))
            if _pr is not None and rms is not None and rms > _pr + 0.5:
                _worse.append(f"RMS {_pr:.2f}->{rms:.2f}")
            if _pz is not None and erz is not None and erz > max(10.0, 2 * _pz):
                _worse.append(f"ERZ {_pz:.1f}->{erz:.1f} km")
            if _pg is not None and gap is not None and gap > _pg + 30:
                _worse.append(f"gap {_pg:.0f}->{gap:.0f}")
            if _worse:
                print(f"[REFINE] {event_id}: güncelleme ATLANDI ({'; '.join(_worse)}) "
                      f"-> önceki çözüm korunuyor")
                continue

        loc_name = reverse_geocode(lat, lon)

        # DALGA FORMU (origin -60s .. +180s) + magnitüd için WA genliği (int32'den ÖNCE, float üzerinde)
        save_start = orig_t_abs - 60
        save_end = orig_t_abs + 180
        pick_rows = []
        mseed_files = []
        wa_amps = {}
        for a in assigned_rows:
            sta_name = a["station"]
            # P dikeyden (Z), S ağırlıkla yataylardan (N/E) türetilir — bileşen etiketi buna göre.
            pick_comp = "HHZ" if str(a['phase']).upper() == "P" else "HHN"
            pick_rows.append(
                f"{event_id},{sta_name},{pick_comp},{a['phase']},{a['t_abs'].datetime.isoformat()},{a['probability']:.4f},PhaseNet,instance")
            with streams_lock:
                for k, kst in global_streams.items():
                    if k.endswith(f".{sta_name}"):
                        raw_slice = kst.slice(starttime=save_start, endtime=save_end).copy()
                        if len(raw_slice) > 0:
                            amp = _wa_amp_mm(raw_slice)  # Wood-Anderson genliği (mm)
                            if amp:
                                wa_amps[sta_name] = amp
                            save_st = raw_slice.copy()
                            for tr in save_st:
                                tr.data = np.require(tr.data, dtype=np.int32)
                            mseed_file = os.path.join(EVENTS_DIR, f"{event_id}_{sta_name}.mseed")
                            try:
                                save_st.write(mseed_file, format="MSEED", encoding="STEIM2")
                                mseed_files.append(mseed_file)
                            except Exception:
                                pass
        mseed_files = list(dict.fromkeys(mseed_files))  # tekilleştir

        # Magnitüd (Wood-Anderson ML)
        mag = _median_ml(wa_amps, lat, lon, depth)

        # Tekil SAHA sayısı: <3 ise olay yalnızca kataloğa kaydedilir, bildirim gönderilmez.
        # (_SITE_ALIAS: co-located istasyonlar tek saha sayılır; şu an eşleşme yok.)
        n_sta = len({_SITE_ALIAS.get(a["station"], a["station"]) for a in assigned_rows})
        n_p = sum(1 for a in assigned_rows if str(a["phase"]).upper() == "P")
        n_s = sum(1 for a in assigned_rows if str(a["phase"]).upper() == "S")
        # İşlem süresi = İLK tespitten bildirime kadar geçen süre. İyileştirme turunda
        # yeniden ölçülmez (yoksa ~8 dk görünüp tespit hızını yanlış temsil ederdi).
        proc_s = prior_proc_s if (is_refine and prior_proc_s is not None) \
            else int(round(float(obspy.UTCDateTime() - orig_t_abs)))
        # Gecikmeli istasyon kullanıldıysa toplam sürenin ~5 dk'sı VERİ TEMİNİ beklemesi OLABİLİR.
        # DİKKAT: yalnızca süre gerçekten gecikmeyi İÇERİYORSA düş. Olay hızlı istasyonlarla
        # zaten bulunmuş, gecikmeli istasyonlar sonradan (iyileştirme turunda) katılmışsa
        # bekleme yaşanmamıştır; o durumda çıkarma yapmak süreyi anlamsız kılıyordu
        # (ör. LIVE_20260804_204355: 75 sn'de bildirildi, 75-300 negatif -> tabana yapışıp "5 sn").
        used_delayed = bool({a["station"] for a in assigned_rows} & DELAYED_STATIONS)
        proc_net_s = (max(5, proc_s - DELAY_OFFSET_SEC)
                      if (used_delayed and proc_s > DELAY_OFFSET_SEC) else proc_s)
        # Çözüm kalitesini kayda al: bir sonraki iyileştirme turu bununla karşılaştırıp
        # geri adım atmayacak (ilk tespitte proc_s de burada saklanır).
        with reported_lock:
            for _r in reported_events:
                if _r["event_id"] == event_id:
                    if not is_refine:
                        _r["proc_s"] = proc_s
                    _r.update({"rms": rms, "erz": erz, "gap": gap, "source": source})
                    break

        m_txt = f"{mag:.1f}" if mag is not None else "?"
        r_txt = f"{rms:.2f}" if rms is not None else "?"
        _mode = ("İYİLEŞTİRME (bildirim yok)" if is_refine
                 else ("bildirim" if n_sta >= 3 else "sadece kayıt (<3 istasyon)"))
        print(f"[EVENT] {event_id} | {lat:.2f}N {lon:.2f}E ({loc_name}) | M {m_txt} | "
              f"Derinlik: {depth:.1f}km | RMS: {r_txt} | Kaynak: {source} | "
              f"İstasyon: {n_sta} (P:{n_p} S:{n_s}) | süre {proc_s}s"
              f"{f' (net {proc_net_s}s, gecikmeli veri)' if used_delayed else ''} -> {_mode}")

        # KATALOG (csv.writer: loc_name virgül içerebilir; kalite parametreleri dahil)
        _cat_row = [event_id, f"{lat:.4f}", f"{lon:.4f}", f"{depth:.2f}",
                    orig_t_abs.datetime.isoformat(), loc_name,
                    f"{mag:.1f}" if mag is not None else "",
                    f"{rms:.2f}" if rms is not None else "",
                    n_sta,
                    f"{erh:.1f}" if erh is not None else "",
                    f"{erz:.1f}" if erz is not None else "",
                    f"{gap:.0f}" if gap is not None else "",
                    n_p, n_s, proc_s, MODEL_STR, 1 if used_delayed else 0, proc_net_s,
                    "", "", "", ""]   # mw, mw_err, mw_n, fc -> spektral hesap sonrası doldurulur
        if is_refine:
            _replace_catalog_row(event_id, _cat_row)   # eski satırı yenisiyle DEĞİŞTİR
        else:
            with open(CATALOG_CSV, "a", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(_cat_row)

        if is_refine and pick_rows:
            _replace_pick_rows(event_id, pick_rows)    # bu olayın pick'lerini tazele
        elif pick_rows:
            with open(PICKS_CSV, "a", encoding="utf-8") as f:
                for r in pick_rows:
                    f.write(r + "\n")

        # GCS yükleme + Telegram (bildirim yalnız n_sta>=3 VE ilk tespitte; iyileştirmede
        # yalnız güncellenmiş katalog/dalga formları yüklenir, mükerrer bildirim gitmez)
        upload_and_notify(event_id, lat, lon, depth, orig_t_abs, loc_name,
                          [a["station"] for a in assigned_rows], mseed_files, mag, rms,
                          n_sta=n_sta, notify=not is_refine)

        # SPEKTRAL Mw — canlı akışı bloklamamak için AYRI İŞ PARÇACIĞINDA; sonuç gelince
        # katalog satırı güncellenir (bildirim tekrar gönderilmez).
        if compute_spectral_mw and mag is not None and mag >= MW_MIN_ML and mseed_files:
            _dists = [_hav_km(lat, lon, *_STA_LATLON[s])
                      for s in {a["station"] for a in assigned_rows} if s in _STA_LATLON]
            threading.Thread(
                target=_spectral_mw_job,
                args=(event_id, lat, lon, depth, orig_t_abs, list(mseed_files),
                      max(_dists) if _dists else 300.0, _cat_row),
                daemon=True).start()


def _station_inventory_file():
    """Cihaz yanıtlarını tek StationXML dosyasına yazar (SourceSpec bunu okur). Bir kez üretilir."""
    global _INV_PATH
    if _INV_PATH and os.path.exists(_INV_PATH):
        return _INV_PATH
    if station_inventory is None:
        return None
    try:
        p = os.path.join(tempfile.gettempdir(), "quakemlab_stations.xml")
        station_inventory.write(p, format="STATIONXML")
        _INV_PATH = p
        return p
    except Exception as e:
        print(f"[MW] StationXML yazılamadı: {str(e)[:80]}")
        return None


def _spectral_mw_job(event_id, lat, lon, depth, orig_t_abs, mseed_files, max_dist, cat_row):
    """Arka plan işi: spektral Mw hesapla, katalog satırının Mw alanlarını güncelle, GCS'e yükle."""
    try:
        inv = _station_inventory_file()
        if not inv:
            return
        res = compute_spectral_mw(event_id, lat, lon, depth, orig_t_abs, mseed_files, inv,
                                  max_dist_km=max_dist)
        if not res:
            return
        # KALİTE KURALI (2026-08-07, geçmiş veri geri-doldurmasında ölçüldü):
        #  - hata payı yok VEYA 0.00 -> ağırlıklı ortalama dejenere; tek spektrumdan gelmiş,
        #    kalitesi denetlenemez (LIVE_20260714_052312 böyle: Mw 3.57 ± 0.00)
        #  - fc bant dışı -> spektral uyum başarısız (LIVE_20260714_115633: fc 15.7 Hz,
        #    M3 için savunulamaz; bölgesel uzaklıkta o frekans zaten soğurulmuş olurdu)
        # Böyle sonuçlar kataloğa YAZILMAZ; olay Mw'siz kalır (ML zaten var).
        _fc = res.get("fc")
        _bad = None
        if not res.get("mw_err"):
            _bad = "hata payı yok (tek spektrum / dejenere ağırlık)"
        elif _fc is not None and not (0.1 <= float(_fc) <= 10.0):
            _bad = f"fc {_fc} bant dışı"
        if _bad:
            print(f"[MW] {event_id}: Mw {res.get('mw')} REDDEDİLDİ -> {_bad}")
            return
        row = list(cat_row)
        while len(row) < 22:
            row.append("")
        row[18] = f"{res['mw']:.2f}"
        row[19] = f"{res['mw_err']:.2f}" if res.get("mw_err") is not None else ""
        row[20] = res.get("mw_n") or ""
        row[21] = f"{res['fc']:.2f}" if res.get("fc") is not None else ""
        _replace_catalog_row(event_id, row)
        try:
            b = storage.Client().bucket(GCS_BUCKET_NAME).blob(f"{EVENTS_DIR}/{CATALOG_CSV}")
            b.cache_control = "no-cache, max-age=0"
            b.upload_from_filename(CATALOG_CSV)
        except Exception as e:
            print(f"[MW] GCS yükleme hatası: {str(e)[:80]}")
        _err = f" ±{res['mw_err']}" if res.get("mw_err") is not None else ""
        print(f"[MW] {event_id}: Mw {res['mw']}{_err} | fc {res.get('fc')} Hz | "
              f"{res.get('mw_n')} istasyon -> katalog güncellendi")
    except Exception as e:
        print(f"[MW] {event_id}: iş hatası {str(e)[:100]}")


def _replace_catalog_row(event_id, new_row):
    """Katalogdaki event_id satırını yenisiyle değiştirir (iyileştirme turu)."""
    try:
        with open(CATALOG_CSV, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        out, replaced = [], False
        for r in rows:
            if r and r[0] == event_id and not replaced:
                out.append(new_row); replaced = True
            else:
                out.append(r)
        if not replaced:
            out.append(new_row)
        with open(CATALOG_CSV, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(out)
    except Exception as e:
        print(f"[REFINE katalog HATA] {e}")


def _replace_pick_rows(event_id, pick_rows):
    """Bu olaya ait pick satırlarını siler, yeni (daha kapsamlı) listeyi yazar."""
    try:
        with open(PICKS_CSV, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        head = lines[0] if lines else "event_id,station,component,phase,pick_time,confidence,model,pretrained"
        keep = [ln for ln in lines[1:] if ln and not ln.startswith(f"{event_id},")]
        with open(PICKS_CSV, "w", encoding="utf-8") as f:
            f.write(head + "\n")
            for ln in keep + list(pick_rows):
                f.write(ln + "\n")
    except Exception as e:
        print(f"[REFINE pick HATA] {e}")


def upload_and_notify(event_id, lat, lon, depth, orig_t_abs, loc_name, sta_names, mseed_files,
                      mag=None, rms=None, n_sta=None, notify=True):
    download_links = []
    try:
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)

        # KRİTİK: bucket herkese açık olduğu için GCS, nesnelere varsayılan ~1 saatlik
        # önbellek uyguluyordu; API/app bayat katalog okuyup YENİ OLAYI GÖSTERMİYORDU.
        # Sürekli değişen bu iki dosya no-cache damgasıyla yüklenir.
        for _name in (CATALOG_CSV, PICKS_CSV):
            _b = bucket.blob(f"{EVENTS_DIR}/{_name}")
            _b.cache_control = "no-cache, max-age=0"
            _b.upload_from_filename(_name)

        # mseed'leri app.py'nin çekebileceği yola yükle
        for mf in mseed_files:
            blob = bucket.blob(f"{EVENTS_DIR}/mseed/{os.path.basename(mf)}")
            blob.upload_from_filename(mf)

        # ZIP
        if mseed_files:
            zip_filename = os.path.join(EVENTS_DIR, f"{event_id}.zip")
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for mf in mseed_files:
                    zipf.write(mf, os.path.basename(mf))
            bucket.blob(f"{EVENTS_DIR}/zip/{event_id}.zip").upload_from_filename(zip_filename)
            download_links.append(
                f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{EVENTS_DIR}/zip/{event_id}.zip")
    except Exception as e:
        print(f"[GCS HATA] {e}")

    # İyileştirme turu: dosyalar GCS'e yüklendi, bildirim gönderilmez (mükerrer uyarı olmasın).
    if not notify:
        print(f"[REFINE] {event_id}: güncellenmiş çözüm GCS'e yüklendi (bildirim yok)")
        return
    # Telegram — yalnız n_sta>=3 olan olaylar bildirilir; azı sadece kataloğa kaydedilir.
    if n_sta is not None and n_sta < 3:
        print(f"[TELEGRAM] atlandı: {event_id} yalnız {n_sta} istasyonla tespit (<3) — sadece kayıt")
        return
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN != "BURAYA_TOKEN_YAZIN":
        try:
            sta_str = ", ".join(sorted(set(sta_names)))
            # Toplam işlem süresi: deprem origin'inden bildirim gönderilene kadar (dalga yolu + buffer + analiz + API + magnitüd + upload)
            _elapsed = int(round(float(obspy.UTCDateTime() - orig_t_abs)))
            _elapsed_txt = f"{_elapsed} sn" if _elapsed < 60 else f"{_elapsed // 60} dk {_elapsed % 60} sn"
            # Katalog sayfasında DOĞRUDAN bu event'in canlı sayfasına yönlendir (?event=...)
            _detail_link = f"{WEB_APP_URL}{'&' if '?' in WEB_APP_URL else '?'}event={event_id}"
            msg = "🚨 *YENİ DEPREM TESPİT EDİLDİ!* 🚨\n\n"
            msg += f"🌍 *Bölge:* {BOLGE_ADI}\n"
            msg += f"📍 *Konum:* {lat:.2f}°N, {lon:.2f}°E ({loc_name})\n"
            if mag is not None:
                msg += f"📐 *Magnitüd (ML):* {mag:.1f}\n"
            msg += f"📏 *Derinlik:* {depth:.1f} km\n"
            if rms is not None:
                msg += f"📊 *RMS:* {rms:.2f} s\n"
            # TR saati (UTC+3, Türkiye'de yaz/kış saati yok) — UTC parantez içinde.
            _tr_dt = (orig_t_abs + 3 * 3600).datetime
            msg += (f"⏰ *Zaman (TR):* {_tr_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(UTC: {orig_t_abs.datetime.strftime('%H:%M:%S')})\n")
            msg += f"⏱️ *Toplam işlem süresi:* {_elapsed_txt}  _(deprem → bildirim)_\n"
            msg += f"📡 *Algılayan İstasyonlar:* {sta_str}\n"
            if download_links:
                msg += "\n📥 *Veri İndirme Linki:*\n"
                for link in set(download_links):
                    msg += f"• [Tüm Verileri (ZIP) İndir]({link})\n"
            msg += f"\n🔍 *Detaylı İncele:*\n{_detail_link}\n"
            msg += f"🆔 *Event ID:* `{event_id}`"

            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload, timeout=30)
            if r.status_code != 200:
                print(f"[TELEGRAM] {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[TELEGRAM HATA] {e}")

    # X (Twitter) bildirimi — Telegram ile aynı kural: n_sta<3 olaylar yukarıda zaten elendi.
    # 280 karakter sınırı için kompakt biçim. Link varsayılan KAPALI (linkli tweet $0.20,
    # linksiz $0.015; site linki hesabın bio'sunda) — X_TWEET_LINK=1 ile açılır.
    try:
        _tr_dt = (orig_t_abs + 3 * 3600).datetime
        tw = ["QuakeMLab Automatic Event",
              f"{_tr_dt.strftime('%d.%m.%Y %H:%M:%S')} TSİ"]
        _l = f"M {mag:.1f}" if mag is not None else "M —"
        tw.append(f"{_l} · Depth {depth:.1f} km")
        tw.append(f"{lat:.2f}°N, {lon:.2f}°E")
        if rms is not None:
            tw.append(f"RMS: {rms:.2f} s")
        if X_TWEET_LINK:
            tw.append(f"{WEB_APP_URL}{'&' if '?' in WEB_APP_URL else '?'}event={event_id}")
        post_to_x("\n".join(tw))
    except Exception as e:
        print(f"[X HATA] {e}")


def _sl_negotiate(sock, cfgs):
    """Ham SeedLink v3 TEK connection / ÇOK istasyon aboneliği:
    HELLO, ardından her istasyon için STATION/SELECT(3 bileşen)/DATA, en sonda TEK END.
    Tek sunucudaki tüm istasyonlar tek bir bağlantı üzerinden çekilir (AFAD'ın 16 Eyl 2026
    geri bildirimi: istasyon başına ayrı connection açmak yerine tek connection kullanılmalı).
    DİKKAT: 'TIME' (dial-back) GÖNDERİLMEZ — GEOFON v4 sunucusu onu reddediyor (obspy'ın hatası).
    Dönen: abonelik OK dönen cfg listesi (boşsa hiçbiri kabul edilmedi)."""
    def cmd(c):
        sock.sendall((c + "\r\n").encode())
        return sock.recv(256).decode("latin1", "ignore").strip()
    cmd("HELLO")
    accepted = []
    for cfg in cfgs:
        net, sta = cfg["net"], cfg["sta"]
        if cmd(f"STATION {sta} {net}") != "OK":
            print(f"[SL {net}.{sta}] STATION reddedildi (bu IP için abonelik yok / istasyon kapalı)")
            continue
        if not all(cmd(f"SELECT {sel}") == "OK" for sel in ("HHZ", "HHN", "HHE")):
            print(f"[SL {net}.{sta}] SELECT reddedildi")
            continue
        if cmd("DATA") != "OK":   # gerçek-zaman (TIME değil!)
            print(f"[SL {net}.{sta}] DATA reddedildi")
            continue
        accepted.append(cfg)
    if accepted:
        sock.sendall(b"END\r\n")  # çok-istasyon negotiate'i bitir -> akış başlar
    return accepted


def _sl_stream_once(cfgs):
    """Bir SUNUCUDAKİ istasyon grubunu TEK connection ile stream'ler; kopana/hataya kadar BLOKLAR.
    cfgs: aynı (host, port)'a bağlı cfg listesi. 'SL'+512B miniSEED çerçevelerini ayrıştırıp
    global_streams'e besler (paketler network.station'a göre yönlendirilir). Dönen: ingest edilen
    kayıt sayısı (0 => bağlantı/negotiate hiç veri vermeden başarısız oldu)."""
    host, port = cfgs[0]["sl"]
    n = 0
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=15)
        sock.settimeout(SL_SOCK_TIMEOUT)
        if not _sl_negotiate(sock, cfgs):
            return 0
        buf = b""
        while True:
            data = sock.recv(4096)
            if not data:
                break  # sunucu bağlantıyı kapattı
            buf += data
            while len(buf) >= SL_FRAME:
                if buf[:2] != b"SL":              # hizadan çıktıysak bir sonraki 'SL'e senkronla
                    idx = buf.find(b"SL", 1)
                    if idx < 0:
                        buf = buf[-1:]
                        break
                    buf = buf[idx:]
                    continue
                rec = buf[8:SL_FRAME]             # 8 bayt SL başlığını at, 512B kaydı al
                buf = buf[SL_FRAME:]
                try:
                    st = obspy.read(io.BytesIO(rec))
                    k = f"{st[0].stats.network}.{st[0].stats.station}"
                    if _ingest_stream(k, st) is not None:
                        n += 1
                        maybe_analyze()
                except Exception:
                    pass  # INFO/keepalive paketleri veya bozuk kayıt -> atla
    except Exception as e:
        if n == 0:
            print(f"[SL {host}:{port}] {type(e).__name__}: {str(e)[:80]}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return n


def _http_waveforms(prov, net, sta, t_start, t_end):
    """Ham HTTP FDSN dataselect (obspy Client'ın kırılgan servis keşfi olmadan).
    Veri yoksa None; HTTP/parse hatasında istisna fırlatır."""
    base = FDSN_BASE.get(prov)
    if not base:
        raise RuntimeError(f"bilinmeyen FDSN sağlayıcı: {prov}")
    r = requests.get(f"{base}/fdsnws/dataselect/1/query",
                     params={"net": net, "sta": sta, "loc": "*", "cha": "HH?",
                             "start": str(t_start), "end": str(t_end), "nodata": "204"},
                     timeout=60)
    if r.status_code == 204 or not r.content:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return obspy.read(io.BytesIO(r.content))


def _fdsn_fetch_once(cfg, client):
    """FDSN dataselect çekişi (SeedLink yedeği VEYA sadece-FDSN istasyonu; ~5 dk gecikmeli).
    Dönen: client (geriye uyumluluk için; HTTP yolunda kullanılmıyor)."""
    net, sta, prov = cfg["net"], cfg["sta"], cfg["fdsn"]
    key = f"{net}.{sta}"
    try:
        t_end = obspy.UTCDateTime() - FDSN_END_LAG
        prev = _fdsn_last_end.get(key)
        # İlk çekişte pencere: SADECE-FDSN istasyonlarında daha geniş (fdsn_window), çünkü
        # verinin son ~5 dk'sı henüz yayında değil; dar pencere yalnız birkaç saniye getirirdi.
        _win = cfg.get("fdsn_window", FETCH_WINDOW)
        t_start = (prev - FDSN_OVERLAP) if prev is not None else (t_end - _win)
        if t_end - t_start >= 1.0:
            st = _http_waveforms(prov, net, sta, t_start, t_end)
            new_end = _ingest_stream(key, st)
            if new_end is not None:
                _fdsn_last_end[key] = new_end
                maybe_analyze()
    except Exception as e:
        en = type(e).__name__
        if "NoData" not in en and "No data" not in str(e):
            print(f"[FDSN {prov} {key}] {en}: {str(e)[:80]}")
    return client


def run_fdsn_only(cfg):
    """SADECE-FDSN istasyonu (SeedLink'i halka kapalı): periyodik dataselect yoklaması."""
    net, sta = cfg["net"], cfg["sta"]
    print(f"[ACQ] {net}.{sta} başladı — SADECE FDSN {cfg['fdsn']} (~5 dk gecikmeli, {FDSN_POLL_SEC}s yoklama)")
    fdsn_client = None
    while True:
        fdsn_client = _fdsn_fetch_once(cfg, fdsn_client)
        time.sleep(FDSN_POLL_SEC)


def run_server_group(cfgs):
    """Bir SeedLink SUNUCUSUNDAKİ istasyon grubunu TEK connection ile toplar; kopunca yeniden bağlanır.
    (AFAD 16 Eyl 2026 geri bildirimi: istasyon başına ayrı connection yerine sunucu başına tek
    connection.) SeedLink üst üste SL_MAX_FAILS kez veri vermezse gruptaki FDSN yedeği olan
    istasyonlar dataselect ile beslenir (kör kalma); bu sırada SeedLink denenmeye devam eder."""
    host, port = cfgs[0]["sl"]
    names = ", ".join(f"{c['net']}.{c['sta']}" for c in cfgs)
    print(f"[ACQ] {host}:{port} grubu başladı — {len(cfgs)} istasyon: {names}")
    sl_fails = 0
    fdsn_clients = {}
    while True:
        n = _sl_stream_once(cfgs)   # sağlıklıysa uzun süre bloklar
        if n > 0:
            sl_fails = 0            # veri aktı -> sağlıklı; kısa bekleyip yeniden bağlan
        else:
            sl_fails += 1
            if sl_fails == SL_MAX_FAILS:
                print(f"[ACQ {host}:{port}] SeedLink {sl_fails}x başarısız -> FDSN yedeğine düşülüyor")
            if sl_fails >= SL_MAX_FAILS:
                for cfg in cfgs:
                    if cfg.get("fdsn"):
                        k = f"{cfg['net']}.{cfg['sta']}"
                        fdsn_clients[k] = _fdsn_fetch_once(cfg, fdsn_clients.get(k))
        time.sleep(SL_RECONNECT_SEC)


def heartbeat():
    """Her HEARTBEAT_INTERVAL'de istasyon başına buffer uzunluğu + gerçek-zaman gecikmesini (lag) loglar.
    Amaç: veri akışı durursa (istasyon eksik / lag çok yüksek) log'dan ANINDA görülsün — worker daha önce
    iki kez SESSİZCE kör kaldı (derece-projeksiyon bug'ı ve GEOFON SeedLink v4 geçişi). Association için
    en az 3 taze istasyon gerekir; onun altına düşünce satır WARN ile işaretlenir."""
    expected = [f"{c['net']}.{c['sta']}" for c in STATIONS]
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            now = obspy.UTCDateTime()
            with streams_lock:
                snap = {k: (v[-1].stats.endtime, v[0].stats.starttime) if len(v) else None
                        for k, v in global_streams.items()}
            # ASCII-only log (konsol/encoding'den bağımsız güvenilir olsun; grep'lenebilir)
            parts, alive, sta_status = [], 0, []
            for key in expected:
                info = snap.get(key)
                if not info:
                    parts.append(f"{key}: NO-DATA")
                    sta_status.append({"station": key, "fresh": False, "lagSec": None})
                    continue
                end_t, start_t = info
                buf = float(end_t - start_t)
                lag = float(now - end_t)
                _fresh = lag <= STALE_LAG_SEC
                if _fresh:
                    alive += 1
                    parts.append(f"{key}: {buf:.0f}s buf, lag {lag:.0f}s")
                else:
                    parts.append(f"{key}: {buf:.0f}s buf, lag {lag:.0f}s STALE")
                sta_status.append({"station": key, "fresh": _fresh, "lagSec": round(lag)})
            with pool_lock:
                npool = len(pick_pool)
            status = "OK" if alive >= 3 else "WARN-LOW-STATIONS"
            print(f"[HEARTBEAT] {status} ({alive}/{len(expected)} fresh) | " +
                  " | ".join(parts) + f" | pool={npool}")
            _publish_status(now, alive, len(expected), sta_status, npool)
        except Exception as e:
            print(f"[HEARTBEAT HATA] {e}")


def _publish_status(now, alive, total, sta_status, npool):
    """İstasyon sağlık durumunu GCS'e yazar (ana sayfadaki 'aktif istasyon' sayacı bunu okur).
    no-cache: sayaç her zaman güncel görünsün."""
    try:
        payload = {
            "updatedAt": now.datetime.replace(microsecond=0).isoformat() + "Z",
            "stationsActive": alive, "stationsTotal": total,
            "poolPicks": npool, "stations": sta_status,
        }
        b = storage.Client().bucket(GCS_BUCKET_NAME).blob(f"{EVENTS_DIR}/STATUS.json")
        b.cache_control = "no-cache, max-age=0"
        b.upload_from_string(json.dumps(payload), content_type="application/json")
    except Exception as e:
        print(f"[STATUS yükleme HATA] {str(e)[:80]}")


def _seed_catalog_from_gcs():
    """Restart'ta GCS'teki mevcut LIVE_CATALOG *ve* LIVE_PICKS'i indir -> geçmiş tespitler,
    pick'ler ve manuel yayınlanan event'ler kaybolmasın. (Fresh container yerel dosyaları
    sıfırdan başlatıp ilk upload'da GCS'i siliyordu; katalog korunuyordu ama pick'ler değil ->
    app.py canlı event listesini pick'lerden kurduğu için restart sonrası tek event görünüyordu.)"""
    try:
        from google.cloud import storage
        bucket = storage.Client().bucket(GCS_BUCKET_NAME)

        # 1) Katalog (loc_name'siz eski başlığı gerekiyorsa migre et)
        blob = bucket.blob(f"{EVENTS_DIR}/{CATALOG_CSV}")
        if blob.exists():
            blob.download_to_filename(CATALOG_CSV)
            with open(CATALOG_CSV, "r", encoding="utf-8") as f:
                _lines = f.read().splitlines()
            if _lines and _lines[0].strip() != _CATALOG_HEADER:  # eski başlığı migre et
                _data = [ln for ln in _lines[1:] if ln.strip()]
                with open(CATALOG_CSV, "w", encoding="utf-8") as f:
                    f.write(_CATALOG_HEADER + "\n")
                    for ln in _data:
                        f.write(ln + "\n")
            n = max(0, sum(1 for _ in open(CATALOG_CSV, encoding="utf-8")) - 1)
            print(f"[CATALOG] GCS'ten {n} mevcut kayıt yüklendi (geçmiş korunuyor)")

            # Debounce/iyileştirme kaydını katalogdan doldur: restart sonrası aynı olay
            # yeniden tespit edilirse mükerrer satır yazılmasın (son 200 olay yeterli).
            try:
                with open(CATALOG_CSV, "r", encoding="utf-8", newline="") as f:
                    _rows = list(csv.DictReader(f))
                for _r in _rows[-200:]:
                    try:
                        _ns = int(float(_r.get("n_sta") or 0))
                    except Exception:
                        _ns = 0
                    reported_events.append({
                        "event_id": _r["event_id"],
                        "orig_t": obspy.UTCDateTime(_r["origin_time"]),
                        "n_sta": _ns, "refines": REFINE_MAX_COUNT, "proc_s": None,
                    })  # refines=MAX: eski olaylar yeniden konumlandırılmaz, sadece debounce
                print(f"[CATALOG] {len(reported_events)} olay debounce kaydına alındı")
            except Exception as e:
                print(f"[CATALOG debounce seed HATA] {e}")

        # 2) Pick'ler — restart'ta sıfırlanıp ilk upload'da GCS pick geçmişini SİLİYORDU.
        #    GCS'teki mevcut LIVE_PICKS'i indir ki tüm geçmiş canlı event'ler app'te listelenebilsin.
        blob_p = bucket.blob(f"{EVENTS_DIR}/{PICKS_CSV}")
        if blob_p.exists():
            blob_p.download_to_filename(PICKS_CSV)
            np = max(0, sum(1 for _ in open(PICKS_CSV, encoding="utf-8")) - 1)
            print(f"[PICKS] GCS'ten {np} mevcut pick yüklendi (geçmiş korunuyor)")
    except Exception as e:
        print(f"[CATALOG/PICKS seed HATA] {e}")


def main():
    global model
    print("Live AI Worker Başlatıldı...")
    config.ensure_catalog_files()  # CWD'de PICKS/CATALOG CSV'lerini hazırla (import yan etkisi değil)
    if model is None:
        print("PhaseNet modeli yükleniyor (instance)...")
        model = sbm.PhaseNet.from_pretrained("instance")
        print("Model yüklendi!")
    _seed_catalog_from_gcs()  # geçmiş kataloğu koru (restart'ta sıfırlanmasın)
    # Magnitüd için cihaz yanıtlarını FDSN'den ARKA PLANDA çek (streaming'i bloklamasın/askıya almasın)
    threading.Thread(target=fetch_response_inventory, daemon=True).start()
    print(f"UTM-36 projeksiyon aktif | Arama alanı (km) X:{XLIM} Y:{YLIM}")
    print("Veri kaynağı: ham SeedLink v3 (birincil, ~10s) + FDSN yedek/sadece-FDSN (~5dk) | İstasyonlar: " +
          ", ".join(f"{c['net']}.{c['sta']}@{c['sl'][0] if c.get('sl') else 'FDSN:' + c['fdsn']}"
                    for c in STATIONS))
    # İstasyonları SUNUCUYA GÖRE grupla: aynı (host, port) tek connection ile çekilir
    # (AFAD geri bildirimi). SeedLink'i halka kapalı (sl=None) istasyonlar ayrı FDSN thread'i.
    sl_groups = {}
    fdsn_only = []
    for cfg in STATIONS:
        if cfg.get("sl") is None:
            fdsn_only.append(cfg)
        else:
            sl_groups.setdefault(tuple(cfg["sl"]), []).append(cfg)
    threads = []
    for cfgs in sl_groups.values():
        t = threading.Thread(target=run_server_group, args=(cfgs,), daemon=True)
        t.start()
        threads.append(t)
    for cfg in fdsn_only:
        t = threading.Thread(target=run_fdsn_only, args=(cfg,), daemon=True)
        t.start()
        threads.append(t)
    print(f"[ACQ] {len(sl_groups)} SeedLink connection + {len(fdsn_only)} FDSN-only thread")

    # Akış sağlığı heartbeat'i (sessiz körlüğü görünür kılar) — her HEARTBEAT_INTERVAL'de loglar.
    threading.Thread(target=heartbeat, daemon=True).start()

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
