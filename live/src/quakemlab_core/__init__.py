"""QuakeMLab canlı sismik tespit çekirdeği.

Modüller:
  config      — bölge YAML yükleme + türetilmiş nesneler/sabitler
  engine      — gerçek-zamanlı tespit motoru (edinim→pikleme→ilişkilendirme→lokasyon→magnitüd→katalog→bildirim)
  geocode     — saf coğrafya yardımcıları (mesafe, azimut boşluğu, ters coğrafkodlama)
  magnitude   — (engine içinde) Wood-Anderson ML
  spectral_mw — SourceSpec tabanlı spektral moment magnitüdü
  notify      — X (Twitter) bildirimi
"""
__version__ = "0.1.0"
