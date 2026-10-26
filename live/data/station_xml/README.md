# İstasyon cihaz yanıtları (StationXML)

Bir bölgenin istasyonları için **açık FDSN station servisi yoksa** (ör. AFAD),
ML/Mw hesabı için gereken cihaz yanıtları bu klasöre bölge bazında konur:
`data/station_xml/<bolge>/<STA>.xml`.

Örnek bölge **Ege** için buna gerek yoktur: yanıtlar GEOFON/IRIS/INGV FDSN'den
otomatik çekilir (`region.yaml` içindeki `response_source: fdsn`).
