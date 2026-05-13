"""
Vinyl Price Tracker — Echte verkoopprijzen per conditie via Discogs
Haalt de laatste verkopen per staat (NM, VG+, VG, ...) op en toont dit in HTML.
Toont ook de beste huidige listing per staat (verkopers met >= 50 ratings).

Vereisten:
    pip install requests curl-cffi

Setup:
    1. Log in op discogs.com in Chrome/Edge
    2. Installeer extensie "Get cookies.txt LOCALLY"
    3. Exporteer cookies van discogs.com → sla op als cookies_www.discogs.com.txt
       in dezelfde map als dit script
    4. Draai: python vinyl_tracker.py
"""

import re
import json
import os
import time
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from curl_cffi import requests as cf_requests
import requests as std_requests

# ─── CONFIGURATIE ─────────────────────────────────────────────────────────────

DISCOGS_TOKEN      = os.getenv("DISCOGS_TOKEN", "")
COOKIES_FILE       = "cookies_www.discogs.com.txt"
SALES_CACHE        = "vinyl_sales_cache.json"
STATS_CACHE        = "vinyl_history_cache.json"
LISTINGS_CACHE     = "vinyl_listings_cache.json"
DEALS_SEEN_FILE    = "vinyl_deals_seen.json"
CACHE_DAYS         = 7   # verkoopdata na X dagen opnieuw ophalen
MIN_SELLER_RATINGS = 50  # minimaal aantal ratings voor een verkoper
PORT               = 8765
USER_RELEASES_FILE = "user_releases.json"

EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO   = os.getenv("EMAIL_TO",   "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")

# Lokale overrides uit config.py (staat in .gitignore, nooit op GitHub)
try:
    import config as _cfg
    DISCOGS_TOKEN = DISCOGS_TOKEN or getattr(_cfg, "DISCOGS_TOKEN", "")
    EMAIL_FROM    = EMAIL_FROM    or getattr(_cfg, "EMAIL_FROM",    "")
    EMAIL_TO      = EMAIL_TO      or getattr(_cfg, "EMAIL_TO",      "")
    EMAIL_PASS    = EMAIL_PASS    or getattr(_cfg, "EMAIL_PASS",    "")
except ImportError:
    pass

RELEASES = {
    # ── OASIS ──
    "939519":   ("Oasis", "Morning Glory (CRE LP 189, Damont, UK 1995)"),
    "517224":   ("Oasis", "Definitely Maybe (CRE LP 169, Damont, UK 1994)"),
    "6127871":  ("Oasis", "Morning Glory (RKIDLP73, EU reissue 2014)"),
    "33663000": ("Oasis", "Time Flies RSD Box Set (RKIDLP150RSD, 2025)"),
    "12864584": ("Oasis", "Definitely Maybe reissue (RKIDLP70, 2014)"),

    # ── RED HOT CHILI PEPPERS ──
    "375491":   ("RHCP", "Blood Sugar Sex Magik (7599-26681-1, EU 1991)"),
    "12042641": ("RHCP", "Blood Sugar Sex Magik (468348-1, US 2012 remaster)"),
    "9899740":  ("RHCP", "Californication (9362-47386-1, EU 1999)"),
    "31323387": ("RHCP", "Californication 25th Anniversary (93624843276, 2024)"),
    "420718":   ("RHCP", "By The Way (9 48140-1, US 2002)"),
    "15276024": ("RHCP", "By The Way reissue (093624814016, EU 2020)"),
    "1629020":  ("RHCP", "Stadium Arcadium (49996-1, US 2006)"),
    "8519678":  ("RHCP", "Stadium Arcadium reissue (9362-44391-1, EU 2016)"),

    # ── THE BEATLES ──
    "7801798":  ("Beatles", "Abbey Road (PCS 7088, UK first pressing 1969)"),
    "14186441": ("Beatles", "Abbey Road 50th Anniversary (0602577915123, 2019)"),

    # ── MICHAEL JACKSON ──
    "2911293":  ("Michael Jackson", "Thriller (QE 38112, Pitman, US 1982)"),
    "459606":   ("Michael Jackson", "Bad (E 40600, Carrollton, US 1987)"),

    # ── METALLICA ──
    "1549636":  ("Metallica", "Master of Puppets (60439-1, Allied, US 1986)"),
    "381988":   ("Metallica", "Black Album (61113-1, Elektra, US 1991)"),
    "439599":   ("Metallica", "Black Album (510 022-1, Vertigo, EU 1991)"),

    # ── QUEEN ──
    "612780":   ("Queen", "A Night at the Opera (EMTC 103, UK 1975)"),
    "7541569":  ("Queen", "A Night at the Opera half-speed (00602547202697, EU 2015)"),

    # ── AC/DC ──
    "400591":   ("AC/DC", "Back in Black (APLP-046, Australisch origineel 1980)"),
    "400587":   ("AC/DC", "Highway to Hell (APLP-040, Australisch origineel 1979)"),

    # ── LINKIN PARK ──
    "534020":   ("Linkin Park", "Hybrid Theory (9 47755-1, US 2001)"),
    "3336797":  ("Linkin Park", "Meteora (48186-1, US 2003)"),

    # ── GREEN DAY ──
    "1203470":  ("Green Day", "American Idiot (9362-48777-1, EU 2004)"),

    # ── GUNS N' ROSES ──
    "383777":   ("Guns N' Roses", "Appetite for Destruction (XXXG 24148, US Allied 1987)"),
    "1238431":  ("Guns N' Roses", "Appetite for Destruction (924 148-1, EU uncensored 1987)"),
    "7492229":  ("Guns N' Roses", "Appetite for Destruction (00720642414811, reissue EU 2015)"),

    # ── NOTORIOUS B.I.G. ──
    "317356":   ("Notorious B.I.G.", "Ready to Die (78612-73000-1, US 1994)"),

    # ── KENDRICK LAMAR ──
    "3975953":  ("Kendrick Lamar", "Good Kid M.A.A.D City (B0017695-01, US 2012)"),

    # ── THE KILLERS ──
    "397167":   ("The Killers", "Hot Fuss (LIZARD011X, blue marbled, UK 2004)"),

    # ── DOE MAAR ──
    "402227":   ("Doe Maar", "Skunk (Kil 19934 Kl, NL 1981)"),
    "382601":   ("Doe Maar", "Doris Day En Andere Stukken (Kil 21032 Kl, NL 1982)"),

    # ── OVERIGE ──
    "12895130": ("Overige", "Flatbush Zombies - Vacation in Hell (clear/black smoke, 2018)"),
    "13672908": ("Overige", "Beast Coast - Escape From New York (blue, 2019)"),
    "223127":   ("Overige", "Mobb Deep - The Infamous (07863 66480-1, US 1995)"),
    "16170729": ("Overige", "Sticks - Stickmatic (350 405-3, NL 2020)"),
}

# ─── GENRE CATEGORISATIE ──────────────────────────────────────────────────────

GROUP_GENRES = {
    "Oasis":            "Rock",
    "RHCP":             "Rock",
    "Beatles":          "Rock",
    "Queen":            "Rock",
    "Green Day":        "Rock",
    "The Killers":      "Rock",
    "AC/DC":            "Hard Rock / Metal",
    "Guns N' Roses":    "Hard Rock / Metal",
    "Metallica":        "Hard Rock / Metal",
    "Linkin Park":      "Hard Rock / Metal",
    "Michael Jackson":  "Pop",
    "Doe Maar":         "Pop",
    "Notorious B.I.G.": "Hip-Hop",
    "Kendrick Lamar":   "Hip-Hop",
}
GENRE_ORDER = ["Rock", "Hard Rock / Metal", "Pop", "Hip-Hop"]

# ─── CONDITIES ────────────────────────────────────────────────────────────────

CONDITION_MAP = {
    "Mint (M)":              "M",
    "Near Mint (NM or M-)":  "NM",
    "Very Good Plus (VG+)":  "VG+",
    "Very Good (VG)":        "VG",
    "Good Plus (G+)":        "G+",
    "Good (G)":              "G",
    "Fair (F)":              "F",
    "Poor (P)":              "P",
}
CONDITION_ORDER = ["M", "NM", "VG+", "VG", "G+", "G", "F", "P"]

CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}

# ─── COOKIES ──────────────────────────────────────────────────────────────────

def load_cookies():
    cookies = {}
    with open(COOKIES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies

# ─── SCRAPEN VERKOOPHISTORIE ──────────────────────────────────────────────────

def parse_history_html(html):
    pattern = re.compile(
        r'sales-history-row[^"]*"[^>]*>.*?'
        r'data-header="Order Date:">\s*([\d\-]+)\s*</td>.*?'
        r'data-header="Media:">\s*([^<]+?)\s*</td>.*?'
        r'data-header="Sleeve:">\s*([^<]+?)\s*</td>.*?'
        r'class="price">\s*[€£\$\s]*([\d\.,]+)',
        re.DOTALL
    )
    sales = []
    for m in pattern.finditer(html):
        date, media_raw, sleeve_raw, price_str = m.groups()
        media  = CONDITION_MAP.get(media_raw.strip(),  media_raw.strip())
        sleeve = CONDITION_MAP.get(sleeve_raw.strip(), sleeve_raw.strip())
        try:
            price = float(price_str.replace(",", ""))
        except ValueError:
            continue
        sales.append({"date": date.strip(), "media": media, "sleeve": sleeve, "price": price})
    return sales

def scrape_history(release_id, cookies, session):
    url = f"https://www.discogs.com/sell/history/{release_id}"
    try:
        r = session.get(url, cookies=cookies, timeout=25,
                        headers={"Accept-Language": "nl-BE,nl;q=0.9"})
        if r.status_code == 200:
            sales = parse_history_html(r.text)
            return sales
        print(f"  HTTP {r.status_code}")
        return []
    except Exception as e:
        print(f"  Fout: {e}")
        return []

# ─── SCRAPEN MARKETPLACE LISTINGS ─────────────────────────────────────────────

def parse_listings_html(html):
    listings = []
    # Split into per-listing blocks at each shortcut_navigable row
    blocks = re.split(r'(?=<tr[^>]+class="[^"]*shortcut_navigable)', html)
    for block in blocks[1:]:
        # Skip unavailable listings
        head = block[:120]
        if "unavailable" in head:
            continue

        # Media condition — only look at the part of item_condition BEFORE the sleeve span
        # (the paragraph contains both media and sleeve text; iterating CONDITION_MAP on the
        # full paragraph can accidentally match the sleeve's condition as the media condition)
        ic_match = re.search(r'class="item_condition"[^>]*>(.*?)</p>', block, re.DOTALL)
        if not ic_match:
            continue
        ic_content = ic_match.group(1)
        sleeve_pos = ic_content.find('item_sleeve_condition')
        media_section = ic_content[:sleeve_pos] if sleeve_pos > 0 else ic_content
        media_text = re.sub(r'<[^>]+>', ' ', media_section)
        media = None
        for full_name, code in CONDITION_MAP.items():
            if full_name in media_text:
                media = code
                break
        if not media:
            continue

        # Sleeve condition
        sc_match = re.search(r'class="item_sleeve_condition"[^>]*>\s*([^<]+?)\s*<', block)
        sleeve_raw = sc_match.group(1).strip() if sc_match else "Generic"
        sleeve = CONDITION_MAP.get(sleeve_raw, sleeve_raw)

        # Currency and price from data attributes (unquoted in Discogs HTML)
        cur_match = re.search(r'data-currency=([A-Z]+)\s+data-pricevalue=([\d.]+)', block)
        if not cur_match:
            continue
        currency = cur_match.group(1)
        try:
            price = float(cur_match.group(2))
        except ValueError:
            continue

        # Seller rating count
        ratings_match = re.search(
            r'class="section_link"[^>]*>\s*([\d,]+)\s*ratings',
            block, re.IGNORECASE
        )
        if not ratings_match:
            continue
        try:
            rating_count = int(ratings_match.group(1).replace(",", ""))
        except ValueError:
            continue

        # Seller name from star_rating alt attribute
        seller_match = re.search(r'class="star_rating"\s+alt="([^"]+?)\s+rating\s+[\d.]', block)
        seller = seller_match.group(1).strip() if seller_match else "?"

        listings.append({
            "media":        media,
            "sleeve":       sleeve,
            "price":        price,
            "currency":     currency,
            "rating_count": rating_count,
            "seller":       seller,
        })

    return listings


def get_best_listings(listings):
    """Cheapest listing per condition from sellers with >= MIN_SELLER_RATINGS."""
    best = {}
    for listing in listings:
        if listing["rating_count"] < MIN_SELLER_RATINGS:
            continue
        cond = listing["media"]
        if cond not in best or listing["price"] < best[cond]["price"]:
            best[cond] = listing
    return best


def scrape_listings(release_id, cookies, session):
    url = f"https://www.discogs.com/sell/release/{release_id}"
    try:
        r = session.get(
            url, cookies=cookies, timeout=25,
            params={"sort": "price,asc", "limit": 50},
        )
        if r.status_code == 200:
            return parse_listings_html(r.text)
        print(f"  Listings HTTP {r.status_code}")
        return []
    except Exception as e:
        print(f"  Listings fout: {e}")
        return []

# ─── CACHE ────────────────────────────────────────────────────────────────────

def load_cache(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def cache_is_fresh(entry, max_days=CACHE_DAYS):
    try:
        fetched = datetime.strptime(entry.get("fetched_at", ""), "%Y-%m-%d")
        return (datetime.now() - fetched).days < max_days
    except Exception:
        return False

# ─── DISCOGS API (marktstatistieken) ─────────────────────────────────────────

DISCOGS_HEADERS = {
    "Authorization": f"Discogs token={DISCOGS_TOKEN}",
    "User-Agent": "VinylTrackerAI/2.0 +schaekentuur@gmail.com"
}

def get_market_stats(release_id):
    try:
        r = std_requests.get(
            f"https://api.discogs.com/marketplace/stats/{release_id}",
            headers=DISCOGS_HEADERS, timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

def scrape_listings_api(release_id):
    """Listings ophalen via officiële Discogs API — werkt ook vanuit GitHub Actions."""
    try:
        r = std_requests.get(
            "https://api.discogs.com/marketplace/search",
            headers=DISCOGS_HEADERS,
            params={
                "release_id": release_id,
                "status": "For Sale",
                "sort": "price",
                "sort_order": "asc",
                "per_page": 50,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Listings API fout: {e}")
        return []

    listings = []
    for item in data.get("listings", []):
        media  = CONDITION_MAP.get(item.get("condition", ""),        item.get("condition", ""))
        sleeve = CONDITION_MAP.get(item.get("sleeve_condition", ""), item.get("sleeve_condition", "Generic"))
        price_obj    = item.get("price", {})
        price        = float(price_obj.get("value", 0))
        currency     = price_obj.get("currency", "EUR")
        seller_obj   = item.get("seller", {})
        seller       = seller_obj.get("username", "?")
        rating_count = int(seller_obj.get("stats", {}).get("total", 0))
        if price > 0:
            listings.append({
                "media": media, "sleeve": sleeve,
                "price": price, "currency": currency,
                "rating_count": rating_count, "seller": seller,
            })
    return listings

# ─── HTML OPBOUW ──────────────────────────────────────────────────────────────

GROUP_COLORS = [
    "#ddeeff", "#ddfff0", "#fff5dd", "#ffe8e8", "#f0ddff",
    "#ddfffd", "#fff0dd", "#e8ffe8", "#ffe8f5", "#eaeaea",
]

def fmt(val):
    if val is None:
        return "—"
    return f"€ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_listing_price(price, currency):
    sym = CURRENCY_SYMBOLS.get(currency, currency + " ")
    formatted = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sym} {formatted}"

def _gid(group):
    """Sanitize group name to a safe HTML id."""
    return "grp-" + re.sub(r'[^a-z0-9]+', '-', group.lower()).strip('-')

def _build_release_cards(group_results):
    """Build the detailed condition cards for a list of releases."""
    cards = ""
    for r in group_results:
        sales = r["sales"]
        stats = r["stats"]
        lowest = stats.get("lowest_price", {})
        lowest_val = lowest.get("value") if isinstance(lowest, dict) else lowest
        best_for_release = get_best_listings(r.get("listings", []))

        by_cond = {}
        for s in sales:
            by_cond.setdefault(s["media"], []).append(s)
        for cond in by_cond:
            by_cond[cond].sort(key=lambda x: x["date"], reverse=True)

        cond_blocks = ""
        for cond in CONDITION_ORDER:
            cond_sales = by_cond.get(cond, [])[:10]
            if not cond_sales:
                continue
            prices = [s["price"] for s in cond_sales]
            avg = sum(prices) / len(prices)
            mn  = min(prices)
            mx  = max(prices)
            rows = "".join(
                f'<tr>'
                f'<td>{s["date"]}</td>'
                f'<td><span class="badge bd-{s["media"].replace("+","p").replace("-","m")}">{s["media"]}</span></td>'
                f'<td><span class="badge bd-{s["sleeve"].replace("+","p").replace("-","m")}">{s["sleeve"]}</span></td>'
                f'<td style="text-align:right;font-weight:bold">{fmt(s["price"])}</td>'
                f'</tr>'
                for s in cond_sales
            )
            best = best_for_release.get(cond)
            if best:
                mc = best["media"].replace("+","p").replace("-","m")
                sc = best["sleeve"].replace("+","p").replace("-","m")
                lhref = f"https://www.discogs.com/sell/release/{r['id']}?sort=price%2Casc&limit=50"
                best_html = (
                    f'<div class="best-listing">'
                    f'<span class="best-label">Beste listing:</span> '
                    f'<strong>{fmt_listing_price(best["price"], best["currency"])}</strong>'
                    f' &mdash; {best["seller"]} ({best["rating_count"]:,} ratings)'
                    f' | Disc: <span class="badge bd-{mc}">{best["media"]}</span>'
                    f' Hoes: <span class="badge bd-{sc}">{best["sleeve"]}</span>'
                    f' | <a href="{lhref}" target="_blank">Koop &rarr;</a>'
                    f'</div>'
                )
            else:
                best_html = ""

            cond_blocks += f"""
            <div class="cb">
              <div class="cb-head">{cond} <span class="cb-n">({len(cond_sales)} verkopen)</span></div>
              <div class="cb-stats">Gem {fmt(avg)} &nbsp;|&nbsp; Min {fmt(mn)} &nbsp;|&nbsp; Max {fmt(mx)}</div>
              {best_html}
              <table>
                <tr><th>Datum</th><th>Disc</th><th>Hoes</th><th>Prijs</th></tr>
                {rows}
              </table>
            </div>"""

        if not cond_blocks:
            cond_blocks = '<p class="no-data">Geen verkoopdata op Discogs.</p>'

        market = (
            f'Laagste nu: <strong>{fmt(lowest_val)}</strong> &nbsp;|&nbsp; '
            f'{stats.get("num_for_sale","?")} te koop &nbsp;&nbsp;'
            f'<a href="https://www.discogs.com/sell/release/{r["id"]}" target="_blank">Listings &rarr;</a>'
            f' &nbsp;<a href="https://www.discogs.com/sell/history/{r["id"]}" target="_blank">Historie &rarr;</a>'
        )
        cards += f"""
        <div class="rb">
          <div class="rb-head">
            <span class="rb-title">{r["title"]}</span>
          </div>
          <p class="market">{market}</p>
          <div class="conds">{cond_blocks}</div>
        </div>"""
    return cards

def compute_deals(results):
    """Bereken top deals: listings goedkoper dan laagste historische verkoop."""
    deals = []
    for r in results:
        best_for_release = get_best_listings(r.get("listings", []))
        by_cond = {}
        for s in r["sales"]:
            by_cond.setdefault(s["media"], []).append(s)
        for cond, best in best_for_release.items():
            cond_sales = by_cond.get(cond, [])
            if not cond_sales:
                continue
            mn   = min(s["price"] for s in cond_sales)
            disc = (mn - best["price"]) / mn * 100
            if disc > 0:
                deals.append({"r": r, "cond": cond, "best": best, "mn": mn, "disc": disc})
    deals.sort(key=lambda x: x["disc"], reverse=True)
    return deals


def _deal_key(d):
    return f"{d['r']['id']}_{d['cond']}"


def find_new_deals(deals, seen):
    """Geeft deals terug die nieuw zijn of waarvan de prijs >3% gedaald is."""
    new = []
    for d in deals:
        key = _deal_key(d)
        if key not in seen:
            d = dict(d); d["tag"] = "NIEUW"
            new.append(d)
        elif d["best"]["price"] < seen[key]["price"] * 0.97:
            d = dict(d); d["tag"] = "GOEDKOPER"; d["prev_price"] = seen[key]["price"]
            new.append(d)
    return new


def send_deals_email(deals, subject_prefix="Nieuwe deals"):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not deals:
        return

    subject = f"Vinyl Tracker — {subject_prefix} ({len(deals)})"
    rows = ""
    for d in deals:
        b     = d["best"]
        tag   = d.get("tag", "NIEUW")
        color = "#10B981" if tag == "NIEUW" else "#F59E0B"
        prev  = (f' <span style="color:#94A3B8;font-size:11px">was {fmt(d["prev_price"])}</span>'
                 if "prev_price" in d else "")
        lhref = (f"https://www.discogs.com/sell/release/{d['r']['id']}"
                 f"?sort=price%2Casc&limit=50")
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;white-space:nowrap">
            <span style="background:{color};color:#fff;font-size:10px;font-weight:700;
                         padding:2px 7px;border-radius:4px;letter-spacing:.3px">{tag}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-size:12px;color:#64748B;white-space:nowrap">{d["r"]["group"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;font-size:13px">{d["r"]["title"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-weight:600;white-space:nowrap">{d["cond"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-weight:700;white-space:nowrap">
            {fmt_listing_price(b["price"], b["currency"])}{prev}
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     color:#065F46;font-weight:700;white-space:nowrap">-{d["disc"]:.0f}%</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-size:12px;color:#64748B">{b["seller"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9">
            <a href="{lhref}" style="color:#3B82F6;text-decoration:none;
               font-size:12px;font-weight:500">Kopen →</a>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
             background:#F1F5F9;margin:0;padding:24px">
  <div style="max-width:800px;margin:0 auto">
    <div style="background:#0F2245;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0">
      <h1 style="margin:0;font-size:18px;font-weight:700">&#9679; Vinyl Tracker</h1>
      <p style="margin:6px 0 0;font-size:13px;opacity:.7">{subject}</p>
    </div>
    <div style="background:#fff;border-radius:0 0 10px 10px;
                border:1px solid #E2E8F0;border-top:none;overflow:hidden">
      <table style="border-collapse:collapse;width:100%">
        <thead>
          <tr style="background:#F8FAFC">
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0"></th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0">Artiest</th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0">Release</th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0">Staat</th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0">Prijs</th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0">Korting</th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0">Verkoper</th>
            <th style="padding:9px 12px;border-bottom:1px solid #E2E8F0"></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    <p style="font-size:11px;color:#94A3B8;margin-top:12px;text-align:center">
      Vinyl Tracker &mdash; {datetime.now().strftime("%d/%m/%Y %H:%M")}
    </p>
  </div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        import smtplib as _smtp
        with _smtp.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASS)
            smtp.send_message(msg)
        print(f"Email verstuurd: {subject}")
    except Exception as e:
        print(f"Email fout: {e}")


def build_html(results, static=False):
    now    = datetime.now().strftime("%d/%m/%Y %H:%M")
    groups = list(dict.fromkeys(r["group"] for r in results))

    # ── Top Deals berekening (nodig voor home stats) ───────────────────────
    deals = compute_deals(results)

    # ── Per-artiest pagina's ───────────────────────────────────────────────
    artist_pages = ""
    for group in groups:
        gresult = [r for r in results if r["group"] == group]
        cards   = _build_release_cards(gresult)
        artist_pages += f"""
        <div class="page" id="{_gid(group)}" style="display:none">
          <div class="page-header">
            <h2>{group}</h2>
            <span class="sub">{len(gresult)} release(s)</span>
          </div>
          {cards}
        </div>"""

    # ── Home-overzicht pagina ──────────────────────────────────────────────
    releases_with_listings = sum(1 for r in results if r.get("listings"))
    home_rows = ""
    for r in results:
        best_for_release = get_best_listings(r.get("listings", []))
        by_cond = {}
        for s in r["sales"]:
            by_cond.setdefault(s["media"], []).append(s)
        cond_badges = " ".join(
            f'<span class="badge bd-{c.replace("+","p").replace("-","m")}">{c}</span>'
            for c in CONDITION_ORDER if c in by_cond
        ) or '<span class="muted">—</span>'
        all_best = [v for v in best_for_release.values()]
        cheapest = min(all_best, key=lambda x: x["price"]) if all_best else None
        listing_cell = fmt_listing_price(cheapest["price"], cheapest["currency"]) if cheapest else "—"
        stats = r["stats"]
        gid   = _gid(r["group"])
        home_rows += (
            f'<tr onclick="showPage(\'{gid}\')" class="home-row">'
            f'<td><span class="rb-group">{r["group"]}</span></td>'
            f'<td class="td-title">{r["title"]}</td>'
            f'<td>{cond_badges}</td>'
            f'<td class="td-num">{listing_cell}</td>'
            f'<td class="td-num">{stats.get("num_for_sale","?")}</td>'
            f'</tr>'
        )
    home_page = f"""
    <div class="page" id="home">
      <div class="page-header">
        <h2>Vinyl Overzicht</h2>
        <span class="sub">{now} &nbsp;&middot;&nbsp; {len(results)} releases &nbsp;&middot;&nbsp; klik op een rij voor details</span>
      </div>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-val">{len(results)}</div>
          <div class="stat-lbl">Releases</div>
        </div>
        <div class="stat-card">
          <div class="stat-val">{len(groups)}</div>
          <div class="stat-lbl">Artiesten</div>
        </div>
        <div class="stat-card">
          <div class="stat-val">{releases_with_listings}</div>
          <div class="stat-lbl">Met listings</div>
        </div>
        <div class="stat-card stat-card-accent">
          <div class="stat-val">{len(deals)}</div>
          <div class="stat-lbl">Actieve deals</div>
        </div>
      </div>
      <div class="card">
        <table class="ov-table">
          <thead><tr>
            <th>Artiest</th><th>Release</th><th>Staten</th>
            <th class="th-r">Beste listing</th><th class="th-r">Te koop</th>
          </tr></thead>
          <tbody>{home_rows}</tbody>
        </table>
      </div>
    </div>"""

    # ── Top Deals pagina ───────────────────────────────────────────────────
    deal_rows = ""
    for d in deals[:30]:
        d = dict(d)
        b  = d["best"]
        r  = d["r"]
        mc = b["media"].replace("+","p").replace("-","m")
        sc = b["sleeve"].replace("+","p").replace("-","m")
        cc = d["cond"].replace("+","p").replace("-","m")
        lhref = f"https://www.discogs.com/sell/release/{r['id']}?sort=price%2Casc&limit=50"
        deal_rows += (
            f'<tr onclick="showPage(\'{_gid(r["group"])}\')" class="home-row">'
            f'<td><span class="rb-group">{r["group"]}</span></td>'
            f'<td class="td-title">{r["title"]}</td>'
            f'<td><span class="badge bd-{cc}">{d["cond"]}</span></td>'
            f'<td class="td-num"><strong>{fmt_listing_price(b["price"], b["currency"])}</strong></td>'
            f'<td class="td-num">{fmt(d["mn"])}</td>'
            f'<td class="td-num"><span class="deal-pct">-{d["disc"]:.0f}%</span></td>'
            f'<td class="td-seller">{b["seller"]} <span class="muted">({b["rating_count"]:,})</span></td>'
            f'<td><span class="badge bd-{mc}">{b["media"]}</span> <span class="badge bd-{sc}">{b["sleeve"]}</span></td>'
            f'<td><a class="btn-link" href="{lhref}" target="_blank" onclick="event.stopPropagation()">Koop &rarr;</a></td>'
            f'</tr>'
        )
    if not deal_rows:
        deal_rows = '<tr><td colspan="9" class="no-data">Geen deals gevonden.</td></tr>'

    deals_page = f"""
    <div class="page" id="deals" style="display:none">
      <div class="page-header">
        <h2>Top Deals</h2>
        <span class="sub">{now} &nbsp;&middot;&nbsp; listings goedkoper dan laagste historische verkoop &nbsp;&middot;&nbsp; verkopers &ge;{MIN_SELLER_RATINGS} ratings</span>
      </div>
      <div class="card">
        <table class="ov-table">
          <thead><tr>
            <th>Artiest</th><th>Release</th><th>Staat</th>
            <th class="th-r">Listing</th><th class="th-r">Laagste verkoop</th>
            <th class="th-r">Korting</th><th>Verkoper</th><th>Disc / Hoes</th><th></th>
          </tr></thead>
          <tbody>{deal_rows}</tbody>
        </table>
      </div>
    </div>"""

    # ── Navigatie sidebar ──────────────────────────────────────────────────
    from collections import defaultdict as _dd
    genre_groups = _dd(list)
    for group in groups:
        genre = GROUP_GENRES.get(group, "Overige")
        genre_groups[genre].append(group)

    # Vaste volgorde + eventuele extra genres achteraan
    ordered_genres = [g for g in GENRE_ORDER if g in genre_groups]
    for g in genre_groups:
        if g not in ordered_genres:
            ordered_genres.append(g)

    nav_genres = ""
    for genre in ordered_genres:
        items = ""
        for group in genre_groups[genre]:
            items += (
                f'<div class="nav-item" data-page="{_gid(group)}" '
                f'onclick="showPage(\'{_gid(group)}\')">{group}</div>\n'
            )
        nav_genres += f"""<details class="nav-genre">
          <summary>{genre}</summary>
          {items}
        </details>\n"""

    nav = f"""
    <nav>
      <div class="nav-logo"><span class="nav-logo-icon">&#9679;</span> Vinyl</div>
      <div class="nav-item active" data-page="home" onclick="showPage('home')">
        <span class="nav-icon">&#9646;</span> Home
      </div>
      <div class="nav-item" data-page="deals" onclick="showPage('deals')">
        <span class="nav-icon">&#9650;</span> Top Deals
        <span class="nav-badge">{len(deals)}</span>
      </div>
      <div class="nav-sep"></div>
      {nav_genres}
    </nav>"""

    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#0F2245">
<title>Vinyl Tracker</title>
<style>
  :root{{
    --navy:#0F2245;--navy2:#1a3460;--accent:#10B981;--accent-dim:#059669;
    --bg:#F1F5F9;--surface:#fff;--border:#E2E8F0;
    --text:#1E293B;--muted:#64748B;--muted2:#94A3B8;
    --deal-bg:#D1FAE5;--deal-fg:#065F46;
    --warn-bg:#FFFBEB;--warn-bdr:#FDE68A;
    --purple:#7C3AED;--purple2:#6D28D9;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
        font-size:14px;color:var(--text);display:flex;height:100vh;height:100dvh;
        overflow:hidden;background:var(--bg)}}

  /* ── Sidebar ── */
  nav{{width:196px;min-width:196px;background:var(--navy);color:#fff;
       display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}}
  .nav-logo{{padding:18px 16px 14px;font-size:16px;font-weight:700;letter-spacing:-.2px;
             border-bottom:1px solid rgba(255,255,255,.1);display:flex;align-items:center;gap:8px}}
  .nav-logo-icon{{color:var(--accent);font-size:10px}}
  .nav-section{{padding:16px 16px 5px;font-size:10px;font-weight:600;
                text-transform:uppercase;letter-spacing:.8px;color:rgba(255,255,255,.35)}}
  .nav-item{{padding:9px 16px;cursor:pointer;font-size:13px;
             border-left:3px solid transparent;transition:background .15s;
             color:rgba(255,255,255,.7);display:flex;align-items:center;gap:8px}}
  .nav-item:hover{{background:rgba(255,255,255,.07);color:#fff}}
  .nav-item.active{{background:rgba(255,255,255,.12);border-left-color:var(--accent);
                   color:#fff;font-weight:600}}
  .nav-icon{{font-size:8px;opacity:.5}}
  .nav-badge{{margin-left:auto;background:var(--accent);color:#fff;
              font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px}}
  .nav-sep{{height:1px;background:rgba(255,255,255,.1);margin:6px 0}}
  /* ── Genre accordion ── */
  .nav-genre{{border:none}}
  .nav-genre summary{{
    list-style:none;padding:9px 16px;cursor:pointer;font-size:10px;font-weight:600;
    text-transform:uppercase;letter-spacing:.8px;color:rgba(255,255,255,.38);
    display:flex;align-items:center;justify-content:space-between;
    user-select:none;transition:color .15s}}
  .nav-genre summary::-webkit-details-marker{{display:none}}
  .nav-genre summary::after{{
    content:'›';font-size:14px;font-weight:400;transition:transform .2s;
    transform:rotate(90deg);opacity:.5}}
  .nav-genre[open] summary{{color:rgba(255,255,255,.65)}}
  .nav-genre[open] summary::after{{transform:rotate(270deg);opacity:.8}}
  .nav-genre summary:hover{{color:rgba(255,255,255,.7)}}

  /* ── Main area ── */
  main{{flex:1;overflow-y:auto;background:var(--bg);display:flex;flex-direction:column}}

  /* ── Sticky top bar ── */
  .topbar-wrap{{position:sticky;top:0;z-index:50;background:var(--bg)}}
  .topbar{{padding:10px 24px;display:flex;align-items:center;justify-content:flex-end;
           gap:8px;border-bottom:1px solid var(--border);background:var(--bg)}}
  .add-panel{{padding:10px 24px;display:none;align-items:center;gap:10px;
              border-bottom:1px solid var(--border);background:var(--surface)}}

  /* ── Buttons ── */
  .btn{{border:none;padding:7px 13px;border-radius:6px;font-size:12.5px;cursor:pointer;
        font-weight:500;display:inline-flex;align-items:center;gap:5px;
        transition:background .15s,opacity .15s;white-space:nowrap}}
  .btn-pdf{{background:var(--navy);color:#fff}}
  .btn-pdf:hover{{background:var(--navy2)}}
  .btn-add{{background:var(--purple);color:#fff}}
  .btn-add:hover{{background:var(--purple2)}}
  .btn-refresh{{background:var(--accent);color:#fff}}
  .btn-refresh:hover{{background:var(--accent-dim)}}
  .btn-refresh:disabled,.btn-add:disabled{{background:var(--muted2);color:#fff;cursor:not-allowed;opacity:.7}}
  .btn-link{{color:#3B82F6;text-decoration:none;font-size:12px;font-weight:500;
             white-space:nowrap;padding:3px 8px;border-radius:4px;
             border:1px solid #BFDBFE;background:#EFF6FF;transition:background .15s}}
  .btn-link:hover{{background:#DBEAFE}}
  .add-input{{flex:1;border:1.5px solid var(--border);border-radius:6px;
              padding:7px 12px;font-size:13px;outline:none;min-width:0;
              transition:border-color .15s,box-shadow .15s}}
  .add-input:focus{{border-color:var(--purple);box-shadow:0 0 0 3px rgba(124,58,237,.1)}}
  .btn-go{{background:var(--purple);color:#fff;padding:7px 16px;border:none;
           border-radius:6px;font-size:13px;cursor:pointer;font-weight:500;white-space:nowrap}}
  .btn-go:hover{{background:var(--purple2)}}
  .btn-x{{background:none;border:none;color:var(--muted2);font-size:20px;
          cursor:pointer;padding:0 2px;line-height:1;transition:color .15s}}
  .btn-x:hover{{color:var(--text)}}

  /* ── Page content ── */
  .page{{padding:22px 24px 36px;flex:1}}
  .page-header{{display:flex;align-items:baseline;gap:10px;margin-bottom:18px}}
  h2{{color:var(--text);font-size:20px;font-weight:700;letter-spacing:-.3px}}
  .sub{{color:var(--muted);font-size:12.5px}}
  .muted{{color:var(--muted)}}

  /* ── Stat cards (home) ── */
  .stat-grid{{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}}
  .stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;
              padding:14px 18px;min-width:110px}}
  .stat-card-accent{{border-color:#A7F3D0;background:#ECFDF5}}
  .stat-val{{font-size:24px;font-weight:700;color:var(--text);line-height:1.1}}
  .stat-card-accent .stat-val{{color:#065F46}}
  .stat-lbl{{font-size:11.5px;color:var(--muted);margin-top:3px}}

  /* ── Card wrapper ── */
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;
         overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04)}}

  /* ── Release cards ── */
  .rb{{background:var(--surface);border:1px solid var(--border);border-radius:10px;
       padding:18px 20px;margin-bottom:12px;
       box-shadow:0 1px 3px rgba(0,0,0,.04)}}
  .rb-head{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
  .rb-group{{background:var(--navy);color:#fff;font-size:10px;font-weight:700;
             padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.5px;
             white-space:nowrap}}
  .rb-title{{font-size:15px;font-weight:600;color:var(--text)}}
  .market{{font-size:12px;color:var(--muted);margin-bottom:14px}}
  .market a{{color:#3B82F6;text-decoration:none}}
  .market a:hover{{text-decoration:underline}}
  .conds{{display:flex;flex-wrap:wrap;gap:10px}}
  .cb{{background:#F8FAFC;border:1px solid var(--border);border-radius:8px;
       padding:12px 14px;min-width:185px}}
  .cb-head{{font-weight:600;color:var(--text);font-size:13px;margin-bottom:2px}}
  .cb-n{{font-weight:400;color:var(--muted);font-size:11px}}
  .cb-stats{{font-size:11.5px;color:var(--muted);margin-bottom:8px}}
  .best-listing{{background:var(--warn-bg);border:1px solid var(--warn-bdr);
                 border-radius:6px;padding:6px 10px;margin-bottom:8px;
                 font-size:11.5px;line-height:1.6}}
  .best-listing a{{color:#3B82F6;text-decoration:none}}
  .best-label{{color:var(--muted);font-weight:500}}

  /* ── Tables ── */
  table{{border-collapse:collapse;width:100%;font-size:12.5px}}
  thead th{{background:#F8FAFC;color:var(--muted);padding:9px 12px;text-align:left;
            font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;
            border-bottom:1px solid var(--border)}}
  .th-r{{text-align:right!important}}
  td{{padding:8px 12px;border-bottom:1px solid #F1F5F9;vertical-align:middle}}
  tbody tr:last-child td{{border-bottom:none}}
  .td-num{{text-align:right;font-variant-numeric:tabular-nums}}
  .td-title{{font-size:12.5px;color:var(--text)}}
  .td-seller{{font-size:12px}}
  .home-row{{cursor:pointer;transition:background .1s}}
  .home-row:hover td{{background:#F8FAFC}}
  .no-data{{color:var(--muted);font-style:italic;font-size:12.5px;padding:16px 12px}}

  /* ── Badges ── */
  .badge{{display:inline-block;padding:2px 8px;border-radius:20px;
          font-size:11px;font-weight:600;white-space:nowrap}}
  .bd-M{{background:#D1FAE5;color:#065F46}}
  .bd-NM{{background:#A7F3D0;color:#065F46}}
  .bd-VGp{{background:#BBF7D0;color:#166534}}
  .bd-VG{{background:#FEF3C7;color:#92400E}}
  .bd-Gp{{background:#FDE68A;color:#854D0E}}
  .bd-G{{background:#FECACA;color:#991B1B}}
  .bd-F{{background:#FCA5A5;color:#7F1D1D}}
  .bd-P{{background:#EF4444;color:#fff}}
  .bd-Generic,.bd-NoCover{{background:#E2E8F0;color:var(--muted)}}

  /* ── Deal percentage badge ── */
  .deal-pct{{background:var(--deal-bg);color:var(--deal-fg);
             font-size:12px;font-weight:700;padding:2px 8px;border-radius:20px}}

  /* ── Hamburger ── */
  .hamburger{{display:none;border:none;background:none;cursor:pointer;
              padding:10px;flex-direction:column;justify-content:center;
              align-items:center;gap:5px;min-width:44px;min-height:44px;
              border-radius:6px;-webkit-tap-highlight-color:transparent}}
  .hamburger:active{{background:rgba(0,0,0,.06)}}
  .hamburger span{{display:block;width:20px;height:2px;background:var(--navy);border-radius:2px}}

  /* ── Nav overlay (mobile) ── */
  .nav-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
                z-index:199;-webkit-tap-highlight-color:transparent}}
  .nav-overlay.open{{display:block}}

  /* ── Mobile layout ── */
  @media(max-width:768px){{
    body{{display:block;overflow:hidden;height:100vh;height:100dvh}}
    nav{{position:fixed;inset:0 auto 0 0;width:220px;min-width:220px;z-index:200;
         transform:translateX(-100%);transition:transform .25s ease;
         box-shadow:4px 0 20px rgba(0,0,0,.18)}}
    nav.open{{transform:translateX(0)}}
    .hamburger{{display:flex}}
    main{{height:100vh;height:100dvh;overflow-y:auto;
          -webkit-overflow-scrolling:touch;width:100%}}
    .topbar{{padding:10px 12px;gap:6px}}
    .topbar .btn,.topbar a.btn{{font-size:11.5px;padding:6px 10px}}
    .page{{padding:16px 12px 52px}}
    h2{{font-size:17px}}
    .sub{{font-size:11px}}
    .page-header{{flex-wrap:wrap;gap:4px}}
    .stat-grid{{gap:8px}}
    .stat-card{{flex:1;min-width:calc(50% - 4px);padding:11px 12px}}
    .stat-val{{font-size:20px}}
    .card{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
    .card table{{min-width:500px}}
    .rb{{padding:14px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch}}
    .conds{{flex-direction:column}}
    .cb{{min-width:0;width:100%}}
    .cb table{{min-width:280px}}
    .best-listing{{font-size:11px}}
    .market{{font-size:11.5px}}
    .rb-title{{font-size:14px}}
    .td-title{{max-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .nav-item{{padding:12px 16px;font-size:13.5px;min-height:44px}}
    .nav-genre summary{{padding:12px 16px;min-height:44px}}
    .add-panel{{flex-wrap:wrap;padding:8px 12px}}
    .add-input{{width:100%;min-width:0}}
  }}

  /* ── Print ── */
  @media print{{
    body{{display:block;height:auto;overflow:visible}}
    nav,.topbar-wrap{{display:none}}
    main{{overflow:visible}}
    .page{{display:block!important;padding:10px}}
    .rb{{box-shadow:none;break-inside:avoid}}
    .cb{{break-inside:avoid}}
    .card{{box-shadow:none}}
  }}
</style>
</head>
<body>
<div class="nav-overlay" id="nav-overlay" onclick="toggleNav()"></div>
{nav}
<main>
  <div class="topbar-wrap">
    {"" if not static else f'<div class="topbar"><button class="hamburger" onclick="toggleNav()" aria-label="Menu"><span></span><span></span><span></span></button><span class="sub" style="margin-right:auto">Snapshot: {now}</span><a class="btn btn-refresh" href="https://github.com/schaekentuur-bit/vinyl-tracker/actions" target="_blank">&#8635; Vernieuwen via GitHub</a><button class="btn btn-pdf" onclick="window.print()">&#128438; PDF</button></div>'}
    {"" if static else """<div class="topbar">
      <button class="hamburger" onclick="toggleNav()" aria-label="Menu"><span></span><span></span><span></span></button>
      <button class="btn btn-pdf" onclick="window.print()">&#128438; PDF</button>
      <button class="btn btn-add" id="abtn" onclick="toggleAdd()">&#43; Toevoegen</button>
      <button class="btn btn-refresh" id="rbtn" onclick="doRefresh()">&#8635; Vernieuwen</button>
    </div>
    <div class="add-panel" id="add-panel">
      <input type="text" id="add-url" class="add-input" placeholder="Plak een Discogs release-URL..." />
      <button class="btn-go" onclick="doAdd()">Toevoegen</button>
      <button class="btn-x" onclick="toggleAdd()" title="Sluiten">&#215;</button>
    </div>"""}
  </div>
  {home_page}
  {deals_page}
  {artist_pages}
</main>
<script>
function showPage(id){{
  document.querySelectorAll('.page').forEach(function(p){{p.style.display='none';}});
  var el=document.getElementById(id);
  if(el){{el.style.display='block'; document.querySelector('main').scrollTop=0;}}
  document.querySelectorAll('.nav-item').forEach(function(i){{i.classList.remove('active');}});
  var ni=document.querySelector('[data-page="'+id+'"]');
  if(ni){{
    ni.classList.add('active');
    var det=ni.closest('details');
    if(det) det.open=true;
  }}
  history.replaceState(null,'','#'+id);
  if(window.innerWidth<=768){{
    document.querySelector('nav').classList.remove('open');
    document.getElementById('nav-overlay').classList.remove('open');
  }}
}}
function toggleNav(){{
  var nav=document.querySelector('nav');
  var ov=document.getElementById('nav-overlay');
  var open=nav.classList.toggle('open');
  ov.classList.toggle('open',open);
}}
function doRefresh(){{
  var b=document.getElementById('rbtn');
  b.disabled=true; b.textContent='Bezig...';
  window.location.href='/refresh';
}}
function toggleAdd(){{
  var p=document.getElementById('add-panel');
  var open=p.style.display==='flex';
  p.style.display=open?'none':'flex';
  if(!open){{ document.getElementById('add-url').focus(); document.getElementById('add-url').value=''; }}
}}
function doAdd(){{
  var url=document.getElementById('add-url').value.trim();
  if(!url) return;
  document.getElementById('abtn').disabled=true;
  document.getElementById('abtn').textContent='Bezig...';
  window.location.href='/add?url='+encodeURIComponent(url);
}}
var _addUrl=document.getElementById('add-url');
if(_addUrl){{
  _addUrl.addEventListener('keydown',function(e){{
    if(e.key==='Enter') doAdd();
    if(e.key==='Escape') toggleAdd();
  }});
}}
var initPage=(window.location.hash||'#home').slice(1);
showPage(document.getElementById(initPage)?initPage:'home');
</script>
</body>
</html>"""

    return html

def extract_release_id(url):
    """Extract Discogs release ID from various URL formats."""
    m = re.search(r'/release[s]?/(\d+)', url)
    if not m:
        m = re.search(r'/sell/(?:release|history)/(\d+)', url)
    if not m:
        m = re.search(r'\b(\d{5,8})\b', url)
    return m.group(1) if m else None

def fetch_release_info(release_id):
    """Fetch artist, title, catno from Discogs API. Returns (group, display_title) or None."""
    try:
        r = std_requests.get(
            f"https://api.discogs.com/releases/{release_id}",
            headers=DISCOGS_HEADERS, timeout=10
        )
        r.raise_for_status()
        data = r.json()
        raw_artist = data.get("artists", [{}])[0].get("name", "Onbekend")
        artist = re.sub(r'\s*\(\d+\)$', '', raw_artist).strip()
        title  = data.get("title", "Onbekend")
        catno  = data.get("labels", [{}])[0].get("catno", "")
        country = data.get("country", "")
        year    = str(data.get("year", ""))
        display_title = f"{title} ({catno}, {country} {year})".strip(", ()")

        # Match to an existing group or fall back to "Overige"
        existing_groups = list(dict.fromkeys(v[0] for v in RELEASES.values()))
        group = "Overige"
        for g in existing_groups:
            if artist.lower() in g.lower() or g.lower() in artist.lower():
                group = g
                break

        return group, display_title
    except Exception as e:
        print(f"  API fout voor {release_id}: {e}")
        return None

LOADING_HTML = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<title>Vernieuwen...</title>
<style>
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;display:flex;align-items:center;
     justify-content:center;height:100vh;margin:0;background:#F1F5F9;flex-direction:column;gap:14px}
.spinner{width:40px;height:40px;border:4px solid #E2E8F0;border-top-color:#10B981;
         border-radius:50%;animation:spin .75s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
h3{margin:0;font-size:18px;font-weight:700;color:#1E293B}
p{margin:0;color:#64748B;font-size:13px;text-align:center;line-height:1.6}
</style>
</head>
<body>
<div class="spinner"></div>
<h3>Data wordt vernieuwd...</h3>
<p>Even geduld &mdash; dit duurt 1 a 2 minuten.<br>Pagina springt automatisch terug.</p>
<script>
function check(){
  fetch('/status').then(r=>r.json()).then(d=>{
    if(!d.refreshing) window.location='/';
    else setTimeout(check,2500);
  }).catch(()=>setTimeout(check,2500));
}
setTimeout(check,3000);
</script>
</body>
</html>"""

# ─── SCRAPEN (herbruikbaar voor refresh) ──────────────────────────────────────

def scrape_all(cookies, session, force_listings=False, force_stats=False):
    """Scrapes alle releases en geeft results terug. Respecteert cache tenzij force=True."""
    sales_cache    = load_cache(SALES_CACHE)
    stats_cache    = load_cache(STATS_CACHE)
    listings_cache = {} if force_listings else load_cache(LISTINGS_CACHE)
    if force_stats:
        stats_cache = {}
    today = datetime.now().strftime("%Y-%m-%d")
    results = []

    for release_id, (group, title) in RELEASES.items():
        print(f">> {group} - {title}")

        # Verkoophistorie (gecached, 7 dagen TTL)
        sc_entry = sales_cache.get(release_id, {})
        if cache_is_fresh(sc_entry):
            sales = sc_entry["sales"]
            print(f"  Geschiedenis cache: {len(sales)} verkopen")
        else:
            sales = scrape_history(release_id, cookies, session)
            sales_cache[release_id] = {"fetched_at": today, "sales": sales}
            save_cache(SALES_CACHE, sales_cache)
            print(f"  Geschiedenis gescraped: {len(sales)} verkopen")
            time.sleep(2)

        # Marktstatistieken API (gecached per dag)
        stats_key = f"{release_id}_{today}"
        stats = stats_cache.get(stats_key)
        if not stats:
            stats = get_market_stats(release_id)
            if stats:
                stats_cache[stats_key] = stats
                save_cache(STATS_CACHE, stats_cache)
            time.sleep(0.5)

        # Marketplace listings via Discogs API (gecached, 1 dag TTL; altijd vers bij force)
        lc_entry = listings_cache.get(release_id, {})
        if cache_is_fresh(lc_entry, max_days=1) and lc_entry.get("listings"):
            raw_listings = lc_entry["listings"]
            print(f"  Listings cache: {len(raw_listings)} listings")
        else:
            raw_listings = scrape_listings_api(release_id)
            if raw_listings:
                listings_cache[release_id] = {"fetched_at": today, "listings": raw_listings}
                save_cache(LISTINGS_CACHE, listings_cache)
                print(f"  Listings API: {len(raw_listings)} listings")
            else:
                old = lc_entry.get("listings", [])
                raw_listings = old
                print(f"  Listings API leeg, cache bewaard: {len(old)} listings")
            time.sleep(0.5)

        results.append({
            "id":       release_id,
            "group":    group,
            "title":    title,
            "sales":    sales,
            "stats":    stats or {},
            "listings": raw_listings,
        })

    return results

# ─── LOKALE SERVER ─────────────────────────────────────────────────────────────

def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open("vinyl_tracker_run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_server(initial_results, cookies, session):
    state = {"results": initial_results, "refreshing": False}

    def do_refresh():
        try:
            _log("Vernieuwen gestart")
            results = scrape_all(cookies, session, force_listings=True, force_stats=True)
            state["results"] = results
            _log("Scrapen klaar, email berekenen")

            deals = compute_deals(results)
            seen  = load_cache(DEALS_SEEN_FILE)
            if not seen:
                send_deals_email(deals, subject_prefix="Alle actieve deals")
            else:
                new = find_new_deals(deals, seen)
                send_deals_email(new, subject_prefix="Nieuwe deals")
            save_cache(DEALS_SEEN_FILE, {_deal_key(d): {
                "price":    d["best"]["price"],
                "currency": d["best"]["currency"],
                "disc":     d["disc"],
                "seller":   d["best"]["seller"],
                "title":    d["r"]["title"],
                "group":    d["r"]["group"],
            } for d in deals})
            _log("Vernieuwen klaar")
        except Exception as e:
            import traceback
            _log(f"FOUT bij vernieuwen: {e}")
            _log(traceback.format_exc())
        finally:
            state["refreshing"] = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                # HTML altijd vers genereren — nooit stale cache
                html = build_html(state["results"])
                self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))
            elif self.path == "/refresh":
                if not state["refreshing"]:
                    state["refreshing"] = True
                    threading.Thread(target=do_refresh, daemon=True).start()
                self._redirect("/refreshing")
            elif self.path == "/refreshing":
                self._respond(200, "text/html; charset=utf-8",
                              LOADING_HTML.encode("utf-8"))
            elif self.path == "/status":
                body = json.dumps({"refreshing": state["refreshing"]}).encode()
                self._respond(200, "application/json", body)
            elif self.path.startswith("/add"):
                from urllib.parse import urlparse, parse_qs, unquote
                qs  = parse_qs(urlparse(self.path).query)
                raw = unquote(qs.get("url", [""])[0]).strip()
                release_id = extract_release_id(raw)
                if not release_id:
                    self._redirect("/?msg=invalid_url")
                    return
                if release_id in RELEASES:
                    gid = _gid(RELEASES[release_id][0])
                    self._redirect(f"/#{gid}")
                    return
                info = fetch_release_info(release_id)
                if not info:
                    self._redirect("/?msg=api_error")
                    return
                group, display_title = info
                RELEASES[release_id] = (group, display_title)
                user_rel = load_cache(USER_RELEASES_FILE)
                user_rel[release_id] = [group, display_title]
                save_cache(USER_RELEASES_FILE, user_rel)
                print(f"Toegevoegd: {release_id} — {group} / {display_title}")
                if not state["refreshing"]:
                    state["refreshing"] = True
                    threading.Thread(target=do_refresh, daemon=True).start()
                self._redirect("/refreshing")
            else:
                self.send_response(404); self.end_headers()

        def _respond(self, code, ct, body):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *_):
            pass

    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    server = HTTPServer(("", PORT), Handler)
    _log(f"Server draait op http://localhost:{PORT}")
    _log(f"Telefoon/tablet:   http://{local_ip}:{PORT}")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Server gestopt.")
        server.server_close()

# ─── HOOFDLOGICA ──────────────────────────────────────────────────────────────

def main():
    print(f"\nVinyl Tracker - {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    # Laad eerder toegevoegde releases
    for rid, val in load_cache(USER_RELEASES_FILE).items():
        if rid not in RELEASES:
            RELEASES[rid] = tuple(val)

    cookies = load_cookies()
    session = cf_requests.Session(impersonate="chrome124")
    session.headers.update({"Accept-Language": "nl-BE,nl;q=0.9"})

    print(f"{len(cookies)} browser-cookies geladen")
    print(f"{len(RELEASES)} releases te verwerken\n")

    results = scrape_all(cookies, session)
    run_server(results, cookies, session)

if __name__ == "__main__":
    main()
