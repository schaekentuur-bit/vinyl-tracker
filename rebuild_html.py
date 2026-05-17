"""
Regenereert docs/index.html vanuit cache — geen scraping nodig.
"""
import os
from datetime import datetime
import vinyl_tracker
from vinyl_tracker import (
    load_cache, build_html, compute_new_listings, cache_is_fresh,
    import_collection,
    RELEASES, USER_RELEASES_FILE, SALES_CACHE, STATS_CACHE, LISTINGS_CACHE,
)

for rid, val in load_cache(USER_RELEASES_FILE).items():
    if rid not in RELEASES:
        RELEASES[rid] = tuple(val)

today = datetime.now().strftime("%Y-%m-%d")
sales_cache    = load_cache(SALES_CACHE)
stats_cache    = load_cache(STATS_CACHE)
listings_cache = load_cache(LISTINGS_CACHE)

results = []
for release_id, (group, title) in vinyl_tracker.RELEASES.items():
    sc  = sales_cache.get(release_id, {})
    lc  = listings_cache.get(release_id, {})
    sc_stats = stats_cache.get(release_id, {})
    results.append({
        "id":       release_id,
        "group":    group,
        "title":    title,
        "sales":    sc.get("sales", []) if cache_is_fresh(sc) else [],
        "stats":    sc_stats.get("stats") or {},
        "listings": lc.get("listings", []) if cache_is_fresh(lc, max_days=7) else [],
    })

print(f"{len(results)} releases geladen uit cache")
new_listings = compute_new_listings(results)
print(f"{len(new_listings)} nieuwe listings")
collection = import_collection()
print(f"{len(collection)} collectie-items")
html = build_html(results, static=True, new_listings=new_listings, collection=collection)
os.makedirs("docs", exist_ok=True)
with open("docs/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"docs/index.html aangemaakt ({len(html):,} tekens)")
