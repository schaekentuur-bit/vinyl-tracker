"""
GitHub Actions entrypoint.
Scrapet Discogs, genereert docs/index.html en stuurt email bij nieuwe deals.
"""
import os, base64, tempfile

# Cookies uit GitHub Secret decoderen naar tijdelijk bestand
_cookies_b64 = os.getenv("DISCOGS_COOKIES_B64", "").lstrip("﻿").strip()
if _cookies_b64:
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                       delete=False, encoding="utf-8")
    _tmp.write(base64.b64decode(_cookies_b64).decode("utf-8"))
    _tmp.close()
    os.environ["_COOKIES_FILE_OVERRIDE"] = _tmp.name

import vinyl_tracker

# Overschrijf cookies pad als die via env var meegegeven is
if os.getenv("_COOKIES_FILE_OVERRIDE"):
    vinyl_tracker.COOKIES_FILE = os.getenv("_COOKIES_FILE_OVERRIDE")

from curl_cffi import requests as cf_requests
from vinyl_tracker import (
    scrape_all, build_html, compute_deals,
    send_deals_email, find_new_deals,
    compute_new_listings, enrich_listing_dates,
    load_cache, save_cache,
    DEALS_SEEN_FILE, LISTINGS_SEEN_FILE, _deal_key, RELEASES, USER_RELEASES_FILE
)

# Eerder toegevoegde releases laden
for rid, val in load_cache(USER_RELEASES_FILE).items():
    if rid not in RELEASES:
        RELEASES[rid] = tuple(val)

cookies = vinyl_tracker.load_cookies()
session = cf_requests.Session(impersonate="chrome124")
session.headers.update({"Accept-Language": "nl-BE,nl;q=0.9"})

# Verwerk expliciete mark-as-read keys (vanuit workflow dispatch via telefoon)
_mark_keys_raw = os.getenv("MARK_READ_KEYS", "").strip()
if _mark_keys_raw:
    import json as _json
    try:
        _mark_keys = _json.loads(_mark_keys_raw)
        if _mark_keys:
            _seen = load_cache(LISTINGS_SEEN_FILE)
            _today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
            for _k in _mark_keys:
                _seen[_k] = _today
            save_cache(LISTINGS_SEEN_FILE, _seen)
            print(f"{len(_mark_keys)} listings gemarkeerd als gelezen")
    except Exception as _e:
        print(f"MARK_READ_KEYS parse fout: {_e}")

force = os.getenv("FORCE_REFRESH", "false").strip().lower() == "true"
print(f"Scrapen... (force_listings=True, force_stats={force})")
results = scrape_all(cookies, session, force_listings=True, force_stats=force)
print(f"{len(results)} releases verwerkt")

print("Plaatsingsdatums ophalen voor beleggingsplaten...")
enrich_listing_dates(results)

new_listings = compute_new_listings(results)
print(f"{len(new_listings)} nieuwe listings gevonden")

html = build_html(results, static=True, new_listings=new_listings)

os.makedirs("docs", exist_ok=True)
with open("docs/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("docs/index.html aangemaakt")

# Email bij nieuwe deals
deals = compute_deals(results)
seen  = load_cache(DEALS_SEEN_FILE)
if not seen:
    send_deals_email(deals, subject_prefix="Alle actieve deals")
else:
    new = find_new_deals(deals, seen)
    send_deals_email(new, subject_prefix="Nieuwe deals")

save_cache(DEALS_SEEN_FILE, {_deal_key(d): {
    "price":     d["best"]["price"],
    "currency":  d["best"]["currency"],
    "shipping":  d["best"].get("shipping", 0.0),
    "total_eur": d["best"].get("total_eur", d["best"]["price"]),
    "disc":      d["disc"],
    "tier":      d.get("tier", "beste"),
    "seller":    d["best"]["seller"],
    "title":     d["r"]["title"],
    "group":     d["r"]["group"],
} for d in deals})
