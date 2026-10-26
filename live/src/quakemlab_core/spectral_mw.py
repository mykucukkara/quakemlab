# -*- coding: utf-8 -*-
"""Spektral moment magnitüd (Mw) — SourceSpec sarmalayıcısı.

Zincir: PhaseNet pick -> faz eşleştirme -> HypoInverse/PyOcto lokasyon -> DALGA FORMU KESİTİ
        -> cihaz yanıtı kaldırma -> SourceSpec -> M0, Mw, fc

Neden ayrı modül: canlı akışı bloklamasın diye worker bunu OLAY KAYDEDİLDİKTEN SONRA
ayrı bir iş parçacığında çağırır; sonuç gelince katalog satırı güncellenir.

Doğrulama (2026-08-06): LIVE_20260804_204355 (139 km derin, 9 istasyon) -> Mw 3.59±0.09,
NOA'nın MLh 3.58 değeriyle 0.01 fark. 683556 (AFAD MW 5.0) -> Mw 4.81±0.18.
"""
import os
import re
import shutil
import subprocess
import tempfile

# Kaynak bölgesi elastik parametreleri: derinliğe göre kabuk / manto.
# (M0 ∝ ρ·v³ olduğu için derin olayda kabuk hızı kullanmak M0'ı ~2.4 kat küçültür.)
_CRUST = dict(vp=6.0, vs=3.5, rho=2700)
_MANTLE = dict(vp=8.0, vs=4.5, rho=3300)
_MOHO_KM = 35.0

_CONF_CACHE = None   # üretilen taban config yolu (bir kez üretilir)


def _base_config(workroot):
    """SourceSpec örnek config'ini bir kez üretip önbelleğe alır."""
    global _CONF_CACHE
    if _CONF_CACHE and os.path.exists(_CONF_CACHE):
        return _CONF_CACHE
    d = os.path.join(workroot, "_conf")
    os.makedirs(d, exist_ok=True)
    subprocess.run(["python", "-m", "sourcespec.source_spec", "-S"],
                   cwd=d, capture_output=True, timeout=120)
    p = os.path.join(d, "source_spec.conf")
    _CONF_CACHE = p if os.path.exists(p) else None
    return _CONF_CACHE


def _write_config(dst, inventory_ref, depth_km, max_dist_km):
    """Taban config'i olaya göre uyarlar. inventory_ref: çalışma dizinine GÖRELİ yol
    (mutlak yol verildiğinde SourceSpec envanteri boş okuyabiliyor)."""
    base = _CONF_CACHE
    if not base:
        return False
    txt = open(base, encoding="utf-8").read()
    el = _MANTLE if depth_km and depth_km > _MOHO_KM else _CRUST
    sets = {
        "station_metadata": inventory_ref,
        "vp_source": f"{el['vp']}", "vs_source": f"{el['vs']}", "rho_source": f"{el['rho']}",
        "win_length": "20.0",
        "epi_dist_ranges": f"0, {int(max(150, max_dist_km + 50))}",
        # KRİTİK: varsayılan 0.5 Hz alt sınırı M>3 olayların düşük frekans platosunu keser
        # (683556'da Mw 4.55 -> 4.81 farkı bundandı). 0.05 Hz'e indiriyoruz.
        "bp_freqmin_broadb": "0.05", "freq1_broadb": "0.05",
        "bp_freqmin_disp": "0.05", "freq1_disp": "0.05",
        "plot_show": "False", "plot_save": "False", "html_report": "False",
    }
    for k, v in sets.items():
        # DİKKAT: değiştirme metnini lambda ile ver — yol içindeki ters bölü (Windows) veya
        # \g gibi diziler re.sub tarafından kaçış olarak yorumlanıp hata veriyordu.
        txt = re.sub(rf"^{k}\s*=.*$", lambda _m, _k=k, _v=v: f"{_k} = {_v}", txt,
                     count=1, flags=re.M)
    open(dst, "w", encoding="utf-8").write(txt)
    return True


_NUM = r"([-\d.eE+]+)"


def _parse_out(path):
    """SourceSpec .ssp.out özetinden Mw / hata / fc / M0 okur (ağırlıklı değerler tercih)."""
    if not os.path.exists(path):
        return None
    txt = open(path, encoding="utf-8", errors="ignore").read()

    def grab(key, weighted=True):
        # NOT: ters bölü f-string İFADESİ içinde olamaz (Python < 3.12 sözdizimi hatası;
        # konteyner 3.10 çalıştırıyor). Bu yüzden desen parçası ayrı değişkende tutulur.
        suffix = r" \(weighted\)" if weighted else ""
        pat = rf"^{key}{suffix}:\s*{_NUM}"
        m = re.search(pat, txt, re.M)
        return float(m.group(1)) if m else None

    mw = grab("Mw") or grab("Mw", False)
    if mw is None:
        return None
    m_err = re.search(r"^Mw \(weighted\):\s*[-\d.]+\s*\+/-\s*([\d.]+)", txt, re.M) \
        or re.search(r"^Mw:\s*[-\d.]+\s*\+/-\s*([\d.]+)", txt, re.M)
    return {
        "mw": round(mw, 2),
        "mw_err": round(float(m_err.group(1)), 2) if m_err else None,
        "fc": grab("fc") or grab("fc", False),
        "m0": grab("Mo") or grab("Mo", False),
    }


def compute_spectral_mw(event_id, lat, lon, depth_km, origin_utc, mseed_files,
                        inventory_path, max_dist_km=300.0, workroot=None, timeout=600):
    """Olay için spektral Mw hesaplar. Başarısızsa None döner (asla istisna fırlatmaz).

    mseed_files    : olayın istasyon bazlı dalga formu dosyaları (worker zaten kaydediyor)
    inventory_path : tüm canlı istasyonların cihaz yanıtını içeren StationXML dosyası
    """
    if not mseed_files or not inventory_path or not os.path.exists(inventory_path):
        return None
    workroot = workroot or os.path.join(tempfile.gettempdir(), "sspec")
    os.makedirs(workroot, exist_ok=True)
    if not _base_config(workroot):
        print("[MW] SourceSpec taban config üretilemedi")
        return None

    d = tempfile.mkdtemp(prefix=f"{event_id}_", dir=workroot)
    try:
        wf = os.path.join(d, "wf")
        os.makedirs(wf, exist_ok=True)
        for f in mseed_files:
            try:
                shutil.copy(f, wf)
            except Exception:
                pass
        if not os.listdir(wf):
            return None

        # Cihaz yanıtı: çalışma dizinine kopyala ve GÖRELİ dizin olarak ver (doğrulanmış biçim)
        meta = os.path.join(d, "meta")
        os.makedirs(meta, exist_ok=True)
        shutil.copy(inventory_path, os.path.join(meta, "stations.xml"))

        ot = origin_utc.datetime.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with open(os.path.join(d, "ev.yaml"), "w", encoding="utf-8") as f:
            f.write(f"""- event_id: '{event_id}'
  hypocenter:
    longitude: {{value: {lon:.4f}, units: deg}}
    latitude: {{value: {lat:.4f}, units: deg}}
    depth: {{value: {depth_km:.2f}, units: km}}
    origin_time: '{ot}'
""")
        conf = os.path.join(d, "source_spec.conf")
        if not _write_config(conf, "meta", depth_km, max_dist_km):
            return None

        r = subprocess.run(["python", "-m", "sourcespec.source_spec",
                            "-c", "source_spec.conf", "-H", "ev.yaml", "-t", "wf"],
                           cwd=d, capture_output=True, text=True, timeout=timeout)
        out_dir = os.path.join(d, "sspec_out", event_id)
        res = _parse_out(os.path.join(out_dir, f"{event_id}.ssp.out"))
        if res is None:
            tail = ((r.stdout or "") + (r.stderr or ""))[-200:].replace("\n", " ")
            print(f"[MW] {event_id}: sonuç üretilemedi ({tail})")
            return None
        # Spektrum üreten istasyon sayısı (istasyon başına 'Mw' satırları)
        try:
            txt = open(os.path.join(out_dir, f"{event_id}.ssp.out"), encoding="utf-8",
                       errors="ignore").read()
            # İstasyon satırları: "GE.APE..HHH broadb   Mw  3.744  fc ..." (bazen *Mw)
            res["mw_n"] = len(set(re.findall(r"^(\w+\.\w+)\S*\s+\w+\s+\*?Mw\s+[\d.]+",
                                             txt, re.M))) or None
        except Exception:
            res["mw_n"] = None
        return res
    except subprocess.TimeoutExpired:
        print(f"[MW] {event_id}: zaman aşımı ({timeout} sn)")
        return None
    except Exception as e:
        print(f"[MW] {event_id}: hata {str(e)[:100]}")
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)
