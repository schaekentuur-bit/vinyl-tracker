"""
Vinyl Price Tracker — AI-gestuurd via Claude + Discogs
Analyseert verkoophistorie per release en stuurt een e-mail met AI-advies.

Vereisten:
    pip install requests anthropic

Configuratie:
    1. Vul DISCOGS_TOKEN in (discogs.com > Settings > Developers > Generate token)
    2. Vul ANTHROPIC_API_KEY in (console.anthropic.com > API Keys)
    3. Vul GMAIL_APP_PASSWORD in (myaccount.google.com > Beveiliging > App-wachtwoorden)
    4. Vul de echte Discogs release-IDs in onder RELEASES (staat in de URL van elke releasepagina)
    5. Voer uit: python vinyl_tracker.py
    6. Optioneel: automatiseer via Windows Task Scheduler of cron

Hoe werkt het:
    - Haalt actieve listings op per release
    - Haalt marktstatistieken op (mediane prijs, laagste ooit, aantal te koop)
    - Stuurt die data naar Claude
    - Claude analyseert of een listing interessant is gegeven de historische trend
    - Resultaten worden gemaild met analyse per release
"""

import requests
import smtplib
import json
import os
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ─── CONFIGURATIE ────────────────────────────────────────────────────────────

DISCOGS_TOKEN      = "JOUW_DISCOGS_TOKEN_HIER"
ANTHROPIC_API_KEY  = "JOUW_ANTHROPIC_API_KEY_HIER"
GMAIL_ADDRESS      = "schaekentuur@gmail.com"
GMAIL_APP_PASSWORD = "JOUW_GMAIL_APP_PASSWORD_HIER"

# Releases om te volgen: {"discogs_release_id": "beschrijvende naam"}
# De ID staat in de URL: discogs.com/release/XXXXXXX
RELEASES = {
    # ── OASIS ──
    "VERVANG_ID": "Oasis - Morning Glory (CRE LP 189, Damont, UK 1995)",
    "VERVANG_ID2": "Oasis - Definitely Maybe (CRE LP 169, Damont, UK 1994)",
    "VERVANG_ID3": "Oasis - Morning Glory (CRE LP 189, MPO, UK 1995)",
    "VERVANG_ID4": "Oasis - Time Flies RSD Box Set (RKIDLP150RSD, 2025)",
    "VERVANG_ID5": "Oasis - Definitely Maybe reissue (RKIDLP70, 2014)",
    "VERVANG_ID6": "Oasis - Morning Glory reissue (RKIDLP73)",

    # ── RED HOT CHILI PEPPERS ──
    "VERVANG_ID7":  "RHCP - Blood Sugar Sex Magik (7599-26681-1, EU 1991)",
    "VERVANG_ID8":  "RHCP - Blood Sugar Sex Magik (468348-1, US 2012 remaster)",
    "VERVANG_ID9":  "RHCP - Californication (9362-47386-1, EU 1999)",
    "VERVANG_ID10": "RHCP - Californication 25th Anniversary (93624843276, 2024)",
    "VERVANG_ID11": "RHCP - By The Way (9 48140-1, US 2002)",
    "VERVANG_ID12": "RHCP - By The Way reissue (093624814016, 2020)",
    "VERVANG_ID13": "RHCP - Stadium Arcadium (49996-1, US 2006)",
    "VERVANG_ID14": "RHCP - Stadium Arcadium reissue (9362-44391-1, EU 2016)",

    # ── THE BEATLES ──
    "VERVANG_ID15": "Beatles - Abbey Road (PCS 7088, UK first pressing 1969)",
    "VERVANG_ID16": "Beatles - Abbey Road 50th Anniversary (0602577915123, 2019)",

    # ── MICHAEL JACKSON ──
    "VERVANG_ID17": "MJ - Thriller (QE 38112, Pitman, US 1982)",
    "VERVANG_ID18": "MJ - Bad (E 40600, Carrollton, US 1987)",

    # ── METALLICA ──
    "VERVANG_ID19": "Metallica - Master of Puppets (60439-1, Allied, US 1986)",
    "VERVANG_ID20": "Metallica - Black Album (61113-1, Elektra, US 1991)",
    "VERVANG_ID21": "Metallica - Black Album (510 022-1, Vertigo, EU 1991)",

    # ── QUEEN ──
    "VERVANG_ID22": "Queen - A Night at the Opera (EMTC 103, Blair's Cut, UK 1975)",
    "VERVANG_ID23": "Queen - A Night at the Opera half-speed (00602547202697, 2015)",

    # ── AC/DC ──
    "VERVANG_ID24": "AC/DC - Back in Black (APLP-046, Australisch origineel 1980)",
    "VERVANG_ID25": "AC/DC - Back in Black (SD 16018, US RL pressing 1980)",

    # ── LINKIN PARK ──
    "VERVANG_ID26": "Linkin Park - Hybrid Theory (9 47755-1, US 2001)",
    "VERVANG_ID27": "Linkin Park - Meteora (48186-1, US 2003)",

    # ── GREEN DAY ──
    "VERVANG_ID28": "Green Day - American Idiot (9362-48777-1, EU 2004)",

    # ── GUNS N' ROSES ──
    "VERVANG_ID29": "GNR - Appetite for Destruction (XXXG 24148, US Allied 1987)",
    "VERVANG_ID30": "GNR - Appetite for Destruction (924 148-1, EU uncensored 1987)",
    "VERVANG_ID31": "GNR - Appetite for Destruction (0072064241481, reissue 2015)",

    # ── NOTORIOUS B.I.G. ──
    "VERVANG_ID32": "Notorious B.I.G. - Ready to Die (78612-73000-1, US 1994)",

    # ── KENDRICK LAMAR ──
    "VERVANG_ID33": "Kendrick Lamar - Good Kid M.A.A.D City (B0017695-01, US 2012)",

    # ── THE KILLERS ──
    "VERVANG_ID34": "The Killers - Hot Fuss (LIZARD011X, blue marbled, UK 2004)",

    # ── DOE MAAR ──
    "VERVANG_ID35": "Doe Maar - Skunk (Kil 19934 Kl, NL 1981)",
    "VERVANG_ID36": "Doe Maar - Doris Day En Andere Stukken (Kil 21032 Kl, NL 1982)",

    # ── OVERIGE ──
    "VERVANG_ID37": "Flatbush Zombies - Vacation in Hell (clear/black smoke, 2018)",
    "VERVANG_ID38": "Beast Coast - Escape From New York (blue, 2019)",
    "VERVANG_ID39": "Mobb Deep - The Infamous (07863 66480-1, US 1995)",
    "VERVANG_ID40": "Sticks - Stickmatic (350 405-3, NL 2020)",
}

CACHE_FILE = "vinyl_history_cache.json"

# ─── DISCOGS API ──────────────────────────────────────────────────────────────

DISCOGS_HEADERS = {
    "Authorization": f"Discogs token={DISCOGS_TOKEN}",
    "User-Agent": "VinylTrackerAI/2.0 +schaekentuur@gmail.com"
}

def get_listings(release_id):
    url = "https://api.discogs.com/marketplace/listings"
    params = {
        "release_id": release_id,
        "status": "For Sale",
        "sort": "price",
        "sort_order": "asc",
        "per_page": 5
    }
    try:
        r = requests.get(url, headers=DISCOGS_HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("listings", [])
    except Exception as e:
        print(f"  Fout listings {release_id}: {e}")
        return []

def get_price_stats(release_id):
    url = f"https://api.discogs.com/marketplace/stats/{release_id}"
    try:
        r = requests.get(url, headers=DISCOGS_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Fout stats {release_id}: {e}")
        return {}

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

# ─── CLAUDE AI ANALYSE ────────────────────────────────────────────────────────

def analyse_with_claude(release_name, listings, stats):
    listings_text = ""
    for i, l in enumerate(listings[:5], 1):
        price = l.get("price", {})
        listings_text += (
            f"  {i}. {l.get('condition', '?')} — "
            f"{price.get('currency', 'EUR')} {price.get('value', 0):.2f} "
            f"— {l.get('seller', {}).get('username', '?')} "
            f"({l.get('ships_from', '?')})\n"
        )

    lowest = stats.get("lowest_price", {})
    median = stats.get("median_price", {})
    stats_text = (
        f"Laagste prijs ooit gezien: {lowest.get('currency','EUR')} {lowest.get('value','onbekend')}\n"
        f"Mediane verkoopprijs: {median.get('currency','EUR')} {median.get('value','onbekend')}\n"
        f"Momenteel te koop: {stats.get('num_for_sale','onbekend')} exemplaren\n"
    )

    prompt = f"""Je bent een expert in vinyl platen als investering. Analyseer of de huidige listings interessant zijn.

Release: {release_name}

Actieve listings (goedkoopste eerst):
{listings_text}
Marktdata:
{stats_text}
Geef een directe analyse in 3-4 zinnen in het Nederlands:
- Is de goedkoopste listing aantrekkelijk t.o.v. de mediane marktprijs?
- Wat is je aanbeveling: kopen, afwachten, of overslaan?
- Waarom?

Wees specifiek met prijzen. Geen omhaal."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        print(f"  Fout Claude: {e}")
        return "Analyse niet beschikbaar."

# ─── E-MAIL ───────────────────────────────────────────────────────────────────

def send_email(subject, body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS
    msg.attach(MIMEText(body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
        print("  E-mail verstuurd.")
    except Exception as e:
        print(f"  Fout e-mail: {e}")

def build_email(results):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    blocks = ""
    for r in results:
        listing = r["cheapest"]
        price = listing.get("price", {})
        stats = r["stats"]
        median = stats.get("median_price", {})
        url = f"https://www.discogs.com/sell/item/{listing.get('id', '')}"

        blocks += f"""
        <div style="border:1px solid #ddd;border-radius:6px;padding:16px;margin-bottom:20px;">
            <h3 style="margin:0 0 10px 0;color:#222;">{r['name']}</h3>
            <table style="font-size:14px;color:#333;">
                <tr>
                    <td style="padding:3px 16px 3px 0;color:#888;">Goedkoopste listing</td>
                    <td><strong>{listing.get('condition','?')}</strong> —
                        <span style="color:green;font-weight:bold;">
                            {price.get('currency','EUR')} {price.get('value',0):.2f}
                        </span>
                        — {listing.get('seller',{}).get('username','?')}
                        ({listing.get('ships_from','?')})
                    </td>
                </tr>
                <tr>
                    <td style="padding:3px 16px 3px 0;color:#888;">Mediane marktprijs</td>
                    <td>{median.get('currency','EUR')} {median.get('value','?')}</td>
                </tr>
                <tr>
                    <td style="padding:3px 16px 3px 0;color:#888;">Te koop</td>
                    <td>{stats.get('num_for_sale','?')} exemplaren</td>
                </tr>
            </table>
            <div style="background:#f5f8ff;border-left:3px solid #4a90d9;
                        padding:10px 14px;margin-top:12px;font-size:14px;line-height:1.6;">
                <strong>Claude:</strong> {r['analysis']}
            </div>
            <a href="{url}" style="display:inline-block;margin-top:12px;
               background:#4a90d9;color:white;padding:7px 16px;
               border-radius:4px;text-decoration:none;font-size:13px;">
               Bekijk op Discogs →
            </a>
        </div>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:820px;margin:auto;padding:24px;">
        <h2 style="color:#222;margin-bottom:4px;">🎵 Vinyl Tracker AI</h2>
        <p style="color:#888;margin-top:0;">{now} — {len(results)} release(s) geanalyseerd</p>
        {blocks}
        <p style="color:#ccc;font-size:12px;margin-top:30px;">
            Voeg releases toe in vinyl_tracker.py onder RELEASES.
        </p>
    </body></html>"""

# ─── HOOFDLOGICA ──────────────────────────────────────────────────────────────

def main():
    print(f"\nVinyl Tracker AI — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    valid = {k: v for k, v in RELEASES.items() if not k.startswith("VERVANG")}
    print(f"{len(valid)} releases te analyseren (van {len(RELEASES)} totaal)\n")

    cache = load_cache()
    results = []

    for release_id, release_name in valid.items():
        print(f"→ {release_name}")

        listings = get_listings(release_id)
        if not listings:
            print("  Geen listings.\n")
            continue

        cache_key = f"{release_id}_{datetime.now().strftime('%Y-%m-%d')}"
        stats = cache.get(cache_key)
        if not stats:
            stats = get_price_stats(release_id)
            cache[cache_key] = stats
            save_cache(cache)

        analysis = analyse_with_claude(release_name, listings, stats)
        cheapest = listings[0]

        results.append({
            "name": release_name,
            "cheapest": cheapest,
            "stats": stats,
            "analysis": analysis
        })

        p = cheapest.get("price", {})
        print(f"  {cheapest.get('condition','?')} — {p.get('value',0):.2f} {p.get('currency','EUR')}")
        print(f"  {analysis[:100]}...\n")
        time.sleep(1)

    if results:
        body = build_email(results)
        send_email(
            f"🎵 Vinyl Tracker — {len(results)} releases geanalyseerd — {datetime.now().strftime('%d/%m/%Y')}",
            body
        )
    else:
        print("Niets te mailen.")

    print("Klaar.")

if __name__ == "__main__":
    main()
