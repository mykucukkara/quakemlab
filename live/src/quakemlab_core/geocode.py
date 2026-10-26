"""Saf coğrafya yardımcıları — global durum yok, kolay test edilir.
hav_km: iki nokta arası büyük-çember mesafesi (km).
azimuthal_gap: episantr etrafındaki en büyük istasyon azimut boşluğu (derece).
reverse_geocode: koordinattan yer adı (Nominatim); bulunamazsa 'Bilinmiyor'.
"""
import math

from .config import _STA_LATLON


def hav_km(la1, lo1, la2, lo2):
    R = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def azimuthal_gap(ev_lat, ev_lon, sta_names):
    """Episantrdan pick veren istasyonlara azimutları hesaplayıp en büyük boşluğu (derece) döner.
    HypoInverse gap veremediğinde (PyOcto-coarse fallback) kullanılır."""
    azs = []
    for s in set(sta_names):
        ll = _STA_LATLON.get(s)
        if not ll:
            continue
        dlon = math.radians(ll[1] - ev_lon)
        la1, la2 = math.radians(ev_lat), math.radians(ll[0])
        y = math.sin(dlon) * math.cos(la2)
        x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
        azs.append((math.degrees(math.atan2(y, x)) + 360.0) % 360.0)
    if len(azs) < 2:
        return None
    azs.sort()
    gaps = [azs[i + 1] - azs[i] for i in range(len(azs) - 1)] + [360.0 - azs[-1] + azs[0]]
    return round(max(gaps), 1)


def reverse_geocode(lat, lon):
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="quakecortex_bot")
        info = geolocator.reverse(f"{lat}, {lon}", exactly_one=True, language="tr")
        if info and "address" in info.raw:
            addr = info.raw["address"]
            name = addr.get("city", addr.get("town", addr.get("village",
                    addr.get("province", addr.get("state", "Bilinmiyor")))))
            if "country" in addr:
                name += f", {addr['country']}"
            return name
    except Exception:
        pass
    return "Bilinmiyor"
