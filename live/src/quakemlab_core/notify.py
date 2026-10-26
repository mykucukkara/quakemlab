"""Dış bildirim kanalları. Telegram gönderimi motor içindeki upload_and_notify'da
gömülüdür (olay meta verisiyle sıkı bağlı); burada X (Twitter) gönderimi bulunur.
Anahtarlar eksikse sessizce atlanır; hiçbir bildirim hatası ana akışı etkilemez.
"""
import requests

from .config import (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)


def post_to_x(text):
    """X'e (Twitter) tweet atar — API v2 POST /2/tweets, OAuth 1.0a kullanıcı bağlamı.
    Anahtarlar eksikse sessizce atlanır; hata ana bildirim akışını etkilemez.
    NOT: Free tier yazma limiti (≈17 tweet/24sa, ~500/ay) aşılırsa 429 döner — loglanır, olay
    yine katalog+Telegram'da olur."""
    if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        return
    try:
        from requests_oauthlib import OAuth1
        auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
        r = requests.post("https://api.x.com/2/tweets", json={"text": text}, auth=auth, timeout=30)
        if r.status_code in (200, 201):
            print("[X] tweet gönderildi")
        else:
            print(f"[X] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[X HATA] {e}")
