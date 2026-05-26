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
import html as _html_mod
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from curl_cffi import requests as cf_requests
import requests as std_requests

# ─── CONFIGURATIE ─────────────────────────────────────────────────────────────

DISCOGS_TOKEN      = os.getenv("DISCOGS_TOKEN", "").lstrip("﻿").strip()
COOKIES_FILE       = "cookies_www.discogs.com.txt"
SALES_CACHE        = "vinyl_sales_cache.json"
STATS_CACHE        = "vinyl_history_cache.json"
LISTINGS_CACHE     = "vinyl_listings_cache.json"
DEALS_SEEN_FILE    = "vinyl_deals_seen.json"
LISTINGS_SEEN_FILE = "vinyl_listings_seen.json"
FAVORITES_FILE     = "vinyl_favorites.json"
LISTINGS_SEEN_DAYS = 30   # geziene listings ouder dan N dagen worden gesneden
CACHE_DAYS         = 7   # marktstatistieken na X dagen opnieuw ophalen
SALES_CACHE_DAYS   = 1   # verkoophistorie na X dagen opnieuw ophalen (1 = dagelijks vers)
LISTINGS_CACHE_HOURS = 0 # marketplace listings: 0 = altijd verversen bij lokale run
MIN_SELLER_RATINGS = 50  # minimaal aantal ratings voor een verkoper
EU_COUNTRIES = {
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece", "Hungary",
    "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
    "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
    "Spain", "Sweden", "United Kingdom",
}
NON_EU_VAT        = 0.21   # Belgische BTW op invoer
NON_EU_DUTY_RATE  = 0.065  # douane vinyl (HS 8524) boven drempel
NON_EU_DUTY_LIMIT = 150.0  # drempel douaneheffing (EUR)
NON_EU_HANDLING   = 15.0   # bpost verwerkingsvergoeding bij invoer

# Staffel: % onder historisch gemiddelde vereist voor "goede deal"
# Hogere prijsklasse = lager percentage (want het absolute bedrag is al groter)
DEALS_AVG_TIERS = [
    (  30,  10),   # gem. < € 30  → vereist 10% korting
    (  75,  10),   # gem. € 30–75 → vereist 10% korting
    ( 200,   5),   # gem. €75–200 → vereist  5% korting
    (float("inf"), 2.5),  # gem. > €200  → vereist 2,5% korting
]

def _deals_avg_pct(avg_eur: float) -> float:
    """Geef de vereiste kortingsdrempel terug op basis van gemiddelde prijs."""
    for ceiling, pct in DEALS_AVG_TIERS:
        if avg_eur < ceiling:
            return pct
    return DEALS_AVG_TIERS[-1][1]

def _non_eu_adjusted_total(total_eur: float) -> float:
    """Schat de werkelijke kost voor een Belgische koper van een non-EU listing (BTW + douane + bpost-verwerking)."""
    vat  = total_eur * NON_EU_VAT
    duty = total_eur * NON_EU_DUTY_RATE if total_eur > NON_EU_DUTY_LIMIT else 0.0
    return total_eur + vat + duty + NON_EU_HANDLING

PORT               = 8765
USER_RELEASES_FILE = "user_releases.json"
THUMB_CACHE        = "vinyl_thumb_cache.json"
MY_COLLECTION_FILE = "my_collection.json"
COLLECTION_CACHE_DAYS = 1  # collectie dagelijks verversen

EMAIL_FROM       = os.getenv("EMAIL_FROM",       "").lstrip("﻿").strip()
EMAIL_TO         = os.getenv("EMAIL_TO",         "").lstrip("﻿").strip()
EMAIL_PASS       = os.getenv("EMAIL_PASS",       "").lstrip("﻿").strip()
DISCOGS_USERNAME = os.getenv("DISCOGS_USERNAME", "").lstrip("﻿").strip()

# Lokale overrides uit config.py (staat in .gitignore, nooit op GitHub)
try:
    import config as _cfg
    DISCOGS_TOKEN    = DISCOGS_TOKEN    or getattr(_cfg, "DISCOGS_TOKEN",    "")
    EMAIL_FROM       = EMAIL_FROM       or getattr(_cfg, "EMAIL_FROM",       "")
    EMAIL_TO         = EMAIL_TO         or getattr(_cfg, "EMAIL_TO",         "")
    EMAIL_PASS       = EMAIL_PASS       or getattr(_cfg, "EMAIL_PASS",       "")
    DISCOGS_USERNAME = DISCOGS_USERNAME or getattr(_cfg, "DISCOGS_USERNAME", "")
except ImportError:
    pass

# ─── RELEASE BESCHRIJVINGEN ──────────────────────────────────────────────────
RELEASE_INFO = {
    # Oasis
    "939519":   ("🏆 First pressing", "Originele UK Damont-persing — de meest gezochte Oasis-versie. Bewaren."),
    "517224":   ("🏆 First pressing", "Originele UK Damont-persing van het debuut — zeldzamer dan Morning Glory. Top collector's item."),
    "6127871":  ("🎵 Luisterversie", "Moderne EU reissue — prima geluid voor dagelijks gebruik."),
    "12864584": ("🎵 Luisterversie", "Moderne reissue — ideaal om af te spelen zonder origineel te slijten."),
    "2334540":  ("🏆 First pressing", "Originele UK Big Brother-persing 2010 (RKIDLP66) — dit IS de first pressing van Time Flies... 1994-2009. 5LP box set, zwart hype-sticker op shrinkwrap."),
    "2521407":  ("📀 EU origineel (niet first)", "Originele EU Big Brother-persing 2010 (88697722641) — gelijktijdig uitgebracht met de UK, wit hype-sticker i.p.v. zwart. 5LP box set met kleurig 20-pagina boekje."),
    "33663000": ("🎁 RSD 2025", "Worldwide Big Brother Record Store Day 2025 (RKIDLP150RSD) — gelimiteerde RSD First Release editie, april 2025."),
    "34257205": ("🎵 Luisterversie", "Worldwide Big Brother reissue 2025 (RKIDLP150) — zwart vinyl reissue met beperkte print van albumhoes, verwacht juni 2025."),
    # RHCP
    "375491":   ("🏆 First pressing", "Originele EU Warner Bros-persing 1991 — BSSM werd niet commercieel op vinyl uitgebracht in de US in 1991; dit IS de first pressing vinyl."),
    "12042641": ("🎵 Luisterversie", "US remaster 2012 — beste keuze voor dagelijks afspelen."),
    "14914560": ("🏆 First pressing", "Originele US Warner Bros-persing 1999 — dit IS de first pressing van Californication (catno 9 47386-1)."),
    "31323387": ("🎵 Luisterversie", "25th Anniversary persing 2024 — beperkte oplage, goed geluid voor dagelijks afspelen."),
    "420718":   ("🏆 First pressing", "Originele US Warner Bros-persing 2002 — dit IS de first pressing van By The Way."),
    "15276024": ("🎵 Luisterversie", "EU reissue 2020 — voor dagelijks gebruik."),
    "1629020":  ("🏆 First pressing", "Originele US Warner Bros dubbel-LP 2006 — dit IS de first pressing van Stadium Arcadium."),
    "8519678":  ("🎵 Luisterversie", "EU reissue 2016 — prima afspeelkopie."),
    # Beatles
    "7801798":  ("🏆 First pressing", "UK first pressing PCS 7088 uit 1969 — een van de meest begeerde platen ter wereld. Nooit afspelen."),
    "14186441": ("🎵 Luisterversie", "50th Anniversary remaster door Giles Martin — beste klankkwaliteit om af te spelen."),
    # Michael Jackson
    "2911293":  ("🏆 First pressing", "Originele US persing, Pitman-plant 1982 — meest gezochte Thriller-variant (Pitman = meest gewaardeerde CBS-persplaats)."),
    "152946":   ("🏆 First pressing", "Originele EU persing 1982 — collector's item uit het releasejaar."),
    "459606":   ("🏆 First pressing", "Originele US persing Carrollton-plant 1987 — first pressing, iets minder gezocht dan Pitman maar zeker een collector's item."),
    # Metallica
    "1549636":  ("🏆 First pressing", "Originele US Allied-persing 1986 — heilige graal voor metalverzamelaars. Niet afspelen."),
    "11118447": ("🎵 Luisterversie", "Blackened Records reissue US 2017 (BLCKND005R-1) — hoge kwaliteitspers voor dagelijks afspelen."),
    "381988":   ("🏆 First pressing", "Originele US Elektra-persing 1991 — dit IS de first pressing van het Black Album."),
    "439599":   ("📀 EU origineel (niet first)", "Originele EU Vertigo-persing 1991 — tegelijk uitgebracht met de US, maar US Elektra geldt als 'the' first pressing."),
    # Queen
    "612780":   ("🏆 First pressing", "Originele UK persing 1975 — topstuk, consistent stijgend in waarde. Met zorg bewaren."),
    "7541569":  ("🎵 Audiofiele versie", "Half-speed remaster EU 2015 — uitstekend geluid, ideaal voor afspelen."),
    "10130642": ("🏆 First pressing", "Originele UK EMI-persing 1974 (EMC 3061) — dit IS de first pressing van Sheer Heart Attack."),
    "23316872": ("🏆 First pressing", "Originele UK EMI-persing 1976 (EMTC 104) — dit IS de first pressing van A Day at the Races."),
    "22048156": ("🎵 Luisterversie", "Originele US Elektra-persing 1976 (6E-101) — voor dagelijks afspelen."),
    "14031557": ("🏆 First pressing", "Originele UK EMI-persing 1977 (EMA 784) — dit IS de first pressing van News of the World."),
    "3824539":  ("🎵 Luisterversie", "Originele US Elektra-persing 1977 (6E-112) — voor dagelijks afspelen."),
    "475606":   ("🏆 First pressing", "Originele UK EMI-persing 1978 (EMA 788) — dit IS de first pressing van Jazz."),
    "4269045":  ("🎵 Luisterversie", "Originele US Elektra-persing 1978 (6E-166) — voor dagelijks afspelen."),
    "455954":   ("🏆 First pressing", "Originele UK EMI-persing 1980 (EMA 795) — dit IS de first pressing van The Game."),
    "446814":   ("🎵 Luisterversie", "Originele US Elektra-persing 1980 (5E-513) — voor dagelijks afspelen."),
    "589920":   ("🏆 First pressing", "Originele UK EMI-persing 1982 (EMA 797) — dit IS de first pressing van Hot Space."),
    "505097":   ("🎵 Luisterversie", "Originele US Elektra-persing 1982 (E1-60128) — voor dagelijks afspelen."),
    "4732312":  ("🏆 First pressing (promo)", "Originele UK EMI white label promo-persing 1984 (EMC 2400141) — pre-release copy, gezocht door verzamelaars."),
    "36148198": ("🎵 Luisterversie", "EU Virgin EMI remaster 2023 (0602547202789) — voor dagelijks afspelen."),
    "11967456": ("🏆 First pressing", "Originele UK EMI-persing 1986 (EU 3509) — dit IS de first pressing van A Kind of Magic."),
    "25210735": ("🎵 Luisterversie", "US Hollywood Records remaster 2022 (D004064601) — voor dagelijks afspelen."),
    # AC/DC
    "400591":   ("🏆 First pressing", "Australisch origineel 1980 — absolute heilige graal voor AC/DC-verzamelaars."),
    "1949857":  ("🎵 Luisterversie", "EU reissue 2009 — voor dagelijks gebruik, geen collector's waarde."),
    "400587":   ("🏆 First pressing", "Australisch origineel Bon Scott-era 1979 — zeldzamer dan Back in Black. Topstuk."),
    "2520300":  ("🎵 Luisterversie", "EU reissue 2009 (5107641) — voor dagelijks afspelen, geen collector's waarde."),
    # Linkin Park
    "534020":   ("🏆 First pressing", "Originele US Warner Bros-persing 2001 — dit IS de first pressing van Hybrid Theory."),
    "21054706": ("🎵 Luisterversie", "20th Anniversary reissue US 2021 (093624941422) — jubileumeditie voor dagelijks afspelen."),
    "3336797":  ("🏆 First pressing", "Originele US Warner Bros-persing 2003 — dit IS de first pressing van Meteora."),
    "28403278": ("🎵 Luisterversie", "20th Anniversary reissue US 2023 (093624853343) — jubileumeditie voor dagelijks afspelen."),
    # Green Day
    "9452213":  ("🏆 First pressing", "Originele US Reprise-persing 2004 — dit IS de first pressing van American Idiot (catno 9362-48777-1)."),
    "1203470":  ("📀 EU origineel (niet first)", "Originele EU Warner persing 2004 — zelfde catno-reeks maar EU-geperst; de US first pressing zit nu ook in je collectie."),
    # Guns N' Roses
    "383777":   ("🏆 First pressing", "Originele US Allied-persing 1987 — meest gezochte GNR-variant. Topstuk."),
    "7492229":  ("🎵 Luisterversie", "EU reissue 2015 — voor dagelijks afspelen."),
    # Notorious B.I.G.
    "317356":   ("🏆 First pressing", "Originele US persing 1994 — waardevolle klassieker van klassieke hiphop. Stijgende markt."),
    "34578556": ("🎵 Luisterversie", "Rhino reissue US 2004 (RR1 285201) — voor dagelijks afspelen."),
    # Kendrick Lamar
    "3975953":  ("🏆 First pressing", "Originele US TDE/Aftermath-persing 2012 — dit IS de first pressing van GKMC."),
    "30551209": ("🎵 Luisterversie", "US reissue 2022 (B0036420-01) — voor dagelijks afspelen."),
    "8814849":  ("🏆 First pressing", "Originele US TDE-persing 2015, RTI-plant — dit IS de first pressing van TPAB."),
    "23398166": ("🎵 Luisterversie", "US repress 2022 (B0023464-01) — voor dagelijks afspelen."),
    "10559651": ("🏆 First pressing", "Originele US TDE-persing 2017 — dit IS de first pressing van DAMN. (Pulitzer Prize-winnaar)."),
    "25683820": ("🎵 Luisterversie", "EU reissue 2022 (00602557618280) — voor dagelijks afspelen."),
    # The Killers
    "397167":   ("🏆 First pressing", "Originele UK Lizard King-persing 2004, blue marbled vinyl — dit IS de first pressing van Hot Fuss (UK-release)."),
    "20298550": ("🎵 Luisterversie", "US reissue 2017 (B0026979-01) — voor dagelijks afspelen."),
    # Doe Maar
    "402227":   ("🏆 First pressing", "Originele NL Killroy-persing 1981 — dit IS de first pressing van Skunk."),
    "22712903": ("🎵 Luisterversie", "Music on Vinyl EU reissue 2022 (MOVLP2295) — kwalitatieve herpers voor dagelijks afspelen."),
    "382601":   ("🏆 First pressing", "Originele NL Killroy-persing 1982 — dit IS de first pressing van Doris Day."),
    "22494431": ("🎵 Luisterversie", "Music on Vinyl NL reissue 2022 (MOVLP2297) — kwalitatieve herpers voor dagelijks afspelen."),
    # Eagles
    "1934367":  ("🏆 First pressing", "Originele US Elektra-persing 1976 — consistent in waarde, iconisch album."),
    "9847048":  ("🎵 Luisterversie", "Rhino remaster Worldwide 2015 (RRM1-1084) — heldere remaster voor dagelijks afspelen."),
    # Amy Winehouse
    "2848009":  ("📀 US origineel (niet first)", "Originele US persing 2006 — Back to Black was een UK Island-release, dus de UK pressing geldt als the first pressing."),
    "34780535": ("🏆 First pressing", "Originele UK Island-persing 2006 — dit IS de first pressing van Back to Black."),
    # Bob Marley
    "12927816": ("🏆 First pressing", "Originele UK Island-persing 1984 — dit IS de first pressing van Legend (catno BMW 1). Stijgende collector's waarde."),
    "4418438":  ("🎵 Luisterversie", "EU reissue compilatie 2015 — veelgeperste plaat, weinig collector's waarde maar geweldig geluid."),
    "3660230":  ("🏆 First pressing", "Originele Jamaicaanse Tuff Gong-persing 1977 — zeldzame authentieke Bob Marley. Topstuk."),
    "1862215":  ("🎵 Luisterversie", "Island EU reissue 2009 (0600753184196) — voor dagelijks afspelen."),
    "65845":    ("🏆 First pressing", "Originele UK Island-persing 1976 — collectors item uit het releasejaar."),
    "746135":   ("🎵 Luisterversie", "Tuff Gong EU reissue 2001 (TGLLP 6) — voor dagelijks afspelen."),
    # Fleetwood Mac
    "526351":   ("🏆 First pressing", "Originele US Warner Bros-persing 1977 — sterk gestegen in waarde door TikTok-hype."),
    "3229870":  ("🎵 Luisterversie", "Worldwide reissue 2023 (R1 567113) — moderne herpers voor dagelijks afspelen."),
    "4065605":  ("🏆 First pressing", "Originele US Warner Bros-persing 1987 (9 25471-1) — dit IS de first pressing van Tango In The Night."),
    "33831750": ("📀 EU origineel (niet first)", "Originele EU Warner Bros-persing 1987 (WX 65) — gelijktijdig uitgebracht, goede afspeelkopie."),
    # Nirvana
    "1813006":  ("🏆 First pressing", "Originele US DGC-persing 1991 — de meest gewilde Nevermind-versie."),
    "3183667":  ("🎵 Audiofiele versie", "20th Anniversary remaster US 2011 — beste klankkwaliteit voor Nevermind."),
    # Flatbush Zombies
    "12895130": ("🎁 Gekleurd vinyl", "Limited clear/black smoke vinyl 2018 — collectors editie, beperkte oplage."),
    # Beast Coast
    "13672908": ("🎁 Gekleurd vinyl", "Limited blue vinyl 2019 — collectors editie."),
    # Mobb Deep
    "223127":   ("🏆 First pressing", "Originele US Loud Records-persing 1995 — dit IS de first pressing van The Infamous."),
    "7753999":  ("🎵 Luisterversie", "Music on Vinyl EU reissue 2015 (MOVLP1463) — kwalitatieve herpers voor dagelijks afspelen."),
    # Sticks
    "16170729": ("🏆 First pressing", "Originele NL persing 2020 — dit IS de (enige) pressing van Stickmatic."),
    # The Police
    "5305755":  ("🏆 First pressing", "Originele UK A&M-persing 1978 (AMLH 68502) — dit IS de first pressing van Outlandos d'Amour."),
    "13549135": ("🎵 Luisterversie", "US A&M remaster 2018 (676325-1) — voor dagelijks afspelen."),
    "11827033": ("🏆 First pressing", "Originele UK A&M-persing 1979 (AMLH 64792) — dit IS de first pressing van Reggatta de Blanc."),
    "3363252":  ("🎵 Luisterversie", "EU A&M reissue 2008 (0082839479219) — voor dagelijks afspelen."),
    "3214829":  ("🏆 First pressing", "Originele UK A&M-persing 1983 (AMLX 63735) — dit IS de first pressing van Synchronicity."),
    "31334893": ("🎵 Luisterversie", "Worldwide A&M remaster 2024 (558 217-9) — voor dagelijks afspelen."),
    # Led Zeppelin
    "2893139":  ("🏆 First pressing", "Originele UK Atlantic-persing 1971 (2401012) — dit IS de first pressing van Led Zeppelin IV. Een van de meest iconische platen aller tijden."),
    "22645229": ("🎵 Luisterversie", "US Atlantic remaster 2022 (8122-79657-7) — 180g gatefold reissue, ideaal voor dagelijks afspelen."),
    # Rolling Stones
    "7264539":  ("🏆 First pressing (stereo)", "Originele UK Decca stereo-persing 1968 (SKL 4955) — zeldzaamste en meest gewaardeerde versie van Beggars Banquet."),
    "1137475":  ("📀 UK mono origineel", "Originele UK Decca mono-persing 1968 (LK 4955) — andere masterband dan stereo, zeer gezocht door audiofilen."),
    "765072":   ("🏆 First pressing US", "Originele US London Records stereo-persing 1966 (PS 476) — enige versie met Paint It Black op het album."),
    "6003779":  ("📀 UK origineel", "Originele UK Decca mono-persing 1966 (LK 4786) — andere tracklist: geen Paint It Black maar wel Mother's Little Helper."),
    "468054":   ("🏆 First pressing US", "Originele US Rolling Stones Records-persing 1981 (COC 16052) — dit IS de first pressing van Tattoo You."),
    "1931909":  ("📀 UK origineel", "Originele UK Rolling Stones Records-persing 1981 (CUNS 39114) — gelijktijdig uitgebracht, prima luisterversie."),
    "7435111":  ("🏆 First pressing US", "Originele US London Records stereo-persing 1965 (PS 429) — enige versie met (I Can't Get No) Satisfaction op het album."),
    # Elvis Presley
    "13314916": ("🏆 First pressing (EP)", "Originele US RCA Victor-persing 1957 — de eerste editie van de Jailhouse Rock EP (7\", 45 RPM, EPA-4114). Bevat ook Treat Me Nice, I Want to Be Free en Don't Leave Me Now."),
    # 21 Savage
    "10597886": ("🏆 Club Edition", "Gelimiteerde genummerde Club Edition US persing 2017 — de meest gezochte Savage Mode variant. Beperkte oplage."),
    "10752389": ("🎵 Luisterversie", "Standaard US Slaughter Gang-persing 2017 — voor dagelijks afspelen."),
    "10873523": ("🏆 First pressing", "Originele US Slaughter Gang-persing 2017 (88985466821) — dit IS de first pressing van Issa Album."),
    "13876606": ("🎵 Luisterversie", "EU reissue 2019 (889854668211) — voor dagelijks afspelen."),
    "13318697": ("🏆 First pressing", "Originele US Slaughter Gang-persing 2018 (19075923521) — dit IS de first pressing van I Am > I Was."),
    "15624365": ("🎵 Luisterversie", "EU persing 2019 (19075922121) — voor dagelijks afspelen."),
    "17277640": ("🏆 First pressing", "Gelimiteerde US Slaughter Gang-persing 2020 (19439818631) — enige officiële vinyluitgave van Savage Mode II."),
    # 50 Cent
    "485114":   ("🏆 First pressing", "Originele US Aftermath/Shady/Interscope-persing 2003 (0694935441) — dit IS de first pressing van Get Rich or Die Tryin'. Inclusief 10\" × 10\" insert. Meer want (3.792) dan have (3.325) — uitzonderlijk gezocht."),
    "1198408":  ("🎵 Luisterversie", "EU Shady/Aftermath/Interscope-persing 2003 (493 544-1) — 180g originele Europese persing, ideaal voor dagelijks afspelen."),
    "598810":   ("🏆 First pressing", "Originele US Shady/Aftermath/Interscope-persing 2005 (B0004317-01) — dit IS de first pressing van The Massacre."),
    "8954977":  ("🎵 Luisterversie", "US UMe reissue 2016 (B0025252-01) — voor dagelijks afspelen."),
    # U2
    "10456142": ("🏆 First pressing", "Originele UK Island Records-persing 1987 (U26) — dit IS de first pressing van The Joshua Tree. Gatefold hoes met zwart binnenhoesje en gouden labels. Bevat With or Without You en I Still Haven't Found What I'm Looking For."),
    "10395824": ("🎵 Luisterversie", "EU Island Records 30th Anniversary reissue 2017 (5749844) — geremasterd 180g vinyl, ideaal voor dagelijks afspelen."),
    "676619":   ("🏆 First pressing", "Originele EU Island Records-persing 2000 (U212 / 524 653-1) — dit IS de first pressing van All That You Can't Leave Behind. Matte hoes met 16-pagina's boekje en ansichtkaart. Bevat Beautiful Day."),
    "11846916": ("🎵 Luisterversie", "EU/UK/US Island Records remaster 2018 (5796988) — 180g reissue, ideaal voor dagelijks afspelen."),
    # Rowwen Hèze
    "15500018": ("🏆 RSD-editie", "HKM Records NL 2020 (HKM 72031) — gelimiteerde Record Store Day-persing op rood vinyl (45 RPM) van het debuutalbum Blieve Loepe. Bevat Limburg."),
    "26032708": ("🎵 Luisterversie", "HKM Records NL 2023 (HKM 72031) — standaard reissue van Blieve Loepe (1990). Bevat Limburg. Ideaal voor dagelijks afspelen."),
    "15969889": ("🎵 Luisterversie", "HKM Records NL 2020 (HKM 72032) — 2LP reissue van Boem (1991). Bevat Bestel Mar en De Peel In Brand."),
    "18017281": ("🎵 Luisterversie", "HKM Records NL 2021 (HKM 72036) — 2LP reissue van Vandaag (2000). Bevat November en Vergeate."),
    # Janse Bagge Bend
    "16062321": ("🎵 40 jaar jubileum", "Marlstone Music NL 2020 (L202010) — jubileumalbum op geel/beige vinyl ter ere van 40 jaar Janse Bagge Bend. Bevat Blief Bie De Mam. Inclusief bedrukt binnenhoesje."),
    # Gorillaz
    "204021":   ("🏆 First pressing", "Originele EU Parlophone-persing 2001 (7243 531138 1 0) — dit IS de first pressing van Gorillaz. 2LP gatefold met twee bedrukte binnenhoesjes. 23.626 have / 11.524 want."),
    "20414716": ("🎵 Luisterversie", "EU Parlophone Special Cut 2021 (7243 531138 1 0) — moderne reissue met bijgewerkte labelrechten, ideaal voor dagelijks afspelen."),
    "474703":   ("🏆 First pressing", "Originele UK Parlophone-persing 2005 (07243 873838 1 4) — dit IS de first pressing van Demon Days. 2LP gatefold, parental advisory sticker op voorkant. Uitzonderlijk: 16.388 want vs 11.025 have."),
    "32440584": ("🎵 Luisterversie", "EU Parlophone repress (℗ 2017, 0724387383814) — officiële repress, 2LP gatefold, ideaal voor dagelijks afspelen."),
    # Nirvana (aanvulling)
    "1073329":  ("🏆 Limited Edition", "US DGC-persing 1993 (DGC-24607) — gelimiteerde Special Edition van In Utero op transparant clear vinyl. 'Special Limited Edition Disc' op de voorkant. 6.505 want vs 4.852 have — uitzonderlijk gezocht."),
    "1559511":  ("🎵 Luisterversie", "EU Geffen 180g reissue 2008 (0720642453612) — geremasterd, deel van de Back To Black-serie. Inclusief download-voucher."),
    # The Clash
    "4126519":  ("🏆 First pressing", "Originele UK CBS-persing 1982 (FMLN 2) — dit IS de first pressing van Combat Rock. Custom CBS-labels, bedrukt binnenhoesje. Vroege exemplaren inclusief poster. Bevat Should I Stay Or Should I Go en Rock The Casbah."),
    "4914174":  ("🎵 Luisterversie", "EU Columbia 180g reissue 2013 (88725446971) — geremasterd, inclusief bedrukt binnenhoesje met teksten en bandsfoto op dik karton."),
    "470912":   ("🏆 First pressing", "Originele UK CBS-persing 1979 (CBS CLASH 3) — dit IS de first pressing van London Calling. 2LP met twee bedrukte inserts (28×28cm). 15.762 have / 13.663 want — iconisch album."),
    "2048710":  ("🎵 Luisterversie", "EU Music On Vinyl 2LP 180g reissue 2009 (MOVLP050) — 30th anniversary reissue, geremasterd, ideaal voor dagelijks afspelen."),
    # Guns N' Roses (aanvulling)
    "2048352":  ("🏆 First pressing", "Originele US Geffen-persing 1991 (GEF 24420) — dit IS de first pressing van Use Your Illusion II. 2LP met bedrukte binnenhoesjes. Parental advisory sticker. 4.252 have / 3.227 want."),
    "25128898": ("🎵 Luisterversie", "EU Geffen/UMe 2LP 180g reissue 2022 (00602445117314) — geremasterd gatefold reissue, ideaal voor dagelijks afspelen."),
    # Madness
    "370989":  ("🎵 Luisterversie", "Originele UK Stiff Records-persing 1982 (HIT-TV1) — de standaard UK-persing van de compilatie Complete Madness. CBS-pressing, gatefold hoes. Bevat House Of Fun, Baggy Trousers en It Must Be Love."),
    "4731834": ("🏆 Collector-editie", "UK Let Them Eat Vinyl reissue 2013 (LETV079LP) — gelimiteerde 2LP-reissue op rood vinyl van Complete Madness. Hoge want-ratio (56%) — zeldzamer dan het origineel."),
    "401145":  ("🏆 First pressing", "Originele UK Stiff Records-persing 1982 (SEEZ 46) — dit IS de first pressing van The Rise & Fall. Gatefold met embossed bandnaam op de voorkant. CBS-pressing. Bevat Our House en Tomorrow's (Just Another Day)."),
    "378607":  ("🎵 Luisterversie", "West-Duitsland Stiff Records-persing 1982 (6.25422 / SEEZ 46) — gatefold, meest beschikbare versie (2902 have), ideaal voor dagelijks afspelen."),
    # ABBA
    "380614":   ("🏆 First pressing", "Originele Zweedse Polar-persing 1980 (POLS 322) — dit IS de first pressing van Super Trouper. Hard-papieren binnenhoesje met teksten. Locked groove aan het einde van The Way Old Friends Do. Bevat Super Trouper en The Winner Takes It All."),
    "27884982": ("🎵 Luisterversie", "EU Polar 180g reissue 2022 (POLS 322 / 0602445509089) — geremasterd, ideaal voor dagelijks afspelen."),
    "8688135":  ("🏆 First pressing", "Originele Zweedse Polar-persing 1979 (POLS 292) — dit IS de first pressing van Voulez-Vous. Bevat Voulez-Vous, Chiquitita, Does Your Mother Know en Gimme! Gimme! Gimme!"),
    "3105488":  ("🎵 Luisterversie", "EU Polar 180g reissue 2011 (POLS 292) — geremasterd, ideaal voor dagelijks afspelen."),
    "441165":   ("🏆 First pressing", "Originele Zweedse Polar-persing 1976 (POLS 272) — dit IS de first pressing van Arrival. Uitzonderlijk gezocht: 16.085 have / 1.605 want. Bevat Dancing Queen, Fernando en Money Money Money."),
    "27888051": ("🎵 Luisterversie", "EU Polar reissue 2022 (POLS 272 / 0602445509126) — geremasterd, ideaal voor dagelijks afspelen."),
    "20031742": ("🏆 First pressing", "Originele Zweedse Polar-persing 1975 (POLS 262) — dit IS de first pressing van ABBA. Bevat I Do I Do I Do I Do I Do, S.O.S. en Mamma Mia."),
    "3105226":  ("🎵 Luisterversie", "EU Polar 180g reissue 2011 (POLS 262 / 00602527346496) — geremasterd, ideaal voor dagelijks afspelen."),
    "4475809":  ("🏆 First pressing", "Originele Zweedse Polar-persing 1977 (POLS 282) — dit IS de first pressing van The Album. Bevat Take A Chance On Me, The Name Of The Game en Eagle."),
    "3102070":  ("🎵 Luisterversie", "EU Polar 180g reissue 2011 (POLS 282 / 00602527346519) — geremasterd, inclusief download-voucher op vroege exemplaren."),
    "9535494":  ("🏆 First pressing", "Originele Zweedse Polar-persing 1981 (POLS 342) — dit IS de first pressing van The Visitors. Locked groove aan het einde van Like An Angel Passing Through My Room. Bevat One Of Us en Head Over Heels."),
    "3102140":  ("🎵 Luisterversie", "EU Polar 180g reissue 2011 (POLS 342) — geremasterd, ideaal voor dagelijks afspelen. Zonder locked groove."),
    # 10cc
    "3386133": ("🏆 First pressing", "Originele UK Mercury-persing 1978 (9102 503) — dit IS de first pressing van Bloody Tourists. Gatefold met picture labels en bedrukt binnenhoesje. Bevat Dreadlock Holiday."),
    "27084564":("🎵 Luisterversie", "UK UMC/Mercury reissue 2023 (UMCLP017) — 180g gatefold reissue, ideaal voor dagelijks afspelen."),
    "1615490": ("🏆 First pressing", "Originele UK Mercury-persing 1977 (9102 502) — dit IS de first pressing van Deceptive Bends. Gatefold met bedrukt binnenhoesje. Bevat The Things We Do For Love en Good Morning Judge."),
    "26309384":("🎵 Luisterversie", "UK/EU/US Mercury reissue 2023 (UMCLP016) — gatefold reissue, ideaal voor dagelijks afspelen."),
    # Rage Against the Machine
    "367339":  ("🏆 First pressing", "Originele US Epic-persing 1992 (Z 52959) — dit IS de first pressing van het zelfgetitelde debuutalbum. LP. Bevat Killing in the Name, Bombtrack en Wake Up. 7.091 have / 11.394 want — extreem gezocht."),
    "4073023": ("🎵 Luisterversie", "US Epic/Legacy 180g reissue 2012 (88725470451) — 20th Anniversary edition, geremasterd. Meest verspreide versie: 31.797 have. Ideaal voor dagelijks afspelen."),
    # Drake
    "3294598":  ("🏆 First pressing", "Originele US Young Money/Cash Money-persing 2011 (B0016280-01) — dit IS de first pressing van Take Care. 2LP gatefold. Bevat Marvins Room, Crew Love en Take Care feat. Rihanna. 16.061 have / 5.930 want."),
    "21976249": ("🎵 Luisterversie", "US Young Money/Cash Money reissue 2021 (B0016280-01) — 2LP gatefold heruitgave, ideaal voor dagelijks afspelen."),
    "9258657":  ("🏆 First pressing", "Officiële eerste US vinyl-persing 2016 (B0025237-01, Young Money/Republic) — het mixtape verscheen digitaal in 2015, vinyl pas in 2016. 6.299 have / 3.019 want."),
    "9247160":  ("🎵 EU-persing", "EU Young Money/Republic vinyl-persing 2016 (0602547973450) — gelijktijdig uitgebracht, EU-geperst via GZ Media, iets meer beschikbaar."),
    "9258642":  ("🏆 First pressing", "Originele US Young Money/Cash Money-persing 2016 (B0025236-01) — dit IS de first pressing van Views. 2LP. Bevat One Dance, Hotline Bling en Too Good feat. Rihanna. 9.004 have / 3.156 want."),
    "26904353": ("🎵 Luisterversie", "US Young Money/Cash Money reissue 2022 (B0025236-01) — 2LP heruitgave, ideaal voor dagelijks afspelen."),
    "26783426": ("🎵 Enige officiële vinyl", "US OVO/Young Money/UMe reissue 2023 (B0036101-01) — eerste en enige officiële vinyl-persing van More Life. Origineel digitaal uitgebracht in 2017. 2LP. Opvallend: 117 have / 1.656 want — uitzonderlijk hoge vraag."),
    "12802012": ("🏆 First pressing", "Originele US Young Money/Cash Money-persing 2018 (B0029103-01) — dit IS de first pressing van Scorpion. 4LP gatefold, kant A-D (rap) en E-H (R&B/pop). Bevat God's Plan, In My Feelings en Nice For What. 5.633 have / 1.967 want."),
    "12800480": ("🎵 EU-persing", "EU Young Money/Cash Money-persing 2018 (00602567874942) — gelijktijdige EU-persing, 4LP gatefold, iets meer beschikbaar voor dagelijks afspelen."),
    # J. Cole
    "6736792":  ("🏆 First pressing", "Originele US Roc Nation/Columbia-persing 2015 (88875 05698 1) — eerste vinyl-persing van 2014 Forest Hills Drive. 2LP gatefold. Bevat No Role Modelz, Love Yourz en Wet Dreamz. 20.423 have / 7.319 want — een van de meest verspreide hip-hop vinylalbums van de jaren 2010."),
    "27356088": ("🎵 Luisterversie", "US Interscope reissue 2023 (B0037320-01) — 2LP gatefold heruitgave, ideaal voor dagelijks afspelen."),
    "13377344": ("🏆 First pressing US", "Originele US Dreamville/Roc Nation/Interscope-persing 2018 (B0028571-01) — dit IS de US first pressing van KOD. 2LP gatefold. Bevat KOD, ATM en FRIENDS. 5.300 have / 1.260 want."),
    "12308370": ("🎵 EU-persing", "Originele EU Dreamville/Roc Nation/Interscope-persing 2018 (00810760032230) — gelijktijdig geperst, 2LP gatefold, iets meer beschikbaar. 5.798 have / 1.595 want."),
    "20020801": ("🏆 First pressing", "Originele US Dreamville/Roc Nation/Interscope-persing 2021 (B0034081-01) — dit IS de first pressing van The Off-Season. 2LP. 6.162 have / 1.225 want."),
    "22026808": ("🎵 Limited Edition blauw", "US/EU Dreamville/Roc Nation/Interscope Limited Edition 2022 (00810061165248) — 2LP op blauw vinyl, gelimiteerde variant van The Off-Season."),
    # Young Thug
    "9480756":  ("🏆 VMP original", "Originele US 300 Entertainment/Atlantic/VMP Club Edition 2016 (557768-1) — eerste vinyl-persing van Jeffery, exclusief via Vinyl Me Please. LP op blauw/wit marmer vinyl, genummerd. 862 have / 2.329 want — extreem gezocht."),
    "30428033": ("🎵 RSD reissue", "Worldwide 300 Entertainment/Atlantic Record Store Day 2024 (075678613456) — LP op blauw galaxy vinyl. RSD-heruitgave, ruimer beschikbaar. 3.466 have."),
    "18582520": ("🎵 VMP persing", "US Atlantic/YSL/300/VMP 2021 (624959-1) — eerste en enige officiële vinyl-persing van So Much Fun. 2LP op groen translucent vinyl, exclusief via Vinyl Me Please. 3.359 have / 2.415 want."),
    # JACKBOYS
    "15227004": ("🏆 US-persing", "Originele US Cactus Jack/Epic-persing 2020 (19439748411) — LP van de JACKBOYS-compilatie (Travis Scott). Bevat JACKBOYS, Out West feat. Young Thug en GATTI. 2.656 have / 531 want."),
    "16211818": ("🎵 EU-persing", "Originele EU Cactus Jack/Epic-persing 2020 (19439748411) — identieke EU-persing, iets meer beschikbaar (4.133 have). Ideaal voor dagelijks afspelen."),
    # Metro Boomin
    "26608835": ("🏆 Target exclusief", "US Boominati/Republic Target exclusive 2023 (B0037189-01) — beperkte editie van Heroes & Villains, exclusief via Target. LP. 1.685 have / 427 want."),
    "26584355": ("🎵 Standaard persing", "US Boominati/Republic-persing 2023 (B0037188-01) — standaard vinyl-persing van Heroes & Villains. LP. Bevat Superhero feat. Future & Chris Brown en Creepin' feat. 21 Savage & The Weeknd. 6.397 have / 2.146 want."),
    "13053315": ("🏆 First pressing", "Originele US Republic/Boominati-persing 2018 (B0029506-01) — dit IS de first pressing van Not All Heroes Wear Capes. LP. Bevat Space Cadet feat. Gunna en Overdue feat. Travis Scott. 2.177 have / 1.802 want."),
    "13208577": ("🎵 EU-persing", "EU Republic/Boominati-persing 2019 (00602577305603) — EU-persing van Not All Heroes Wear Capes. LP. 975 have / 689 want."),
    # Coldplay
    "484030":   ("🏆 First pressing", "Originele EU Parlophone/EMI-persing 2000 (7243 5 27783 1 7) — dit IS de first pressing van Parachutes. LP. Bevat Yellow, Shiver en Trouble. 15.286 have / 6.901 want."),
    "16231042": ("🎵 Luisterversie", "EU Parlophone/Warner reissue 2020 (0190295182502) — LP op geel translucent vinyl. Populairste reissue: 8.856 have. Ideaal voor dagelijks afspelen."),
    "703741":   ("🏆 First pressing", "Originele EU Parlophone-persing 2002 (7243 5 40504 1 1) — dit IS de first pressing van A Rush of Blood to the Head. LP 180g. Bevat The Scientist, Clocks en In My Place. 8.839 have / 4.264 want."),
    "7266689":  ("🎵 Luisterversie", "EU Parlophone reissue 2013 (7243 5 40504 1 1) — LP 180g heruitgave. Meest verspreide versie: 16.364 have. Ideaal voor dagelijks afspelen."),
    "1044164":  ("🏆 First pressing", "Originele EU Parlophone-persing 2005 (7243 4 74786 1 1) — dit IS de first pressing van X&Y. LP gatefold. Bevat Speed of Sound, Fix You en Talk. 6.864 have / 2.896 want."),
    "10039232": ("🎵 Luisterversie", "EU Parlophone reissue 2016 (07243 474786 1 1) — LP heruitgave van X&Y. 5.313 have. Ideaal voor dagelijks afspelen."),
    "5699282":  ("🏆 First pressing EU", "Originele EU Parlophone/Warner-persing 2014 (825646298815) — dit IS de EU first pressing van Ghost Stories. LP 180g gatefold. Bevat A Sky Full of Stars en Magic. 12.577 have / 2.075 want."),
    "5709533":  ("🎵 US-persing", "Originele US Atlantic/Parlophone-persing 2014 (542279-1) — gelijktijdige US-persing van Ghost Stories. LP. 5.260 have / 1.231 want."),
    # Golden Earring
    "589850":   ("🏆 First pressing NL", "Originele Nederlandse Polydor-persing 1973 (2925 017) — dit IS de NL first pressing van Moontan. LP gatefold. Bevat Radar Love. 2.114 have / 760 want."),
    "22207354": ("🎵 Luisterversie", "EU Music On Vinyl reissue 2022 (MOVLP3000) — 2LP geremasterd op clear vinyl gatefold. Ideaal voor dagelijks afspelen."),
    # Toto
    "386005":   ("🏆 First pressing EU", "Originele EU CBS-persing 1982 (CBS 85529) — dit IS de EU first pressing van Toto IV. LP. Bevat Africa en Rosanna. Meest verspreide originele persing: 19.266 have / 2.201 want."),
    "16132613": ("🎵 Luisterversie", "EU Columbia geremasterde reissue 2020 (19075801121) — LP. 4.435 have. Ideaal voor dagelijks afspelen."),
    "1464270":  ("🏆 First pressing US", "Originele US Columbia-persing 1978 (JC 35317) — dit IS de US first pressing van het debuutalbum Toto. LP stereo. Bevat Hold the Line en I'll Supply the Love. 14.523 have / 1.745 want."),
    "693037":   ("🎵 EU-persing", "Originele EU CBS-persing 1978 (CBS 83148) — gelijktijdige EU-persing van het debuutalbum Toto. LP stereo. 6.792 have / 811 want."),
    # Racoon
    "3033070":  ("🏆 First pressing", "Nederlandse PIAS-persing 2011 (PIASNL0026CLPCD) — eerste vinyl-persing van Liverpool Rain. LP+CD. 365 have / 127 want."),
    "10174337": ("🎵 Wit vinyl reissue", "Nederlandse PIAS-heruitgave 2017 (PIASNL0026CLPCD) — Liverpool Rain op wit vinyl. LP+CD. 221 have / 69 want."),
    "22336765": ("🎵 Vinyl reissue", "Nederlandse PIAS beperkte editie 2022 — eerste vinyl-persing van Another Day (2005 studioalbum). LP."),
    "6647916":  ("🎵 Officiële vinyl", "Nederlandse PIAS-persing 2015 (944.A174.010) — officiële vinyl-persing van All in Good Time. LP+CD. 500 have / 65 want."),
    "20619157": ("🏆 First pressing", "Nederlandse Sony Music-persing 2021 (19439887531) — eerste vinyl-persing van Spijt Is Iets Voor Later. LP+CD. 1.037 have / 74 want."),
    "20433880": ("🎵 EU clear vinyl", "EU Sony Music-persing 2021 (19439887541) — Spijt Is Iets Voor Later op clear vinyl. LP+CD. 758 have / 105 want."),
    "22785002": ("🏆 Artone Sessions", "Nederlandse Sony Music Artone Sessions LP 2022 (19439976911) — speciale akoestische versie op bruin vinyl. Bevat OCEAAN voor het eerst op vinyl! 868 have / 79 want."),
    # BLØF
    "26234072": ("🎵 Vinyl reissue", "EU Music On Vinyl reissue 2023 (MOVLP3301) — eerste vinyl-persing van Boven (1999 origineel). 2LP 180g gatefold. 216 have / 49 want."),
    "10320776": ("🏆 Beperkt geel vinyl", "Nederlandse Altijd Wakker beperkte genummerde editie 2017 (97205) — Aan op geel vinyl. Eerste vinyl-persing van het album. 2LP. 274 have / 81 want."),
    "11745367": ("🎵 Standaard persing", "Benelux Altijd Wakker-persing 2017 (97205) — standaard zwart vinyl persing van Aan. 2LP. 108 have / 49 want."),
    "27135846": ("🎵 Vinyl reissue", "Nederlandse Music On Vinyl beperkte editie 2023 (MOVLP3195) — eerste vinyl-persing van Blauwe Ruis (2002 origineel). LP 180g blauw transparant vinyl, 500 exemplaren. 126 have / 25 want."),
    "26449052": ("🎵 Vinyl reissue", "Nederlandse Music On Vinyl beperkte genummerde editie 2023 (MOVLP3302) — eerste vinyl-persing van Watermakers (2000 origineel). 2LP zilver vinyl. 230 have / 39 want."),
    # Janse Bagge Bend (aanvulling)
    "2267945":  ("🎵 Debuut LP", "Nederlandse Sky/Marlstone-persing 1983 (SKY 21048 SL) — originele vinyl-persing van het debuutalbum Flazjelêttentaere. LP. 278 have / 38 want."),
    # De Dijk
    "2084461":  ("🎵 Officiële vinyl", "Benelux Dureco-persing 1982 (88.053) — originele vinyl-persing van het zelfgetitelde debuutalbum De Dijk. LP. Bevat Bloedend Hart. 628 have / 90 want."),
    "3469887":  ("🎵 Officiële vinyl", "Nederlandse Sky-persing 1985 (TLP 19081) — originele vinyl-persing van Elke Dag Een Nieuwe Hoed. LP. Bevat Groot Hart. 258 have / 128 want."),
    "2375621":  ("🏆 First pressing", "Originele Nederlandse Mercury-persing 1987 (832 637-1) — dit IS de first pressing van Wakker in een Vreemde Wereld. LP. Bevat Dansen op de Vulkaan en Mag Het Licht Uit. 715 have / 151 want."),
    "30376355": ("🎵 Luisterversie", "Nederlandse Universal Music reissue 2024 (650 170-4) — heruitgave van Wakker in een Vreemde Wereld. LP. Ideaal voor dagelijks afspelen."),
    "1265189":  ("🏆 First pressing", "Originele Nederlandse Mercury/Phonogram-persing 1989 (836 985-1) — dit IS de first pressing van Niemand in de Stad. LP. Bevat Nergens Goed Voor en Ik Kan Het Niet Alleen. 755 have / 217 want."),
    "21119311": ("🎵 Luisterversie", "EU Music On Vinyl reissue 2021 (MOVLP619) — Niemand in de Stad op geel vinyl, gelimiteerd. LP. 194 have / 40 want."),
    "22978889": ("🎵 Vinyl reissue", "Nederlandse Music On Vinyl RSD reissue 2022 (MOVLP3032) — eerste vinyl-persing van De Blauwe Schuit (1994 origineel). LP blauw transparant vinyl. Bevat Als Ze Er Niet Is. 371 have / 51 want."),
    # Green Day (aanvulling)
    "2103788":  ("🏆 First pressing", "Originele US Reprise-persing 1994 (1-45529) — dit IS de first pressing van Dookie. LP. Bevat Basket Case, When I Come Around en Longview. 2.843 have / 5.475 want — vraag ruim groter dan aanbod."),
    "1770697":  ("🎵 Luisterversie", "US Reprise reissue 2009 (468284-1) — meest beschikbare versie van Dookie: 27.540 have. Ideaal voor dagelijks afspelen."),
    "1297507":  ("🏆 First pressing", "Originele US Reprise-persing 1995 (1-46046) — dit IS de first pressing van Insomniac. LP. Bevat Brain Stew, Geek Stink Breath en Stuck With Me. 2.951 have / 1.959 want."),
    "17885617": ("🎵 Luisterversie", "US/EU Reprise 25th Anniversary reissue 2021 (093624884576) — LP deluxe heruitgave van Insomniac. 8.248 have. Ideaal voor dagelijks afspelen."),
    "1220700":  ("🏆 First pressing", "Originele Duitse Reprise-persing 1997 (9362-46794-1) — eerste Europese vinyl-persing van Nimrod. LP. Bevat Good Riddance (Time of Your Life), Hitchin' a Ride en Nice Guys Finish Last. 454 have / 1.501 want — vraag meer dan 3× het have."),
    "18825352": ("🎵 Luisterversie", "US/EU Reprise reissue 2021 (093624912231) — LP heruitgave van Nimrod. 4.866 have. Ideaal voor dagelijks afspelen."),
    "1827139":  ("🏆 First pressing US", "Originele US Reprise-persing 2009 (517153-1) — dit IS de US first pressing van 21st Century Breakdown. 2LP 180g gatefold. Bevat Know Your Enemy en 21 Guns. 3.549 have / 1.882 want."),
    "15703648": ("🎵 Luisterversie", "US Reprise/Warner reissue 2019 (093624978534) — 2LP heruitgave van 21st Century Breakdown. 2.298 have. Ideaal voor dagelijks afspelen."),
    # Bon Jovi
    "1443701":  ("🏆 First pressing US", "Originele US Mercury-persing 1986 (830 264-1 M-1) — dit IS de US first pressing van Slippery When Wet. LP. Bevat Livin' on a Prayer, You Give Love a Bad Name en Wanted Dead or Alive. 11.242 have / 1.276 want."),
    "9307146":  ("🎵 Luisterversie", "EU Mercury/Back To Black 180g reissue 2016 — Slippery When Wet op 180g vinyl. 5.428 have. Ideaal voor dagelijks afspelen."),
    "17650882": ("🏆 US-persing", "US Island Records 180g gatefold 2014 (B0021972-01) — eerste US vinyl-persing van Crush. 2LP. Bevat It's My Life en Thank You for Loving Me. Origineel (2000) was CD-only."),
    "9299636":  ("🎵 EU-persing", "EU Island Records 180g gatefold reissue 2016 (06025 470 299-4) — 2LP gatefold van Crush. 915 have / 615 want. Meest beschikbare vinyl versie."),
    # Doe Maar (aanvulling)
    "401816":   ("🏆 First pressing", "Originele Nederlandse Sky/Foon-persing 1983 (24000 SL) — dit IS de first pressing van 4US. LP. Bevat De Bom en Doris Day. 6.394 have / 366 want — meest verspreide Doe Maar LP."),
    "23005994": ("🎵 Luisterversie", "Nederlandse Music On Vinyl reissue 2022 (MOVLP2298) — LP heruitgave van 4US. 368 have. Ideaal voor dagelijks afspelen."),
    # Fugees
    "361323":   ("🏆 First pressing", "Originele US Columbia/Ruffhouse-persing 1996 (C2 67147) — dit IS de first pressing van The Score. 2LP. Bevat Killing Me Softly, Ready or Not en Fu-Gee-La. 4.557 have / 6.863 want — vraag ruim hoger dan aanbod."),
    "2691711":  ("🎵 Luisterversie", "EU Music On Vinyl 180g reissue 2010 (MOVLP068) — 2LP 180g heruitgave van The Score. 5.585 have. Ideaal voor dagelijks afspelen."),
    # André Hazes
    "952645":   ("🏆 First pressing", "Originele Nederlandse EMI-persing 1981 (1A 064-26677) — dit IS de first pressing van Gewoon André. LP. Bevat Zij Gelooft In Mij en Bloed, Zweet en Tranen. 4.487 have / 193 want — meest verspreide André Hazes LP."),
    "20794702": ("🎵 Luisterversie", "Nederlandse Music On Vinyl reissue 2021 (MOVLP2884) — Gewoon André op rood vinyl, gelimiteerd. LP. 144 have."),
    "3100518":  ("🏆 First pressing", "Originele Nederlandse EMI-persing 1990 (7949391) — dit IS de first pressing van Kleine Jongen. LP. Bevat Kleine Jongen en Caruso. 187 have / 153 want."),
    "26658734": ("🎵 Luisterversie", "Nederlandse Music On Vinyl reissue 2023 (MOVLP3362) — Kleine Jongen op groen vinyl, gelimiteerd. LP. 78 have."),
    "28648639": ("🎵 Vinyl reissue", "Nederlandse Music On Vinyl reissue 2023 (MOVLP3546) — eerste vinyl-persing van Strijdlustig (2002 origineel). LP zilver vinyl. 91 have / 31 want."),
    "26918048": ("🎵 Vinyl reissue", "Nederlandse Music On Vinyl reissue 2023 (MOVLP3431) — eerste vinyl-persing van Met Heel Mijn Hart (1993 origineel). LP geel vinyl. 110 have / 29 want."),
    "1201387":  ("🏆 First pressing", "Originele Nederlandse EMI/EMI-Bovema-persing 1983 (1A 068-1270201) — dit IS de first pressing van Voor Jou. LP. Bevat Zeg Maar Niets Meer en Bij Jou. 1.695 have / 79 want."),
    "24606677": ("🎵 Luisterversie", "Nederlandse Music On Vinyl reissue 2022 (MOVLP3134) — Voor Jou op oranje vinyl, gelimiteerd. LP. 99 have."),
    "25287358": ("🏆 Origineel 1977", "Originele Nederlandse Philips-persing 1977 (6410 140) — eerste persing van Zo Is Het Leven met De Vlieger op zijde B. LP. Zeldzaam: 15 have / 8 want."),
    "2693688":  ("🎵 Meest beschikbaar", "Nederlandse Philips/Gouden Molen repress 1981 (6423 412) — repress van Zo Is Het Leven. LP. Bevat De Vlieger. Meest verspreide versie: 570 have / 22 want."),
    # Pop Smoke
    "16578819": ("🏆 First pressing", "Originele US Victor Victor/Republic-persing 2020 (B0032626-01) — dit IS de first pressing van Shoot for the Stars Aim for the Moon. 2LP gatefold. Bevat Welcome to the Party, For the Night en Dior. Postuum album afgewerkt door 50 Cent. 1.654 have / 442 want."),
    "16938279": ("🎵 EU-persing", "EU Victor Victor/Republic-persing 2020 (00602507306465) — gelijktijdige EU-persing, 2LP. Meest verspreide versie: 1.695 have. Ideaal voor dagelijks afspelen."),
    "31489625": ("🎵 Officiële vinyl", "US Victor Victor/Republic 5th Anniversary reissue 2024 (602465755855) — eerste en enige officiële vinyl-persing van Meet the Woo V.1. LP op blauw vinyl."),
    "16026374": ("🎵 Officiële vinyl", "US Victor Victor/Republic-persing 2020 (B0032506-01) — officiële vinyl-persing van Meet the Woo V.2. 2LP Deluxe Edition, 16 tracks. Bevat Shake the Room, Christopher Walking en Welcome to the Party (Remix). 1.946 have / 1.124 want."),
    # Travis Scott
    "31843496": ("🏆 Beperkt rood vinyl", "US Cactus Jack/Epic reissue 2024 (PRES BIZ 011 847 088) — eerste officiële vinyl-persing van Days Before Rodeo op rood vinyl, Deluxe Edition. Oorspronkelijk digitaal mixtape (2014), vinyl pas in 2024. 4.254 have / 1.295 want."),
    "31828724": ("🎵 Standaard persing", "US Cactus Jack/Epic reissue 2024 (TSBR-VR07) — standaard zwart vinyl persing van Days Before Rodeo. Zelfde 2024-batch als de rode variant, iets meer beschikbaar."),
    "7435327":  ("🏆 First pressing", "Originele US Epic/Grand Hustle-persing 2015 (88875065201) — dit IS de first pressing van Rodeo. 2LP 150g met download card. Bevat Antidote, 3500 en Oh My Dis Side. 19.335 have / 6.924 want — meest verspreide Travis-album op vinyl."),
    "22348948": ("🎵 Luisterversie", "US/EU Epic/Grand Hustle reissue 2022 (88875065201) — 2LP heruitgave, geperst door GZ Media. 8.197 have. Laagste vraagprijs ~$18. Ideaal voor dagelijks afspelen."),
    "9403008":  ("🏆 First pressing US", "Originele US Epic/Grand Hustle-persing 2016 (88985377711) — dit IS de US first pressing van Birds in the Trap Sing McKnight. 2LP. Bevat goosebumps, through the late night en way back feat. Frank Ocean. 13.417 have / 3.648 want."),
    "9402676":  ("🎵 EU-persing", "Originele EU Epic/Grand Hustle-persing 2016 (88985377711) — gelijktijdige EU-persing, 2LP. 7.224 have / 1.576 want. Ideaal voor dagelijks afspelen."),
    "27840414": ("🏆 First pressing", "Originele US Epic/Cactus Jack-persing 2023 (1 96588 15041 8) — dit IS de first pressing van Utopia. 2LP. Bevat MY EYES, FE!N feat. Playboi Carti en MODERN JAM. 10.925 have / 2.115 want."),
    "29085418": ("🎵 EU-persing", "EU Epic/Cactus Jack reissue 2023 (1 96588 46031) — EU-persing van Utopia, 2LP. 4.985 have. Ideaal voor dagelijks afspelen."),
    # Migos
    "9896622":  ("🏆 Limited Edition goud", "US QC/300 Entertainment/Atlantic beperkte editie — Culture op goud translucent vinyl. Extreem gezocht: 448 have / 1.094 want. Zeldzamer dan de standaard zwarte persing."),
    "10271326": ("🎵 Standaard persing", "US QC/300 Entertainment/Atlantic-persing 2017 (560566-1) — standaard zwart vinyl persing van Culture. 2LP. Bevat Bad and Boujee, T-Shirt en Slippery. Opvallend: 1.606 have / 1.721 want — vraag overtreft aanbod."),
    "12270746": ("🏆 Limited Edition rood", "US QC/Motown/Capitol beperkte editie 2018 (B002813601) — Culture II op rood vinyl, tri-fold gatefold. 3LP. 581 have / 587 want — collector item."),
    "12274680": ("🎵 Standaard persing", "US QC/Motown/Capitol-persing 2018 (B002813501) — standaard zwart vinyl persing van Culture II. 3LP. 1.147 have / 418 want. Ideaal voor dagelijks afspelen."),
    "23055125": ("🏆 Standaard persing", "US QC/Motown/UMG-persing 2022 (B003486901) — officiële vinyl-persing van Culture III (digitaal 2021). 2LP. 1.414 have / 168 want."),
    "23100230": ("🎵 Walmart exclusief wit", "US QC/Motown/Walmart exclusieve persing 2022 (B003486901) — Culture III op wit vinyl. Walmart exclusive limited edition."),
    # 2Pac
    "238369":   ("🏆 First pressing", "Originele US Death Row/Interscope-persing 1996 (314-524 204-1) — dit IS de first pressing van All Eyez on Me. 4LP gatefold. Bevat California Love (Remix), Ambitionz Az a Ridah en 2 Of Amerikaz Most Wanted. 2.902 have / 7.474 want — vraag meer dan dubbel zo hoog als aanbod."),
    "25119211": ("🎵 Luisterversie", "US/EU Interscope/Amaru/UMe 180g reissue 2022 (00602448276261) — 4LP 180g gatefold, gemasterd door Brian Gardner, geperst door GZ Media. 5.200 have. Beste moderne persing: 4.8/5 door fans beoordeeld."),
    "960025":   ("🏆 First pressing", "Originele US Death Row/Interscope-persing 1998 (INT4-90301) — dit IS de first pressing van Greatest Hits. 4LP gatefold. Bevat California Love, Changes, Hit 'Em Up en Hail Mary. 1.904 have / 1.776 want."),
    "12824597": ("🎵 Luisterversie", "US Death Row/Interscope/UMG reissue 2018 (B0029039-01) — meest verspreide persing van Greatest Hits. 4LP gatefold heruitgave. 2.603 have. Ideaal voor dagelijks afspelen."),
    # Marvin Gaye & Tammi Terrell
    "16254322": ("🏆 First pressing (stereo)", "Originele US Tamla-persing 1967 (TS 277) — dit IS de stereo first pressing van United. Indianapolis pressing, meest gezochte variant. Bevat Ain't No Mountain High Enough."),
    "3925863":  ("🎵 Audiofiele versie", "Speakers Corner 180g reissue Duitsland 2009 — gemasterd door Emil Berliner Studios, geperst door Pallas. Ideaal voor dagelijks afspelen."),
    # Ramses Shaffy
    "735833":   ("🏆 First pressing", "Originele Nederlandse Philips-persing 1978 (6423 112) — dit IS de first pressing van Dag En Nacht. Bevat Laat Me."),
    # UB40
    "15311529": ("🏆 First pressing", "Originele UK DEP International/Virgin-persing 1983 (LP DEP 5) — dit IS de first pressing van Labour of Love. Bevat Red Red Wine."),
    "6848587":  ("🎵 Luisterversie", "EU Virgin EMI/UMC Back To Black 2LP 180g reissue 2015 (00602547161116) — inclusief download-voucher. Ideaal voor dagelijks afspelen."),
    "4634884":  ("🏆 First pressing", "Originele UK DEP International-persing 1989 (LPDEP 14) — dit IS de first pressing van Labour of Love II. Gemasterd bij Abbey Road. Bevat Kingston Town."),
    "634218":   ("🏆 First pressing", "Originele UK DEP International/Virgin-persing 1985 (LP DEP 10) — dit IS de first pressing van Baggariddim. Gatefold, 33+45 RPM. Bevat I Got You Babe feat. Chrissie Hynde."),
    "1201716":  ("🏆 First pressing", "Originele UK/EU DEP International/Virgin-persing 1993 (LPDEP 15) — dit IS de first pressing van Promises and Lies. Bevat (I Can't Help) Falling in Love with You."),
    # Sting
    "1066027":  ("🏆 First pressing UK/EU", "Originele UK/EU A&M-persing 1987 (AMA 6402) — dit IS de UK/EU first pressing van ...Nothing Like the Sun. 2LP. Bevat Englishman in New York."),
    "9124039":  ("🎵 Audiofiele versie", "EU A&M 2LP reissue 2016 (0082839391214) — nieuw gemasterd bij Abbey Road Studios, geperst in Duitsland. Beoordeeld 4.53/5 door fans. Ideaal voor dagelijks afspelen."),
    # Drukwerk
    "2689368":  ("🏆 First pressing", "Originele Nederlandse EMI-persing 1981 (1A 058-26650) — dit IS de first pressing van het debuutalbum Drukwerk. Bevat Je Loog Tegen Mij."),
    "2972649":  ("🏆 First pressing", "Originele Nederlandse EMI-persing 1982 (1A 068 26852) — dit IS de first pressing van Tweede Druk. Bevat Schijn 'n Lichtje Op Mij."),
    # Jay-Z & Kanye West
    "3351778":  ("🏆 Officieel vinyl (picture disc)", "US Roc-A-Fella/Island Def Jam 2LP picture disc 2012 (B0016010-01) — enige officiële vinyl-persing van Watch the Throne. Gouden gevouwen kruis-hoes (ontworpen door Givenchy). Lacquers gesneden door Ray Janos bij Sterling Sound. Bevat Niggas in Paris."),
    # Notorious B.I.G. (aanvulling)
    "268090":   ("🏆 First pressing", "Originele US Bad Boy/Arista-persing 1997 (78612-73011-1) — dit IS de first pressing van Life After Death. 3LP. Bevat Hypnotize en Mo Money Mo Problems. 'Recorded and manufactured prior to March 9, 1997.'"),
    "22556825": ("🎵 Luisterversie", "EU Bad Boy Entertainment 25th Anniversary 3LP reissue 2022 (R1 541302) — ideaal voor dagelijks afspelen."),
    # KISS
    "688267":   ("🏆 First pressing", "Originele US Casablanca-persing 1975 (NBLP 7016) — dit IS de first pressing van Dressed to Kill. PRC Richmond pressing. Bevat Rock and Roll All Nite."),
    "5557406":  ("🎵 Luisterversie", "EU Casablanca/Mercury/UMG 180g reissue 2014 (0602537727889) — inclusief MP3 download-voucher, geperst door GZ Media. Ideaal voor dagelijks afspelen."),
    "625331":   ("🏆 First pressing", "Originele US Casablanca-persing 1979 (NBLP 7152) — dit IS de first pressing van Dynasty. Santa Maria pressing. Inclusief 22×33\" poster. Bevat I Was Made for Lovin' You."),
    "6102279":  ("🎵 Luisterversie", "Duitsland Casablanca/Mercury 180g reissue 2014 (0602537770946) — inclusief poster en origineel binnenhoesje. Ideaal voor dagelijks afspelen."),
    # ZZ Top
    "840158":   ("🏆 First pressing", "Originele US London Records-persing 1973 (XPS 631) — dit IS de first pressing van Tres Hombres. LP gatefold, Terre Haute pressing. Bevat La Grange."),
    "2560004":  ("🎵 Audiofiele versie", "US Warner Bros. 180g reissue 2006 (274492) — gemasterd door Kevin Gray & Steve Hoffman bij AcousTech, geperst door RTI van originele analoge mastertapes. Beste moderne persing."),
    # Radiohead
    "339574":   ("🏆 First pressing", "Originele UK Parlophone-persing 1993 (PCS 7360) — dit IS de first pressing van Pablo Honey. 'COME TO FLORIDA' gegraveerd in runout. Bevat Creep."),
    "1635232":  ("🎵 Luisterversie", "US Capitol 180g reissue 2008 — 'Faithfully Restored' serie, geperst door Rainbo Records. Ideaal voor dagelijks afspelen."),
    # Foo Fighters
    "2070894":  ("🏆 First pressing", "Originele US Roswell/RCA-persing 2007 (88697 11516-1) — dit IS de first pressing van Echoes, Silence, Patience & Grace. 2LP, geperst door United Record Pressing. Bevat The Pretender."),
    "15388111": ("🎵 Luisterversie", "US Roswell/RCA/BMG reissue 2LP gatefold — Pallas USA pressing. Ideaal voor dagelijks afspelen."),
    # Lynyrd Skynyrd
    "2034523":  ("🏆 First pressing", "Originele US MCA/Sounds of the South-persing 1973 (MCA-363) — dit IS de first pressing van (Pronounced 'Leh-'nérd 'Skin-'nérd). LP gatefold, Gloversville pressing, gele labels. Bevat Free Bird."),
    "4179730":  ("🎵 Audiofiele versie", "US Mobile Fidelity Sound Lab 180g reissue 2013 (MFSL 1-400) — gelimiteerd genummerd, GAIN 2 Ultra Analog systeem, gemasterd van originele tapes. Beste moderne persing."),
    "1634156":  ("🏆 First pressing", "Originele US MCA/Sounds of the South-persing 1974 (MCA-413) — dit IS de first pressing van Second Helping. Pinckneyville pressing, gele labels. Bevat Sweet Home Alabama."),
    "2341640":  ("🎵 Luisterversie", "US MCA 180g reissue 2008 (MCA-1686) — Back to Black-serie, geremasterd. Ideaal voor dagelijks afspelen."),
    # Guns N' Roses (Use Your Illusion I)
    "2206780":  ("🏆 First pressing", "Originele US Geffen-persing 1991 (GEF-24415) — dit IS de first pressing van Use Your Illusion I. 2LP. Bevat November Rain."),
    "25133437": ("🎵 Luisterversie", "US/EU Geffen 2LP 180g reissue 2022 (00602445117307) — geremasterd door Ted Jensen bij Sterling Sound van 192kHz/24-bit transfers. Ideaal voor dagelijks afspelen."),
    # Limp Bizkit
    "3782742":  ("🏆 First pressing", "Originele US Interscope/Flip-persing 1999 (INT2-90335) — dit IS de first pressing van Significant Other. 2LP gatefold. Bevat Break Stuff."),
    "10821533": ("🎵 Luisterversie", "US Flip/Interscope/UMe 2LP gatefold reissue 2017 (B0026803-01) — 'First time on vinyl in U.S. since 1999.' Hand-gegraveerde runouts. Beoordeeld 4.64/5. Ideaal voor dagelijks afspelen."),
    # Stormzy
    "10996313": ("🏆 First pressing", "Originele UK #Merky Records-persing 2017 (MRKY001LP) — dit IS de first pressing van Gang Signs & Prayer. 2LP. 500 gesigneerde exemplaren via Stormzy's webshop. Lacquer gesneden door Shane McEnhill bij Finyl Tweek."),
    "15841340": ("🏆 First pressing", "Originele UK #Merky Records/Atlantic-persing 2020 (0190295403027) — eerste vinyl-persing van Heavy Is the Head. 2LP, 45 RPM voor betere geluidskwaliteit. Geperst door Optimal Media."),
    # Skepta
    "10305796": ("🏆 Eerste vinyl (rood)", "UK Boy Better Know-persing 2017 (BBKS004LP) — enige officiële vinyl LP van Konnichiwa. Rood vinyl, gelimiteerde editie. Lacquer gesneden door Finyl Tweek."),
    "13713464": ("🏆 First pressing", "Originele UK Boy Better Know-persing 2019 — dit IS de first pressing van Ignorance Is Bliss. 2LP gatefold met thermochromische hoes."),
    # Billy Joel
    "9603231":  ("🏆 First pressing", "Originele US Columbia-persing 1973 (KC 32544) — dit IS de first pressing van Piano Man. Santa Maria pressing."),
    "3333076":  ("🎵 Audiofiele versie", "US Mobile Fidelity Sound Lab 180g reissue 2011 (MFSL 1-349) — gelimiteerd genummerd, GAIN 2 Ultra Analog 180g, gemasterd van analoge mastertapes. Beste moderne persing."),
    # John Lennon
    "376138":   ("🏆 First pressing", "Originele US Apple Records-persing 1971 (SW 3379) — dit IS de first pressing van Imagine. Los Angeles pressing. Inclusief poster en twee ansichtkaarten. 13.609 have op Discogs."),
    "2590105":  ("🎵 Audiofiele versie", "US Mobile Fidelity Sound Lab 180g reissue 2003 (MFSL 1-277) — gelimiteerd genummerd, GAIN 2 Ultra Analog, geperst door RTI. Beoordeeld 4.53/5."),
    # The Cranberries
    "501415":   ("🏆 First pressing", "Originele UK/EU Island Records-persing 1994 (524050-1 / ILPS 8029) — dit IS de first pressing van No Need to Argue. LP gatefold met teksten. Bevat Zombie."),
    "6986271":  ("🎵 Luisterversie", "US Plain Recordings 180g gatefold reissue 2015 (PLAIN202) — geremasterd door Gary Hobish, geperst door Rainbo Records. Ideaal voor dagelijks afspelen."),
    # Pink Floyd
    "495681":   ("🏆 First pressing", "Originele US Columbia-persing 1979 (PC2 36183) — dit IS de first pressing van The Wall. 2LP, Pitman pressing. Bevat Another Brick in the Wall."),
    "8961614":  ("🎵 Audiofiele versie", "EU Pink Floyd Records 2LP 180g reissue 2016 (PFRLP11) — geremasterd door James Guthrie, Joel Plante & Bernie Grundman van originele analoge banden. Beoordeeld 4.78/5 door 3.933 fans (37.667 have) — de definitieve moderne persing."),
    # The Verve
    "445878":   ("🏆 First pressing", "Originele UK Hut Recordings/Virgin-persing 1997 (HUTLP 45) — dit IS de first pressing van Urban Hymns. 2LP. Bevat Bittersweet Symphony."),
    "9057753":  ("🎵 Luisterversie", "EU Virgin EMI 2LP 180g reissue 2016 (4787014) — lacquer gesneden door Matt Colton bij Alchemy, geperst door Optimal Media van originele halve-inch tapes. Ideaal voor dagelijks afspelen."),
    # R.E.M.
    "2024524":  ("🏆 First pressing", "Originele US Warner Bros.-persing 1991 (9 26496-1) — dit IS de first pressing van Out of Time. LP. Bevat Losing My Religion."),
    "9359884":  ("🎵 Luisterversie", "EU/US Concord/Warner 25th Anniversary 180g reissue 2016 — geremasterd van originele analoge masters. Ideaal voor dagelijks afspelen."),
    # Van Dik Hout
    "13488794": ("🎵 Eerste vinyl (jubileum)", "NL Polydor/Universal Music jubileum 2LP 180g blauw vinyl 2019 — eerste ooit op vinyl, gelimiteerd (1000 exemplaren, RSD). Bevat Stil in Mij."),
    # Bryan Adams
    "1469334":  ("🏆 First pressing", "Originele Canada A&M-persing 1984 (SP-5013) — dit IS de Canadian first pressing van Reckless. Bevat Summer of '69."),
    "6275103":  ("🎵 Luisterversie", "UK/EU A&M 30th Anniversary 2LP 180g reissue 2014 (3783059) — geremasterd. Ideaal voor dagelijks afspelen."),
    # Dire Straits
    "382417":   ("🏆 First pressing", "Originele UK Vertigo/Phonogram-persing 1985 (VERH 25) — dit IS de first pressing van Brothers in Arms. LP. Bevat Walk of Life."),
    "17896735": ("🎵 Audiofiele versie", "EU Vertigo/Universal 2LP 180g 45 RPM half-speed mastered reissue 2021 — gemasterd door Miles Showell bij Abbey Road van originele U-Matic master. Met echtheidsverklaring. Definitieve moderne persing."),
    # The Scene
    "864382":   ("🏆 First pressing", "Originele Nederlandse Phonogram-persing 1990 — dit IS de first pressing van Blauw. LP. Bevat Iedereen Is Van de Wereld."),
    "13670805": ("🎵 Luisterversie", "Nederlandse Music On Vinyl/Universal reissue 2019 — ideaal voor dagelijks afspelen."),
    # Louis Armstrong
    "4194515":  ("🏆 First pressing (stereo)", "Originele US ABC Records-persing 1968 (ABCS-650) — dit IS de stereo first pressing van What a Wonderful World. LP. Bevat What a Wonderful World."),
    # Nickelback
    "1982122":  ("🎵 Eerste vinyl persing", "EU Roadrunner Records LP reissue 2002 — eerste ooit op vinyl uitgebrachte persing van Silver Side Up (origineel 2001 CD-only). Bevat How You Remind Me."),
    "10533002": ("🎵 Luisterversie", "EU Roadrunner Records LP reissue 2017 — voor dagelijks afspelen."),
    "10518961": ("🎵 Eerste vinyl persing", "EU Roadrunner Records LP reissue 2017 — eerste en enige vinyl-persing van All the Right Reasons (origineel 2005 CD-only). Bevat Rockstar."),
    # Soft Cell
    "238877":   ("🏆 First pressing", "Originele UK Some Bizzare/Phonogram-persing 1981 (BIZL 1) — dit IS de first pressing van Non-Stop Erotic Cabaret. LP. Bevat Tainted Love."),
    "6063431":  ("🎵 Luisterversie", "EU Mercury/Universal 180g reissue 2014 — ideaal voor dagelijks afspelen."),
    # Depeche Mode
    "1217497":  ("🏆 First pressing", "Originele UK Mute Records-persing 1981 (STUMM 5) — dit IS de first pressing van Speak & Spell. LP, origineel Mute 'walking man'-logo. Bevat Just Can't Get Enough."),
    "8953011":  ("🎵 Luisterversie", "EU Sony Music/Mute 180g gatefold reissue 2016 — inclusief 12\"×12\" binnenhoesje replica. Ideaal voor dagelijks afspelen."),
    # Frank Sinatra
    "804778":   ("🏆 First pressing (mono)", "Originele US Reprise Records mono-persing 1966 (F 1020) — dit IS de mono first pressing van That's Life. Pitman pressing. Bevat That's Life."),
    "8212247":  ("🎵 Luisterversie", "EU/US Reprise/Sinatra Centennial 180g reissue 2016 — centennial heruitgavereeks. Ideaal voor dagelijks afspelen."),
    # Coolio
    "1410402":  ("🏆 First pressing US", "Originele US Tommy Boy-persing 1995 (TB 1141) — dit IS de US first pressing van Gangsta's Paradise. LP. Bevat Gangsta's Paradise feat. L.V."),
    "3480688":  ("🎵 EU-persing", "Originele EU Tommy Boy/Island/Polydor-persing 1995 — gelijktijdige Europese persing, meer beschikbaar. Ideaal voor dagelijks afspelen."),
    # Wheatus
    "14950419": ("🎵 Eerste vinyl (RSD)", "EU Wheatus Records RSD-persing 2020 — eerste ooit op vinyl uitgebrachte persing van het debuutalbum (origineel 2000 CD-only). LP turquoise 180g, gelimiteerd. Inclusief boekje en poster. Bevat Teenage Dirtbag."),
    # Eurythmics
    "66534":    ("🏆 First pressing", "Originele UK RCA-persing 1983 (RCALP 6063) — dit IS de first pressing van Sweet Dreams (Are Made of This). LP, inclusief gekleurd binnenhoesje met teksten. Bevat Sweet Dreams."),
    "11860171": ("🎵 Luisterversie", "EU RCA/Legacy/Sony 180g reissue 2018 — geremasterd van originele ½\" tapes. Ideaal voor dagelijks afspelen."),
}

# ─── ALBUM PAIRINGS ─────────────────────────────────────────────────────────────
# Elk tupel: (belegging_id, luister_id) — links=belegging, rechts=luisteren
RELEASE_PAIRS = [
    # Rock
    ("939519",   "6127871"),   # Oasis — Morning Glory
    ("517224",   "12864584"),  # Oasis — Definitely Maybe
    ("2334540",  "34257205"),  # Oasis — Time Flies (UK first / reissue 2025)
    ("375491",   "12042641"),  # RHCP — Blood Sugar Sex Magik
    ("14914560", "31323387"),  # RHCP — Californication
    ("420718",   "15276024"),  # RHCP — By The Way
    ("1629020",  "8519678"),   # RHCP — Stadium Arcadium
    ("7801798",  "14186441"),  # Beatles — Abbey Road
    ("612780",   "7541569"),   # Queen — A Night at the Opera
    ("23316872", "22048156"),  # Queen — A Day at the Races
    ("14031557", "3824539"),   # Queen — News of the World
    ("475606",   "4269045"),   # Queen — Jazz
    ("455954",   "446814"),    # Queen — The Game
    ("589920",   "505097"),    # Queen — Hot Space
    ("4732312",  "36148198"),  # Queen — The Works
    ("11967456", "25210735"),  # Queen — A Kind of Magic
    ("9452213",  "1203470"),   # Green Day — American Idiot
    ("397167",   "20298550"),  # The Killers — Hot Fuss
    ("1934367",  "9847048"),   # Eagles — Hotel California
    ("526351",   "3229870"),   # Fleetwood Mac — Rumours
    ("4065605",  "33831750"),  # Fleetwood Mac — Tango In The Night
    # Hard Rock / Metal
    ("1813006",  "3183667"),   # Nirvana — Nevermind
    ("400591",   "1949857"),   # AC/DC — Back in Black
    ("400587",   "2520300"),   # AC/DC — Highway to Hell
    ("383777",   "7492229"),   # Guns N' Roses — Appetite for Destruction
    ("381988",   "439599"),    # Metallica — Black Album (US ↔ EU)
    ("1549636",  "11118447"),  # Metallica — Master of Puppets
    ("534020",   "21054706"),  # Linkin Park — Hybrid Theory
    ("3336797",  "28403278"),  # Linkin Park — Meteora
    # Pop
    ("2911293",  "152946"),    # Michael Jackson — Thriller (Pitman ↔ EU)
    ("402227",   "22712903"),  # Doe Maar — Skunk
    ("382601",   "22494431"),  # Doe Maar — Doris Day
    # Soul / R&B
    ("34780535", "2848009"),   # Amy Winehouse — Back to Black
    # Reggae
    ("12927816", "4418438"),   # Bob Marley — Legend
    ("3660230",  "1862215"),   # Bob Marley — Exodus
    ("65845",    "746135"),    # Bob Marley — Rastaman Vibration
    # Hip-Hop
    ("317356",   "34578556"),  # Notorious B.I.G. — Ready to Die
    ("223127",   "7753999"),   # Mobb Deep — The Infamous
    ("3975953",  "30551209"),  # Kendrick Lamar — GKMC
    ("8814849",  "23398166"),  # Kendrick Lamar — TPAB
    ("10559651", "25683820"),  # Kendrick Lamar — DAMN.
    # The Police
    ("5305755",  "13549135"),  # The Police — Outlandos d'Amour
    ("11827033", "3363252"),   # The Police — Reggatta de Blanc
    ("3214829",  "31334893"),  # The Police — Synchronicity
    # Led Zeppelin
    ("2893139",  "22645229"),  # Led Zeppelin — IV
    # Rolling Stones
    ("7264539",  "1137475"),   # Rolling Stones — Beggars Banquet (stereo ↔ mono)
    ("765072",   "6003779"),   # Rolling Stones — Aftermath (US ↔ UK)
    ("468054",   "1931909"),   # Rolling Stones — Tattoo You (US ↔ UK)
    # 21 Savage
    ("10597886", "10752389"),  # 21 Savage — Savage Mode
    ("10873523", "13876606"),  # 21 Savage — Issa Album
    ("13318697", "15624365"),  # 21 Savage — I Am > I Was
    # 50 Cent
    ("485114",   "1198408"),   # 50 Cent — Get Rich or Die Tryin'
    ("598810",   "8954977"),   # 50 Cent — The Massacre
    # U2
    ("10456142", "10395824"),  # U2 — The Joshua Tree
    ("676619",   "11846916"),  # U2 — All That You Can't Leave Behind
    # Rowwen Hèze
    ("15500018", "26032708"),  # Rowwen Hèze — Blieve Loepe
    # Madness
    ("4731834",  "370989"),    # Madness — Complete Madness
    ("401145",   "378607"),    # Madness — The Rise & Fall
    # 10cc
    ("3386133",  "27084564"),  # 10cc — Bloody Tourists
    ("1615490",  "26309384"),  # 10cc — Deceptive Bends
    # Gorillaz
    ("204021",   "20414716"),  # Gorillaz — Gorillaz
    ("474703",   "32440584"),  # Gorillaz — Demon Days
    # Nirvana (aanvulling)
    ("1073329",  "1559511"),   # Nirvana — In Utero
    # The Clash
    ("4126519",  "4914174"),   # The Clash — Combat Rock
    ("470912",   "2048710"),   # The Clash — London Calling
    # Guns N' Roses (aanvulling)
    ("2048352",  "25128898"),  # Guns N' Roses — Use Your Illusion II
    # ABBA
    ("380614",   "27884982"),  # ABBA — Super Trouper
    ("8688135",  "3105488"),   # ABBA — Voulez-Vous
    ("441165",   "27888051"),  # ABBA — Arrival
    ("20031742", "3105226"),   # ABBA — ABBA
    ("4475809",  "3102070"),   # ABBA — The Album
    ("9535494",  "3102140"),   # ABBA — The Visitors
    # Rage Against the Machine
    ("367339",   "4073023"),   # RATM — Rage Against the Machine
    # Drake
    ("3294598",  "21976249"),  # Drake — Take Care
    ("9258657",  "9247160"),   # Drake — If You're Reading This It's Too Late
    ("9258642",  "26904353"),  # Drake — Views
    ("12802012", "12800480"),  # Drake — Scorpion
    # J. Cole
    ("6736792",  "27356088"),  # J. Cole — 2014 Forest Hills Drive
    ("13377344", "12308370"),  # J. Cole — KOD
    ("20020801", "22026808"),  # J. Cole — The Off-Season
    # Young Thug
    ("9480756",  "30428033"),  # Young Thug — Jeffery
    # JACKBOYS
    ("15227004", "16211818"),  # JACKBOYS — JACKBOYS
    # Metro Boomin
    ("26608835", "26584355"),  # Metro Boomin — Heroes & Villains
    ("13053315", "13208577"),  # Metro Boomin — Not All Heroes Wear Capes
    # Coldplay
    ("484030",   "16231042"),  # Coldplay — Parachutes
    ("703741",   "7266689"),   # Coldplay — A Rush of Blood to the Head
    ("1044164",  "10039232"),  # Coldplay — X&Y
    ("5699282",  "5709533"),   # Coldplay — Ghost Stories (EU ↔ US)
    # Golden Earring
    ("589850",   "22207354"),  # Golden Earring — Moontan
    # Toto
    ("386005",   "16132613"),  # Toto — Toto IV
    ("1464270",  "693037"),    # Toto — Toto (debut)
    # Racoon
    ("3033070",  "10174337"),  # Racoon — Liverpool Rain
    ("20619157", "20433880"),  # Racoon — Spijt Is Iets Voor Later
    # BLØF
    ("10320776", "11745367"),  # BLØF — Aan
    # Pop Smoke
    ("16578819", "16938279"),  # Pop Smoke — Shoot for the Stars Aim for the Moon
    # Travis Scott
    ("31843496", "31828724"),  # Travis Scott — Days Before Rodeo
    ("7435327",  "22348948"),  # Travis Scott — Rodeo
    ("9403008",  "9402676"),   # Travis Scott — Birds in the Trap Sing McKnight
    ("27840414", "29085418"),  # Travis Scott — Utopia
    # Migos
    ("9896622",  "10271326"),  # Migos — Culture
    ("12270746", "12274680"),  # Migos — Culture II
    ("23055125", "23100230"),  # Migos — Culture III
    # 2Pac
    ("238369",   "25119211"),  # 2Pac — All Eyez on Me
    ("960025",   "12824597"),  # 2Pac — Greatest Hits
    # De Dijk
    ("2375621",  "30376355"),  # De Dijk — Wakker in een Vreemde Wereld
    ("1265189",  "21119311"),  # De Dijk — Niemand in de Stad
    # Green Day (aanvulling)
    ("2103788",  "1770697"),   # Green Day — Dookie
    ("1297507",  "17885617"),  # Green Day — Insomniac
    ("1220700",  "18825352"),  # Green Day — Nimrod
    ("1827139",  "15703648"),  # Green Day — 21st Century Breakdown
    # Bon Jovi
    ("1443701",  "9307146"),   # Bon Jovi — Slippery When Wet
    ("17650882", "9299636"),   # Bon Jovi — Crush (US ↔ EU)
    # Doe Maar (aanvulling)
    ("401816",   "23005994"),  # Doe Maar — 4US
    # Fugees
    ("361323",   "2691711"),   # Fugees — The Score
    # André Hazes
    ("952645",   "20794702"),  # André Hazes — Gewoon André
    ("3100518",  "26658734"),  # André Hazes — Kleine Jongen
    ("1201387",  "24606677"),  # André Hazes — Voor Jou
    ("25287358", "2693688"),   # André Hazes — Zo Is Het Leven (De Vlieger)
    # Marvin Gaye & Tammi Terrell
    ("16254322", "3925863"),   # Marvin Gaye & Tammi Terrell — United
    # UB40
    ("15311529", "6848587"),   # UB40 — Labour of Love
    # Sting
    ("1066027",  "9124039"),   # Sting — ...Nothing Like the Sun
    # Notorious B.I.G. (aanvulling)
    ("268090",   "22556825"),  # Notorious B.I.G. — Life After Death
    # KISS
    ("688267",   "5557406"),   # KISS — Dressed to Kill
    ("625331",   "6102279"),   # KISS — Dynasty
    # ZZ Top
    ("840158",   "2560004"),   # ZZ Top — Tres Hombres
    # Radiohead
    ("339574",   "1635232"),   # Radiohead — Pablo Honey
    # Foo Fighters
    ("2070894",  "15388111"),  # Foo Fighters — Echoes, Silence, Patience & Grace
    # Lynyrd Skynyrd
    ("2034523",  "4179730"),   # Lynyrd Skynyrd — (Pronounced)
    ("1634156",  "2341640"),   # Lynyrd Skynyrd — Second Helping
    # Guns N' Roses (Use Your Illusion I)
    ("2206780",  "25133437"),  # Guns N' Roses — Use Your Illusion I
    # Limp Bizkit
    ("3782742",  "10821533"),  # Limp Bizkit — Significant Other
    # Billy Joel
    ("9603231",  "3333076"),   # Billy Joel — Piano Man
    # John Lennon
    ("376138",   "2590105"),   # John Lennon — Imagine
    # The Cranberries
    ("501415",   "6986271"),   # The Cranberries — No Need to Argue
    # Pink Floyd
    ("495681",   "8961614"),   # Pink Floyd — The Wall
    # The Verve
    ("445878",   "9057753"),   # The Verve — Urban Hymns
    # R.E.M.
    ("2024524",  "9359884"),   # R.E.M. — Out of Time
    # Bryan Adams
    ("1469334",  "6275103"),   # Bryan Adams — Reckless
    # Dire Straits
    ("382417",   "17896735"),  # Dire Straits — Brothers in Arms
    # The Scene
    ("864382",   "13670805"),  # The Scene — Blauw
    # Nickelback
    ("1982122",  "10533002"),  # Nickelback — Silver Side Up
    # Soft Cell
    ("238877",   "6063431"),   # Soft Cell — Non-Stop Erotic Cabaret
    # Depeche Mode
    ("1217497",  "8953011"),   # Depeche Mode — Speak & Spell
    # Frank Sinatra
    ("804778",   "8212247"),   # Frank Sinatra — That's Life
    # Coolio
    ("1410402",  "3480688"),   # Coolio — Gangsta's Paradise (US ↔ EU)
    # Eurythmics
    ("66534",    "11860171"),  # Eurythmics — Sweet Dreams
]
# Snelle lookup: id → partner_id
_PAIR_MAP = {a: b for a, b in RELEASE_PAIRS} | {b: a for a, b in RELEASE_PAIRS}
_LEFT_IDS = {a for a, b in RELEASE_PAIRS}

RELEASES = {
    # ── OASIS ──
    "939519":   ("Oasis", "Morning Glory (CRE LP 189, Damont, UK 1995)"),
    "517224":   ("Oasis", "Definitely Maybe (CRE LP 169, Damont, UK 1994)"),
    "6127871":  ("Oasis", "Morning Glory (RKIDLP73, EU reissue 2014)"),
    "12864584": ("Oasis", "Definitely Maybe reissue (RKIDLP70, 2014)"),
    "2334540":  ("Oasis", "Time Flies... 1994-2009 (RKIDLP66, Big Brother UK 2010)"),
    "2521407":  ("Oasis", "Time Flies... 1994-2009 (88697722641, Big Brother EU 2010)"),
    "33663000": ("Oasis", "Time Flies... 1994-2009 RSD (RKIDLP150RSD, Worldwide 2025)"),
    "34257205": ("Oasis", "Time Flies... 1994-2009 reissue (RKIDLP150, Worldwide 2025)"),

    # ── RED HOT CHILI PEPPERS ──
    "375491":   ("RHCP", "Blood Sugar Sex Magik (7599-26681-1, EU first pressing 1991)"),
    "12042641": ("RHCP", "Blood Sugar Sex Magik (468348-1, US 2012 remaster)"),
    "14914560": ("RHCP", "Californication (9 47386-1, US first pressing 1999)"),
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
    "152946":   ("Michael Jackson", "Thriller (EPC 85930, EU 1982)"),
    "459606":   ("Michael Jackson", "Bad (E 40600, Carrollton, US 1987)"),

    # ── METALLICA ──
    "1549636":  ("Metallica", "Master of Puppets (60439-1, Allied, US 1986)"),
    "11118447": ("Metallica", "Master of Puppets (BLCKND005R-1, Blackened, US 2017)"),
    "381988":   ("Metallica", "Black Album (61113-1, Elektra, US 1991)"),
    "439599":   ("Metallica", "Black Album (510 022-1, Vertigo, EU 1991)"),

    # ── QUEEN ──
    "612780":   ("Queen", "A Night at the Opera (EMTC 103, UK 1975)"),
    "7541569":  ("Queen", "A Night at the Opera half-speed (00602547202697, EU 2015)"),
    "10130642": ("Queen", "Sheer Heart Attack (EMC 3061, EMI UK 1974)"),
    "23316872": ("Queen", "A Day at the Races (EMTC 104, EMI UK 1976)"),
    "22048156": ("Queen", "A Day at the Races (6E-101, Elektra US 1976)"),
    "14031557": ("Queen", "News of the World (EMA 784, EMI UK 1977)"),
    "3824539":  ("Queen", "News of the World (6E-112, Elektra US 1977)"),
    "475606":   ("Queen", "Jazz (EMA 788, EMI UK 1978)"),
    "4269045":  ("Queen", "Jazz (6E-166, Elektra US 1978)"),
    "455954":   ("Queen", "The Game (EMA 795, EMI UK 1980)"),
    "446814":   ("Queen", "The Game (5E-513, Elektra US 1980)"),
    "589920":   ("Queen", "Hot Space (EMA 797, EMI UK 1982)"),
    "505097":   ("Queen", "Hot Space (E1-60128, Elektra US 1982)"),
    "4732312":  ("Queen", "The Works (EMC 2400141, EMI UK 1984) [white label promo]"),
    "36148198": ("Queen", "The Works (0602547202789, Virgin EMI EU 2023)"),
    "11967456": ("Queen", "A Kind of Magic (EU 3509, EMI UK 1986)"),
    "25210735": ("Queen", "A Kind of Magic (D004064601, Hollywood US 2022)"),

    # ── AC/DC ──
    "400591":   ("AC/DC", "Back in Black (APLP-046, Australisch origineel 1980)"),
    "1949857":  ("AC/DC", "Back in Black (5107651, EU reissue 2009)"),
    "400587":   ("AC/DC", "Highway to Hell (APLP-040, Australisch origineel 1979)"),
    "2520300":  ("AC/DC", "Highway to Hell (5107641, EU reissue 2009)"),

    # ── LINKIN PARK ──
    "534020":   ("Linkin Park", "Hybrid Theory (9 47755-1, US 2001)"),
    "21054706": ("Linkin Park", "Hybrid Theory 20th Anniversary (093624941422, US 2021)"),
    "3336797":  ("Linkin Park", "Meteora (48186-1, US 2003)"),
    "28403278": ("Linkin Park", "Meteora 20th Anniversary (093624853343, US 2023)"),

    # ── GREEN DAY ──
    "9452213":  ("Green Day", "American Idiot (9362-48777-1, US first pressing 2004)"),
    "1203470":  ("Green Day", "American Idiot (9362-48777-1, EU 2004)"),

    # ── GUNS N' ROSES ──
    "383777":   ("Guns N' Roses", "Appetite for Destruction (XXXG 24148, US Allied 1987)"),
    "7492229":  ("Guns N' Roses", "Appetite for Destruction (00720642414811, reissue EU 2015)"),

    # ── NOTORIOUS B.I.G. ──
    "317356":   ("Notorious B.I.G.", "Ready to Die (78612-73000-1, US 1994)"),
    "34578556": ("Notorious B.I.G.", "Ready to Die (RR1 285201, Rhino reissue US 2004)"),

    # ── KENDRICK LAMAR ──
    "3975953":  ("Kendrick Lamar", "Good Kid M.A.A.D City (B0017695-01, US 2012)"),
    "30551209": ("Kendrick Lamar", "Good Kid M.A.A.D City (B0036420-01, US reissue 2022)"),
    "8814849":  ("Kendrick Lamar", "To Pimp A Butterfly (B0023464-01, US 2015)"),
    "23398166": ("Kendrick Lamar", "To Pimp A Butterfly (B0023464-01, US repress 2022)"),
    "10559651": ("Kendrick Lamar", "DAMN. (B0026745-01, US 2017)"),
    "25683820": ("Kendrick Lamar", "DAMN. (00602557618280, EU reissue 2022)"),

    # ── THE KILLERS ──
    "397167":   ("The Killers", "Hot Fuss (LIZARD011X, blue marbled, UK 2004)"),
    "20298550": ("The Killers", "Hot Fuss (B0026979-01, US reissue 2017)"),

    # ── DOE MAAR ──
    "402227":   ("Doe Maar", "Skunk (Kil 19934 Kl, NL 1981)"),
    "22712903": ("Doe Maar", "Skunk (MOVLP2295, Music on Vinyl EU 2022)"),
    "382601":   ("Doe Maar", "Doris Day En Andere Stukken (Kil 21032 Kl, NL 1982)"),
    "22494431": ("Doe Maar", "Doris Day En Andere Stukken (MOVLP2297, Music on Vinyl NL 2022)"),

    # ── EAGLES ──
    "1934367":  ("Eagles", "Hotel California (7E-1084, US origineel 1976)"),
    "9847048":  ("Eagles", "Hotel California (RRM1-1084, Rhino remaster Worldwide 2015)"),

    # ── AMY WINEHOUSE ──
    "2848009":  ("Amy Winehouse", "Back To Black (B0008994-01, US 2006)"),
    "34780535": ("Amy Winehouse", "Back To Black (B0008994-01, Island UK 2006)"),

    # ── BOB MARLEY ──
    "12927816": ("Bob Marley", "Legend (BMW 1, Island UK first pressing 1984)"),
    "4418438":  ("Bob Marley", "Legend (0600753030523, EU reissue 2015)"),
    "3660230":  ("Bob Marley", "Exodus (ILPS 9498, Tuff Gong, Jamaica 1977)"),
    "1862215":  ("Bob Marley", "Exodus (0600753184196, Island EU reissue 2009)"),
    "65845":    ("Bob Marley", "Rastaman Vibration (ILPS 9383, Island UK 1976)"),
    "746135":   ("Bob Marley", "Rastaman Vibration (TGLLP 6, Tuff Gong EU reissue 2001)"),

    # ── FLEETWOOD MAC ──
    "526351":   ("Fleetwood Mac", "Rumours (BSK 3010, US origineel 1977)"),
    "3229870":  ("Fleetwood Mac", "Rumours (R1 567113, Worldwide reissue 2023)"),
    "4065605":  ("Fleetwood Mac", "Tango In The Night (9 25471-1, Warner Bros. US 1987)"),
    "33831750": ("Fleetwood Mac", "Tango In The Night (WX 65, Warner Bros. EU 1987)"),

    # ── NIRVANA ──
    "1813006":  ("Nirvana", "Nevermind (DGC-24425, US origineel 1991)"),
    "3183667":  ("Nirvana", "Nevermind 20th Anniversary (B0015884-01, US 2011)"),

    # ── FLATBUSH ZOMBIES ──
    "12895130": ("Flatbush Zombies", "Vacation in Hell (clear/black smoke vinyl, 2018)"),

    # ── BEAST COAST ──
    "13672908": ("Beast Coast", "Escape From New York (blue vinyl, 2019)"),

    # ── MOBB DEEP ──
    "223127":   ("Mobb Deep", "The Infamous (07863 66480-1, US 1995)"),
    "7753999":  ("Mobb Deep", "The Infamous (MOVLP1463, Music on Vinyl EU 2015)"),

    # ── STICKS ──
    "16170729": ("Sticks", "Stickmatic (350 405-3, NL 2020)"),

    # ── THE POLICE ──
    "5305755":  ("The Police", "Outlandos d'Amour (AMLH 68502, A&M UK 1978)"),
    "13549135": ("The Police", "Outlandos d'Amour (676325-1, A&M US 2018 remaster)"),
    "11827033": ("The Police", "Reggatta de Blanc (AMLH 64792, A&M UK 1979)"),
    "3363252":  ("The Police", "Reggatta de Blanc (0082839479219, A&M EU 2008)"),
    "3214829":  ("The Police", "Synchronicity (AMLX 63735, A&M UK 1983)"),
    "31334893": ("The Police", "Synchronicity (558 217-9, A&M Worldwide 2024 remaster)"),

    # ── LED ZEPPELIN ──
    "2893139":  ("Led Zeppelin", "Led Zeppelin IV (2401012, Atlantic UK 1971)"),
    "22645229": ("Led Zeppelin", "Led Zeppelin IV (8122-79657-7, Atlantic US 2022 remaster)"),

    # ── ROLLING STONES ──
    "7264539":  ("Rolling Stones", "Beggars Banquet (SKL 4955, Decca UK 1968) [stereo]"),
    "1137475":  ("Rolling Stones", "Beggars Banquet (LK 4955, Decca UK 1968) [mono]"),
    "765072":   ("Rolling Stones", "Aftermath (PS 476, London US 1966)"),
    "6003779":  ("Rolling Stones", "Aftermath (LK 4786, Decca UK 1966)"),
    "468054":   ("Rolling Stones", "Tattoo You (COC 16052, Rolling Stones Records US 1981)"),
    "1931909":  ("Rolling Stones", "Tattoo You (CUNS 39114, Rolling Stones Records UK 1981)"),
    "7435111":  ("Rolling Stones", "Out of Our Heads (PS 429, London US 1965)"),

    # ── ELVIS PRESLEY ──
    "13314916": ("Elvis Presley", "Jailhouse Rock (EP) (EPA-4114, RCA Victor US 1957)"),

    # ── 21 SAVAGE ──
    "10597886": ("21 Savage", "Savage Mode (Club Edition, Slaughter Gang, US 2017)"),
    "10752389": ("21 Savage", "Savage Mode (Slaughter Gang, US 2017)"),
    "10873523": ("21 Savage", "Issa Album (88985466821, Slaughter Gang, US 2017)"),
    "13876606": ("21 Savage", "Issa Album (889854668211, Slaughter Gang, EU 2019)"),
    "13318697": ("21 Savage", "I Am > I Was (19075923521, Slaughter Gang, US 2018)"),
    "15624365": ("21 Savage", "I Am > I Was (19075922121, Slaughter Gang, EU 2019)"),
    "17277640": ("21 Savage", "Savage Mode II (19439818631, Slaughter Gang, US 2020)"),

    # ── 50 CENT ──
    "485114":   ("50 Cent", "Get Rich or Die Tryin' (0694935441, Aftermath US 2003)"),
    "1198408":  ("50 Cent", "Get Rich or Die Tryin' (493 544-1, Shady EU 2003, 180g)"),
    "598810":   ("50 Cent", "The Massacre (B0004317-01, Shady US 2005)"),
    "8954977":  ("50 Cent", "The Massacre (B0025252-01, UMe US 2016 reissue)"),

    # ── U2 ──
    "10456142": ("U2", "The Joshua Tree (U26, Island Records UK 1987)"),
    "10395824": ("U2", "The Joshua Tree (5749844, Island Records EU 2017, 30th Anniversary)"),
    "676619":   ("U2", "All That You Can't Leave Behind (U212 / 524 653-1, Island Records EU 2000)"),
    "11846916": ("U2", "All That You Can't Leave Behind (5796988, Island Records EU 2018 remaster)"),

    # ── ROWWEN HÈZE ──
    "15500018": ("Rowwen Hèze", "Blieve Loepe (HKM 72031, HKM NL 2020, RSD rood vinyl 45RPM)"),
    "26032708": ("Rowwen Hèze", "Blieve Loepe (HKM 72031, HKM NL 2023, reissue)"),
    "15969889": ("Rowwen Hèze", "Boem (HKM 72032, HKM NL 2020, 2LP)"),
    "18017281": ("Rowwen Hèze", "Vandaag (HKM 72036, HKM NL 2021, 2LP reissue)"),

    # ── JANSE BAGGE BEND ──
    "16062321": ("Janse Bagge Bend", "Veërtig (L202010, Marlstone Music NL 2020, geel vinyl)"),

    # ── MADNESS ──
    "370989":  ("Madness", "Complete Madness (HIT-TV1, Stiff Records UK 1982)"),
    "4731834": ("Madness", "Complete Madness (LETV079LP, Let Them Eat Vinyl UK 2013, 2LP rood vinyl)"),
    "401145":  ("Madness", "The Rise & Fall (SEEZ 46, Stiff Records UK 1982)"),
    "378607":  ("Madness", "The Rise & Fall (6.25422, Stiff Records DE 1982)"),

    # ── ABBA ──
    "380614":   ("ABBA", "Super Trouper (POLS 322, Polar SE 1980)"),
    "27884982": ("ABBA", "Super Trouper (POLS 322, Polar EU 2022, 180g reissue)"),
    "8688135":  ("ABBA", "Voulez-Vous (POLS 292, Polar SE 1979)"),
    "3105488":  ("ABBA", "Voulez-Vous (POLS 292, Polar EU 2011, 180g reissue)"),
    "441165":   ("ABBA", "Arrival (POLS 272, Polar SE 1976)"),
    "27888051": ("ABBA", "Arrival (POLS 272, Polar EU 2022, reissue)"),
    "20031742": ("ABBA", "ABBA (POLS 262, Polar SE 1975)"),
    "3105226":  ("ABBA", "ABBA (POLS 262, Polar EU 2011, 180g reissue)"),
    "4475809":  ("ABBA", "The Album (POLS 282, Polar SE 1977)"),
    "3102070":  ("ABBA", "The Album (POLS 282, Polar EU 2011, 180g reissue)"),
    "9535494":  ("ABBA", "The Visitors (POLS 342, Polar SE 1981)"),
    "3102140":  ("ABBA", "The Visitors (POLS 342, Polar EU 2011, 180g reissue)"),

    # ── 10CC ──
    "3386133":  ("10cc", "Bloody Tourists (9102 503, Mercury UK 1978)"),
    "27084564": ("10cc", "Bloody Tourists (UMCLP017, UMC/Mercury UK 2023, 180g)"),
    "1615490":  ("10cc", "Deceptive Bends (9102 502, Mercury UK 1977)"),
    "26309384": ("10cc", "Deceptive Bends (UMCLP016, Mercury UK/EU/US 2023)"),

    # ── GORILLAZ ──
    "204021":   ("Gorillaz", "Gorillaz (7243 531138 1 0, Parlophone EU 2001, 2LP gatefold)"),
    "20414716": ("Gorillaz", "Gorillaz (7243 531138 1 0, Parlophone EU 2021, Special Cut)"),
    "474703":   ("Gorillaz", "Demon Days (07243 873838 1 4, Parlophone UK 2005, 2LP gatefold)"),
    "32440584": ("Gorillaz", "Demon Days (0724387383814, Parlophone EU repress, 2LP gatefold)"),

    # ── NIRVANA ──
    "1073329":  ("Nirvana", "In Utero (DGC-24607, DGC US 1993, clear vinyl Limited Edition)"),
    "1559511":  ("Nirvana", "In Utero (0720642453612, Geffen EU 2008, 180g remastered)"),

    # ── THE CLASH ──
    "4126519":  ("The Clash", "Combat Rock (FMLN 2, CBS UK 1982)"),
    "4914174":  ("The Clash", "Combat Rock (88725446971, Columbia EU 2013, 180g)"),
    "470912":   ("The Clash", "London Calling (CBS CLASH 3, CBS UK 1979, 2LP)"),
    "2048710":  ("The Clash", "London Calling (MOVLP050, Music On Vinyl EU 2009, 2LP 180g)"),

    # ── GUNS N' ROSES (aanvulling) ──
    "2048352":  ("Guns N' Roses", "Use Your Illusion II (GEF 24420, Geffen US 1991, 2LP)"),
    "25128898": ("Guns N' Roses", "Use Your Illusion II (00602445117314, Geffen EU 2022, 2LP 180g)"),

    # ── RAGE AGAINST THE MACHINE ──
    "367339":  ("Rage Against the Machine", "Rage Against the Machine (Z 52959, Epic US 1992, LP)"),
    "4073023": ("Rage Against the Machine", "Rage Against the Machine (88725470451, Epic/Legacy US 2012, LP 180g)"),

    # ── DRAKE ──
    "3294598":  ("Drake", "Take Care (B0016280-01, Young Money/Cash Money US 2011, 2LP)"),
    "21976249": ("Drake", "Take Care (B0016280-01, Young Money/Cash Money US 2021, 2LP reissue)"),
    "9258657":  ("Drake", "If You're Reading This It's Too Late (B0025237-01, Young Money US 2016, 2LP)"),
    "9247160":  ("Drake", "If You're Reading This It's Too Late (0602547973450, Young Money EU 2016, 2LP)"),
    "9258642":  ("Drake", "Views (B0025236-01, Young Money/Cash Money US 2016, 2LP)"),
    "26904353": ("Drake", "Views (B0025236-01, Young Money/Cash Money US 2022, 2LP reissue)"),
    "26783426": ("Drake", "More Life (B0036101-01, OVO/Young Money US 2023, 2LP reissue)"),
    "12802012": ("Drake", "Scorpion (B0029103-01, Young Money/Cash Money US 2018, 4LP)"),
    "12800480": ("Drake", "Scorpion (00602567874942, Young Money/Cash Money EU 2018, 4LP)"),

    # ── J. COLE ──
    "6736792":  ("J. Cole", "2014 Forest Hills Drive (88875 05698 1, Roc Nation/Columbia US 2015, 2LP)"),
    "27356088": ("J. Cole", "2014 Forest Hills Drive (B0037320-01, Interscope US 2023, 2LP reissue)"),
    "13377344": ("J. Cole", "KOD (B0028571-01, Dreamville/Interscope US 2018, 2LP gatefold)"),
    "12308370": ("J. Cole", "KOD (00810760032230, Dreamville/Interscope EU 2018, 2LP gatefold)"),
    "20020801": ("J. Cole", "The Off-Season (B0034081-01, Dreamville/Interscope US 2021, 2LP)"),
    "22026808": ("J. Cole", "The Off-Season (00810061165248, Dreamville/Interscope US/EU 2022, 2LP blauw)"),

    # ── YOUNG THUG ──
    "9480756":  ("Young Thug", "Jeffery (557768-1, 300 Entertainment/Atlantic/VMP US 2016, LP blauw/wit marmer)"),
    "30428033": ("Young Thug", "Jeffery (075678613456, 300 Entertainment/Atlantic 2024, LP blauw galaxy RSD)"),
    "18582520": ("Young Thug", "So Much Fun (624959-1, Atlantic/YSL/300/VMP US 2021, 2LP groen)"),

    # ── JACKBOYS ──
    "15227004": ("JACKBOYS", "JACKBOYS (19439748411, Cactus Jack/Epic US 2020, LP)"),
    "16211818": ("JACKBOYS", "JACKBOYS (19439748411, Cactus Jack/Epic EU 2020, LP)"),

    # ── POP SMOKE ──
    "16578819": ("Pop Smoke", "Shoot for the Stars Aim for the Moon (B0032626-01, Victor Victor/Republic US 2020, 2LP)"),
    "16938279": ("Pop Smoke", "Shoot for the Stars Aim for the Moon (00602507306465, Victor Victor/Republic EU 2020, 2LP)"),
    "31489625": ("Pop Smoke", "Meet the Woo (602465755855, Victor Victor/Republic US 2024, LP blauw)"),
    "16026374": ("Pop Smoke", "Meet the Woo 2 (B0032506-01, Victor Victor/Republic US 2020, 2LP)"),

    # ── TRAVIS SCOTT ──
    "31843496": ("Travis Scott", "Days Before Rodeo (Cactus Jack/Epic US 2024, LP rood)"),
    "31828724": ("Travis Scott", "Days Before Rodeo (TSBR-VR07, Cactus Jack/Epic US 2024, LP reissue)"),
    "7435327":  ("Travis Scott", "Rodeo (88875065201, Epic/Grand Hustle US 2015, 2LP)"),
    "22348948": ("Travis Scott", "Rodeo (88875065201, Epic/Grand Hustle US/EU 2022, 2LP reissue)"),
    "9403008":  ("Travis Scott", "Birds in the Trap Sing McKnight (88985377711, Epic/Grand Hustle US 2016, 2LP)"),
    "9402676":  ("Travis Scott", "Birds in the Trap Sing McKnight (88985377711, Epic/Grand Hustle EU 2016, 2LP)"),
    "27840414": ("Travis Scott", "Utopia (1 96588 15041 8, Epic/Cactus Jack US 2023, 2LP)"),
    "29085418": ("Travis Scott", "Utopia (1 96588 46031, Epic/Cactus Jack EU 2023, 2LP reissue)"),

    # ── MIGOS ──
    "9896622":  ("Migos", "Culture (QC/300 Entertainment/Atlantic US, 2LP goud translucent beperkt)"),
    "10271326": ("Migos", "Culture (560566-1, QC/300 Entertainment/Atlantic US 2017, 2LP)"),
    "12270746": ("Migos", "Culture II (B002813601, QC/Motown/Capitol US 2018, 3LP rood)"),
    "12274680": ("Migos", "Culture II (B002813501, QC/Motown/Capitol US 2018, 3LP)"),
    "23055125": ("Migos", "Culture III (B003486901, QC/Motown/UMG US 2022, 2LP)"),
    "23100230": ("Migos", "Culture III (B003486901, QC/Motown/Walmart US 2022, 2LP wit)"),

    # ── 2PAC ──
    "238369":   ("2Pac", "All Eyez on Me (314-524 204-1, Death Row/Interscope US 1996, 4LP)"),
    "25119211": ("2Pac", "All Eyez on Me (00602448276261, Interscope/Amaru/UMe US/EU 2022, 4LP 180g)"),
    "960025":   ("2Pac", "Greatest Hits (INT4-90301, Death Row/Interscope US 1998, 4LP)"),
    "12824597": ("2Pac", "Greatest Hits (B0029039-01, Death Row/Interscope/UMG US 2018, 4LP reissue)"),

    # ── METRO BOOMIN ──
    "26608835": ("Metro Boomin", "Heroes & Villains (B0037189-01, Boominati/Republic US 2023, LP Target exclusief)"),
    "26584355": ("Metro Boomin", "Heroes & Villains (B0037188-01, Boominati/Republic US 2023, LP)"),
    "13053315": ("Metro Boomin", "Not All Heroes Wear Capes (B0029506-01, Republic/Boominati US 2018, LP)"),
    "13208577": ("Metro Boomin", "Not All Heroes Wear Capes (00602577305603, Republic/Boominati EU 2019, LP)"),

    # ── COLDPLAY ──
    "484030":   ("Coldplay", "Parachutes (7243 5 27783 1 7, Parlophone/EMI EU 2000, LP)"),
    "16231042": ("Coldplay", "Parachutes (0190295182502, Parlophone/Warner EU 2020, LP geel translucent)"),
    "703741":   ("Coldplay", "A Rush of Blood to the Head (7243 5 40504 1 1, Parlophone EU 2002, LP 180g)"),
    "7266689":  ("Coldplay", "A Rush of Blood to the Head (7243 5 40504 1 1, Parlophone EU 2013, LP 180g reissue)"),
    "1044164":  ("Coldplay", "X&Y (7243 4 74786 1 1, Parlophone EU 2005, LP)"),
    "10039232": ("Coldplay", "X&Y (07243 474786 1 1, Parlophone EU 2016, LP reissue)"),
    "5699282":  ("Coldplay", "Ghost Stories (825646298815, Parlophone/Warner EU 2014, LP 180g)"),
    "5709533":  ("Coldplay", "Ghost Stories (542279-1, Atlantic/Parlophone US 2014, LP)"),

    # ── GOLDEN EARRING ──
    "589850":   ("Golden Earring", "Moontan (2925 017, Polydor NL 1973, LP gatefold)"),
    "22207354": ("Golden Earring", "Moontan (MOVLP3000, Music On Vinyl EU 2022, 2LP geremasterd clear vinyl)"),

    # ── TOTO ──
    "386005":   ("Toto", "Toto IV (CBS 85529, CBS EU 1982, LP)"),
    "16132613": ("Toto", "Toto IV (19075801121, Columbia EU 2020, LP remaster)"),
    "1464270":  ("Toto", "Toto (JC 35317, Columbia US 1978, LP)"),
    "693037":   ("Toto", "Toto (CBS 83148, CBS EU 1978, LP)"),

    # ── RACOON ──
    "3033070":  ("Racoon", "Liverpool Rain (PIASNL0026CLPCD, PIAS NL 2011, LP+CD)"),
    "10174337": ("Racoon", "Liverpool Rain (PIASNL0026CLPCD, PIAS NL 2017, LP wit+CD)"),
    "22336765": ("Racoon", "Another Day (PIAS NL 2022, LP beperkt)"),
    "6647916":  ("Racoon", "All in Good Time (944.A174.010, PIAS NL 2015, LP+CD)"),
    "20619157": ("Racoon", "Spijt Is Iets Voor Later (19439887531, Sony Music NL 2021, LP+CD)"),
    "20433880": ("Racoon", "Spijt Is Iets Voor Later (19439887541, Sony Music EU 2021, LP clear+CD)"),
    "22785002": ("Racoon", "Spijt Is Iets Voor Later — Artone Sessions (19439976911, Sony Music NL 2022, LP bruin)"),

    # ── BLØF ──
    "26234072": ("BLØF", "Boven (MOVLP3301, Music On Vinyl EU 2023, 2LP 180g)"),
    "10320776": ("BLØF", "Aan (97205, Altijd Wakker NL 2017, 2LP geel beperkt genummerd)"),
    "11745367": ("BLØF", "Aan (97205, Altijd Wakker Benelux 2017, 2LP)"),
    "27135846": ("BLØF", "Blauwe Ruis (MOVLP3195, Music On Vinyl NL 2023, LP 180g blauw)"),
    "26449052": ("BLØF", "Watermakers (MOVLP3302, Music On Vinyl NL 2023, 2LP zilver genummerd)"),

    # ── JANSE BAGGE BEND (aanvulling) ──
    "2267945":  ("Janse Bagge Bend", "Flazjelêttentaere (SKY 21048 SL, Sky/Marlstone NL 1983, LP)"),

    # ── DE DIJK ──
    "2084461":  ("De Dijk", "De Dijk (88.053, Dureco Benelux 1982, LP)"),
    "3469887":  ("De Dijk", "Elke Dag Een Nieuwe Hoed (TLP 19081, Sky NL 1985, LP)"),
    "2375621":  ("De Dijk", "Wakker in een Vreemde Wereld (832 637-1, Mercury NL 1987, LP)"),
    "30376355": ("De Dijk", "Wakker in een Vreemde Wereld (650 170-4, Universal Music NL 2024, LP reissue)"),
    "1265189":  ("De Dijk", "Niemand in de Stad (836 985-1, Mercury/Phonogram NL 1989, LP)"),
    "21119311": ("De Dijk", "Niemand in de Stad (MOVLP619, Music On Vinyl EU 2021, LP geel)"),
    "22978889": ("De Dijk", "De Blauwe Schuit (MOVLP3032, Music On Vinyl NL 2022, LP blauw transparant)"),

    # ── GREEN DAY (aanvulling) ──
    "2103788":  ("Green Day", "Dookie (1-45529, Reprise US 1994, LP)"),
    "1770697":  ("Green Day", "Dookie (468284-1, Reprise US 2009, LP reissue)"),
    "1297507":  ("Green Day", "Insomniac (1-46046, Reprise US 1995, LP)"),
    "17885617": ("Green Day", "Insomniac (093624884576, Reprise US/EU 2021, LP 25th Anniversary)"),
    "1220700":  ("Green Day", "Nimrod (9362-46794-1, Reprise DE 1997, LP)"),
    "18825352": ("Green Day", "Nimrod (093624912231, Reprise US/EU 2021, LP reissue)"),
    "1827139":  ("Green Day", "21st Century Breakdown (517153-1, Reprise US 2009, 2LP 180g)"),
    "15703648": ("Green Day", "21st Century Breakdown (093624978534, Reprise/Warner US 2019, 2LP reissue)"),

    # ── BON JOVI ──
    "1443701":  ("Bon Jovi", "Slippery When Wet (830 264-1 M-1, Mercury US 1986, LP)"),
    "9307146":  ("Bon Jovi", "Slippery When Wet (Mercury/Back To Black EU 2016, LP 180g)"),
    "17650882": ("Bon Jovi", "Crush (B0021972-01, Island US 2014, 2LP 180g)"),
    "9299636":  ("Bon Jovi", "Crush (06025 470 299-4, Island EU 2016, 2LP 180g)"),

    # ── DOE MAAR (aanvulling) ──
    "401816":   ("Doe Maar", "4US (24000 SL, Sky/Foon NL 1983, LP)"),
    "23005994": ("Doe Maar", "4US (MOVLP2298, Music On Vinyl NL 2022, LP reissue)"),

    # ── FUGEES ──
    "361323":   ("Fugees", "The Score (C2 67147, Columbia/Ruffhouse US 1996, 2LP)"),
    "2691711":  ("Fugees", "The Score (MOVLP068, Music On Vinyl EU 2010, 2LP 180g)"),

    # ── ANDRÉ HAZES ──
    "952645":   ("André Hazes", "Gewoon André (1A 064-26677, EMI NL 1981, LP)"),
    "20794702": ("André Hazes", "Gewoon André (MOVLP2884, Music On Vinyl NL 2021, LP rood)"),
    "3100518":  ("André Hazes", "Kleine Jongen (7949391, EMI NL 1990, LP)"),
    "26658734": ("André Hazes", "Kleine Jongen (MOVLP3362, Music On Vinyl NL 2023, LP groen)"),
    "28648639": ("André Hazes", "Strijdlustig (MOVLP3546, Music On Vinyl NL 2023, LP zilver)"),
    "26918048": ("André Hazes", "Met Heel Mijn Hart (MOVLP3431, Music On Vinyl NL 2023, LP geel)"),
    "1201387":  ("André Hazes", "Voor Jou (1A 068-1270201, EMI/EMI-Bovema NL 1983, LP)"),
    "24606677": ("André Hazes", "Voor Jou (MOVLP3134, Music On Vinyl NL 2022, LP oranje)"),
    "25287358": ("André Hazes", "Zo Is Het Leven (6410 140, Philips NL 1977, LP)"),
    "2693688":  ("André Hazes", "Zo Is Het Leven (6423 412, Philips/Gouden Molen NL 1981, LP repress)"),

    # ── MARVIN GAYE & TAMMI TERRELL ──
    "16254322": ("Marvin Gaye & Tammi Terrell", "United (TS 277, Tamla US stereo first pressing 1967)"),
    "3925863":  ("Marvin Gaye & Tammi Terrell", "United (TS 277, Speakers Corner 180g DE 2009)"),

    # ── RAMSES SHAFFY ──
    "735833":   ("Ramses Shaffy", "Dag En Nacht (6423 112, Philips NL 1978)"),

    # ── UB40 ──
    "15311529": ("UB40", "Labour of Love (LP DEP 5, DEP International UK 1983)"),
    "6848587":  ("UB40", "Labour of Love (00602547161116, Virgin EMI EU 2015, 2LP 180g)"),
    "4634884":  ("UB40", "Labour of Love II (LPDEP 14, DEP International UK 1989)"),
    "634218":   ("UB40", "Baggariddim (LP DEP 10, DEP International UK 1985, gatefold)"),
    "1201716":  ("UB40", "Promises and Lies (LPDEP 15, DEP International UK/EU 1993)"),

    # ── STING ──
    "1066027":  ("Sting", "...Nothing Like the Sun (AMA 6402, A&M UK/EU 1987, 2LP)"),
    "9124039":  ("Sting", "...Nothing Like the Sun (0082839391214, A&M EU 2016, 2LP Abbey Road remaster)"),

    # ── DRUKWERK ──
    "2689368":  ("Drukwerk", "Drukwerk (1A 058-26650, EMI NL 1981)"),
    "2972649":  ("Drukwerk", "Tweede Druk (1A 068 26852, EMI NL 1982)"),

    # ── JAY-Z & KANYE WEST ──
    "3351778":  ("Jay-Z & Kanye West", "Watch the Throne (B0016010-01, Roc-A-Fella US 2012, 2LP picture disc)"),

    # ── NOTORIOUS B.I.G. (aanvulling) ──
    "268090":   ("Notorious B.I.G.", "Life After Death (78612-73011-1, Bad Boy/Arista US 1997, 3LP)"),
    "22556825": ("Notorious B.I.G.", "Life After Death (R1 541302, Bad Boy EU 2022, 3LP 25th Anniversary)"),

    # ── KISS ──
    "688267":   ("KISS", "Dressed to Kill (NBLP 7016, Casablanca US 1975, LP)"),
    "5557406":  ("KISS", "Dressed to Kill (0602537727889, Casablanca/Mercury EU 2014, LP 180g)"),
    "625331":   ("KISS", "Dynasty (NBLP 7152, Casablanca US 1979, LP)"),
    "6102279":  ("KISS", "Dynasty (0602537770946, Casablanca/Mercury DE 2014, LP 180g)"),

    # ── ZZ TOP ──
    "840158":   ("ZZ Top", "Tres Hombres (XPS 631, London Records US 1973, LP gatefold)"),
    "2560004":  ("ZZ Top", "Tres Hombres (274492, Warner Bros. US 2006, LP 180g)"),

    # ── RADIOHEAD ──
    "339574":   ("Radiohead", "Pablo Honey (PCS 7360, Parlophone UK 1993, LP)"),
    "1635232":  ("Radiohead", "Pablo Honey (Capitol US 2008, LP 180g)"),

    # ── FOO FIGHTERS ──
    "2070894":  ("Foo Fighters", "Echoes, Silence, Patience & Grace (88697 11516-1, Roswell/RCA US 2007, 2LP)"),
    "15388111": ("Foo Fighters", "Echoes, Silence, Patience & Grace (88697 11516-1, Roswell/RCA reissue, 2LP)"),

    # ── LYNYRD SKYNYRD ──
    "2034523":  ("Lynyrd Skynyrd", "(Pronounced 'Leh-'nérd 'Skin-'nérd) (MCA-363, MCA US 1973, LP gatefold)"),
    "4179730":  ("Lynyrd Skynyrd", "(Pronounced 'Leh-'nérd 'Skin-'nérd) (MFSL 1-400, MFSL US 2013, LP 180g)"),
    "1634156":  ("Lynyrd Skynyrd", "Second Helping (MCA-413, MCA US 1974, LP)"),
    "2341640":  ("Lynyrd Skynyrd", "Second Helping (MCA-1686, MCA US 2008, LP 180g)"),

    # ── GUNS N' ROSES (Use Your Illusion I) ──
    "2206780":  ("Guns N' Roses", "Use Your Illusion I (GEF-24415, Geffen US 1991, 2LP)"),
    "25133437": ("Guns N' Roses", "Use Your Illusion I (00602445117307, Geffen US/EU 2022, 2LP 180g)"),

    # ── LIMP BIZKIT ──
    "3782742":  ("Limp Bizkit", "Significant Other (INT2-90335, Interscope/Flip US 1999, 2LP)"),
    "10821533": ("Limp Bizkit", "Significant Other (B0026803-01, Flip/Interscope/UMe US 2017, 2LP reissue)"),

    # ── STORMZY ──
    "10996313": ("Stormzy", "Gang Signs & Prayer (MRKY001LP, #Merky Records UK 2017, 2LP)"),
    "15841340": ("Stormzy", "Heavy Is the Head (0190295403027, #Merky Records/Atlantic UK 2020, 2LP 45RPM)"),

    # ── SKEPTA ──
    "10305796": ("Skepta", "Konnichiwa (BBKS004LP, Boy Better Know UK 2017, LP rood vinyl)"),
    "13713464": ("Skepta", "Ignorance Is Bliss (Boy Better Know UK 2019, 2LP gatefold)"),

    # ── BILLY JOEL ──
    "9603231":  ("Billy Joel", "Piano Man (KC 32544, Columbia US 1973, LP)"),
    "3333076":  ("Billy Joel", "Piano Man (MFSL 1-349, Mobile Fidelity US 2011, LP 180g)"),

    # ── JOHN LENNON ──
    "376138":   ("John Lennon", "Imagine (SW 3379, Apple Records US 1971, LP)"),
    "2590105":  ("John Lennon", "Imagine (MFSL 1-277, Mobile Fidelity US 2003, LP 180g)"),

    # ── THE CRANBERRIES ──
    "501415":   ("The Cranberries", "No Need to Argue (524050-1, Island Records UK/EU 1994, LP)"),
    "6986271":  ("The Cranberries", "No Need to Argue (PLAIN202, Plain Recordings US 2015, LP 180g)"),

    # ── PINK FLOYD ──
    "495681":   ("Pink Floyd", "The Wall (PC2 36183, Columbia US 1979, 2LP)"),
    "8961614":  ("Pink Floyd", "The Wall (PFRLP11, Pink Floyd Records EU 2016, 2LP 180g)"),

    # ── THE VERVE ──
    "445878":   ("The Verve", "Urban Hymns (HUTLP 45, Hut Recordings/Virgin UK 1997, 2LP)"),
    "9057753":  ("The Verve", "Urban Hymns (4787014, Virgin EMI EU 2016, 2LP 180g)"),

    # ── R.E.M. ──
    "2024524":  ("R.E.M.", "Out of Time (9 26496-1, Warner Bros. US 1991, LP)"),
    "9359884":  ("R.E.M.", "Out of Time (Concord/Warner US/EU 2016, LP 180g 25th Anniversary)"),

    # ── VAN DIK HOUT ──
    "13488794": ("Van Dik Hout", "Van Dik Hout (Polydor/Universal NL 2019, 2LP 180g blauw jubileumeditie)"),

    # ── BRYAN ADAMS ──
    "1469334":  ("Bryan Adams", "Reckless (SP-5013, A&M Canada 1984, LP)"),
    "6275103":  ("Bryan Adams", "Reckless (3783059, A&M UK/EU 2014, 2LP 180g 30th Anniversary)"),

    # ── DIRE STRAITS ──
    "382417":   ("Dire Straits", "Brothers in Arms (VERH 25, Vertigo UK 1985, LP)"),
    "17896735": ("Dire Straits", "Brothers in Arms (Vertigo/Universal EU 2021, 2LP 180g 45RPM half-speed)"),

    # ── THE SCENE ──
    "864382":   ("The Scene", "Blauw (Phonogram NL 1990, LP)"),
    "13670805": ("The Scene", "Blauw (Music On Vinyl/Universal NL 2019, LP reissue)"),

    # ── LOUIS ARMSTRONG ──
    "4194515":  ("Louis Armstrong", "What a Wonderful World (ABCS-650, ABC Records US 1968, LP stereo)"),

    # ── NICKELBACK ──
    "1982122":  ("Nickelback", "Silver Side Up (Roadrunner EU 2002, LP — eerste vinyl persing)"),
    "10533002": ("Nickelback", "Silver Side Up (Roadrunner EU 2017, LP reissue)"),
    "10518961": ("Nickelback", "All the Right Reasons (Roadrunner EU 2017, LP — eerste vinyl persing)"),

    # ── SOFT CELL ──
    "238877":   ("Soft Cell", "Non-Stop Erotic Cabaret (BIZL 1, Some Bizzare/Phonogram UK 1981, LP)"),
    "6063431":  ("Soft Cell", "Non-Stop Erotic Cabaret (Mercury/Universal EU 2014, LP 180g)"),

    # ── DEPECHE MODE ──
    "1217497":  ("Depeche Mode", "Speak & Spell (STUMM 5, Mute Records UK 1981, LP)"),
    "8953011":  ("Depeche Mode", "Speak & Spell (Sony Music/Mute EU 2016, LP 180g gatefold)"),

    # ── FRANK SINATRA ──
    "804778":   ("Frank Sinatra", "That's Life (F 1020, Reprise US 1966, LP mono)"),
    "8212247":  ("Frank Sinatra", "That's Life (Reprise/Sinatra Centennial EU/US 2016, LP 180g)"),

    # ── COOLIO ──
    "1410402":  ("Coolio", "Gangsta's Paradise (TB 1141, Tommy Boy US 1995, LP)"),
    "3480688":  ("Coolio", "Gangsta's Paradise (Tommy Boy/Island/Polydor EU 1995, LP)"),

    # ── WHEATUS ──
    "14950419": ("Wheatus", "Wheatus (Wheatus Records EU 2020, LP 180g turquoise RSD)"),

    # ── EURYTHMICS ──
    "66534":    ("Eurythmics", "Sweet Dreams (Are Made of This) (RCALP 6063, RCA UK 1983, LP)"),
    "11860171": ("Eurythmics", "Sweet Dreams (Are Made of This) (RCA/Legacy/Sony EU 2018, LP 180g)"),
}

# ─── GENRE CATEGORISATIE ──────────────────────────────────────────────────────

GROUP_GENRES = {
    # Rock
    "Oasis":             "Rock",
    "RHCP":              "Rock",
    "Beatles":           "Rock",
    "Queen":             "Rock",
    "Green Day":         "Rock",
    "The Killers":       "Rock",
    "Eagles":            "Rock",
    "Fleetwood Mac":     "Rock",
    # Hard Rock / Metal
    "Nirvana":                   "Hard Rock / Metal",
    "AC/DC":                     "Hard Rock / Metal",
    "Guns N' Roses":             "Hard Rock / Metal",
    "Metallica":                 "Hard Rock / Metal",
    "Linkin Park":               "Hard Rock / Metal",
    "Rage Against the Machine":  "Hard Rock / Metal",
    "Coldplay":                  "Rock",
    "Golden Earring":            "Rock",
    "Toto":                      "Rock",
    "Bon Jovi":                  "Rock",
    # Pop
    "Michael Jackson":   "Pop",
    "Doe Maar":          "Pop",
    "ABBA":              "Pop",
    "Gorillaz":          "Pop",
    # Soul / R&B
    "Amy Winehouse":     "Soul / R&B",
    # Reggae
    "Bob Marley":        "Reggae",
    # Hip-Hop
    "Notorious B.I.G.":  "Hip-Hop",
    "Kendrick Lamar":    "Hip-Hop",
    "Mobb Deep":         "Hip-Hop",
    "Flatbush Zombies":  "Hip-Hop",
    "Beast Coast":       "Hip-Hop",
    "Sticks":            "Hip-Hop",
    "21 Savage":         "Hip-Hop",
    "50 Cent":           "Hip-Hop",
    "Drake":             "Hip-Hop",
    "J. Cole":           "Hip-Hop",
    "Young Thug":        "Hip-Hop",
    "JACKBOYS":          "Hip-Hop",
    "Pop Smoke":         "Hip-Hop",
    "Travis Scott":      "Hip-Hop",
    "Migos":             "Hip-Hop",
    "2Pac":              "Hip-Hop",
    "Metro Boomin":      "Hip-Hop",
    "Fugees":            "Hip-Hop",
    # Rock & Roll
    "Elvis Presley":     "Rock & Roll",
    "Rolling Stones":    "Rock",
    "Led Zeppelin":      "Rock",
    "The Police":        "Rock",
    "U2":                "Rock",
    "The Clash":         "Rock",
    # Ska
    "Madness":           "Ska",
    "10cc":              "Rock",
    # Nederlandstalig
    "Rowwen Hèze":       "Nederlandstalig",
    "Janse Bagge Bend":  "Nederlandstalig",
    "Racoon":            "Nederlandstalig",
    "BLØF":              "Nederlandstalig",
    "De Dijk":           "Nederlandstalig",
    "André Hazes":       "Nederlandstalig",
    # Soul / R&B (aanvulling)
    "Marvin Gaye & Tammi Terrell": "Soul / R&B",
    # Nederlandstalig (aanvulling)
    "Ramses Shaffy":               "Nederlandstalig",
    "Drukwerk":                    "Nederlandstalig",
    # Reggae (aanvulling)
    "UB40":                        "Reggae",
    # Rock (aanvulling)
    "Sting":                       "Rock",
    # Hip-Hop (aanvulling)
    "Jay-Z & Kanye West":          "Hip-Hop",
    "Stormzy":                     "Hip-Hop",
    "Skepta":                      "Hip-Hop",
    # Rock (aanvulling)
    "KISS":                        "Rock",
    "ZZ Top":                      "Rock",
    "Radiohead":                   "Rock",
    "Lynyrd Skynyrd":              "Rock",
    "John Lennon":                 "Rock",
    "The Cranberries":             "Rock",
    "Pink Floyd":                  "Rock",
    "The Verve":                   "Rock",
    "Billy Joel":                  "Rock",
    # Hard Rock / Metal (aanvulling)
    "Foo Fighters":                "Hard Rock / Metal",
    "Limp Bizkit":                 "Hard Rock / Metal",
    # Rock (aanvulling batch 3)
    "R.E.M.":                      "Rock",
    "Bryan Adams":                 "Rock",
    "Dire Straits":                "Rock",
    "Nickelback":                  "Rock",
    "The Verve":                   "Rock",
    "Wheatus":                     "Rock",
    "Soft Cell":                   "Pop",
    "Depeche Mode":                "Pop",
    "Eurythmics":                  "Pop",
    # Nederlandstalig (aanvulling batch 3)
    "Van Dik Hout":                "Nederlandstalig",
    "The Scene":                   "Nederlandstalig",
    # Jazz
    "Louis Armstrong":             "Jazz",
    "Frank Sinatra":               "Jazz",
    # Hip-Hop (aanvulling batch 3)
    "Coolio":                      "Hip-Hop",
}
GENRE_ORDER = ["Rock", "Hard Rock / Metal", "Pop", "Soul / R&B", "Reggae", "Ska", "Hip-Hop", "Rock & Roll", "Jazz", "Nederlandstalig"]

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

_fx_rates: dict = {}

def _get_fx_rates() -> dict:
    global _fx_rates
    if _fx_rates:
        return _fx_rates
    try:
        r = std_requests.get("https://api.frankfurter.app/latest", timeout=5)
        r.raise_for_status()
        _fx_rates = {**r.json().get("rates", {}), "EUR": 1.0}
    except Exception:
        _fx_rates = {"EUR": 1.0, "USD": 1.08, "GBP": 0.86, "CAD": 1.48,
                     "AUD": 1.65, "CHF": 0.96, "JPY": 163.0, "SEK": 11.4,
                     "DKK": 7.46, "NOK": 11.8}
    return _fx_rates

def _to_eur(price: float, currency: str) -> float:
    """Convert price to EUR. frankfurter rates: 1 EUR = rates[currency]."""
    return price / _get_fx_rates().get(currency, 1.0)

def _fmt_eur(eur: float) -> str:
    return f"€ {eur:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _shipping_breakdown(best: dict) -> str:
    """Return parenthesised breakdown string when it adds useful info."""
    ship     = best.get("shipping", 0.0)
    currency = best["currency"]
    price    = best["price"]
    parts = []
    if currency != "EUR":
        parts.append(fmt_listing_price(price, currency))
    if ship > 0:
        ship_eur = _to_eur(ship, currency)
        parts.append(f"+ {_fmt_eur(ship_eur)} verzend")
    if parts:
        return f' <span class="muted" style="font-size:11px">({" ".join(parts)})</span>'
    return ""

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

_SYMBOL_TO_CURRENCY = {"€": "EUR", "£": "GBP", "$": "USD", "¥": "JPY", "": "EUR"}

def parse_history_html(html):
    pattern = re.compile(
        r'sales-history-row[^"]*"[^>]*>.*?'
        r'data-header="Order Date:">\s*([\d\-]+)\s*</td>.*?'
        r'data-header="Media:">\s*([^<]+?)\s*</td>.*?'
        r'data-header="Sleeve:">\s*([^<]+?)\s*</td>.*?'
        r'class="price">\s*([€£\$¥]?)\s*([\d\.,]+)',
        re.DOTALL
    )
    sales = []
    for m in pattern.finditer(html):
        date, media_raw, sleeve_raw, symbol, price_str = m.groups()
        media    = CONDITION_MAP.get(media_raw.strip(),  media_raw.strip())
        sleeve   = CONDITION_MAP.get(sleeve_raw.strip(), sleeve_raw.strip())
        currency = _SYMBOL_TO_CURRENCY.get(symbol, "EUR")
        try:
            price = float(price_str.replace(",", ""))
        except ValueError:
            continue
        sales.append({"date": date.strip(), "media": media, "sleeve": sleeve,
                      "price": price, "currency": currency})
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

        # Shipping cost (optional; same currency as listing price)
        ship_match = re.search(
            r'class="item_shipping[^"]*"[^>]*>.*?\+\s*(?:[€£\$]|[A-Z]{3})?\s*([\d]+(?:[.,][\d]+)?)',
            block, re.DOTALL
        )
        shipping = 0.0
        if ship_match:
            raw = ship_match.group(1)
            if "," in raw and "." not in raw:
                raw = raw.replace(",", ".")
            try:
                shipping = float(raw)
            except ValueError:
                shipping = 0.0

        total_eur = _to_eur(price + shipping, currency)

        # Ships from country
        loc_match = re.search(r'class="seller_location"[^>]*>.*?<strong>([^<]+)</strong>', block, re.DOTALL)
        if not loc_match:
            loc_match = re.search(
                r'[Ss]hips\s+[Ff]rom:?\s*(?:<[^>]*>)*\s*([A-Za-z][A-Za-z\s]+?)(?:\s*<|\s*$)',
                block, re.DOTALL
            )
        ships_from = loc_match.group(1).strip() if loc_match else ""

        # Listing date — <time datetime="YYYY-MM-DD..."> within the block
        time_match = re.search(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})', block)
        listed_date = time_match.group(1) if time_match else None

        listings.append({
            "media":        media,
            "sleeve":       sleeve,
            "price":        price,
            "currency":     currency,
            "shipping":     shipping,
            "total_eur":    total_eur,
            "rating_count": rating_count,
            "seller":       seller,
            "ships_from":   ships_from,
            "listed_date":  listed_date,
        })

    return listings


_NEUTRAL_SLEEVES = {"Generic", "No Cover", ""}

def _effective_cond(media: str, sleeve: str) -> str:
    """Slechtste van disc en sleeve als vergelijkingsconditie.
    Generic/No Cover hoezen tellen niet mee — die zijn neutraal.
    Geeft ook terug of de sleeve de beperkende factor was."""
    if sleeve in _NEUTRAL_SLEEVES:
        return media
    rank = {c: i for i, c in enumerate(CONDITION_ORDER)}
    r_m = rank.get(media, 0)
    r_s = rank.get(sleeve, 0)
    return sleeve if r_s > r_m else media


def get_best_listings(listings):
    """Cheapest listing per EFFECTIVE condition (disc vs sleeve, worst wins) from sellers with >= MIN_SELLER_RATINGS.
    Non-EU listings are penalised by estimated Belgian import costs (BTW + douane + bpost) so they only
    win when they remain cheaper even after invoertaks."""
    best = {}
    for listing in listings:
        if listing["rating_count"] < MIN_SELLER_RATINGS:
            continue
        listing = dict(listing)
        if "total_eur" not in listing:
            listing["total_eur"] = _to_eur(listing["price"] + listing.get("shipping", 0.0), listing["currency"])
        ships_from = listing.get("ships_from", "")
        if ships_from:
            is_eu = ships_from in EU_COUNTRIES
        else:
            # Fallback voor cache-entries zonder ships_from: valuta als proxy
            # EUR/GBP → vermoedelijk EU/UK; alles anders (USD, CAD, AUD, JPY...) → non-EU
            is_eu = listing.get("currency", "EUR") in ("EUR", "GBP")
        adj = _non_eu_adjusted_total(listing["total_eur"]) if not is_eu else listing["total_eur"]
        listing["_is_eu"]     = is_eu
        listing["_adj_total"] = adj
        eff = _effective_cond(listing["media"], listing["sleeve"])
        if eff not in best or adj < best[eff]["_adj_total"]:
            best[eff] = listing
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
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            import shutil
            backup = path + ".corrupt"
            shutil.move(path, backup)
            print(f"Waarschuwing: {path} is corrupt en hernoemd naar {backup}. Wordt opnieuw opgebouwd.")
    return {}

def save_cache(path, data):
    # Schrijf eerst naar tijdelijk bestand, dan atomisch hernoemen (voorkomt halve writes)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def cache_is_fresh(entry, max_days=CACHE_DAYS, max_hours=None):
    try:
        raw = entry.get("fetched_at", "")
        try:
            fetched = datetime.fromisoformat(raw)
        except ValueError:
            fetched = datetime.strptime(raw, "%Y-%m-%d")
        age = datetime.now() - fetched
        if max_hours is not None:
            return age.total_seconds() < max_hours * 3600
        return age.days < max_days
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

def get_release_thumb(release_id):
    """Haal album-cover thumbnail URL op via Discogs API (gecached in THUMB_CACHE)."""
    try:
        r = std_requests.get(
            f"https://api.discogs.com/releases/{release_id}",
            headers=DISCOGS_HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            thumb = data.get("thumb", "")
            if not thumb:
                imgs = data.get("images", [])
                if imgs:
                    thumb = imgs[0].get("uri150", "") or imgs[0].get("uri", "")
            return thumb
    except Exception:
        pass
    return ""

def scrape_listings_api(release_id):
    """Listings ophalen via officiële Discogs API — werkt ook vanuit GitHub Actions."""
    # Poging 1: marketplace/search endpoint
    try:
        r = std_requests.get(
            "https://api.discogs.com/marketplace/search",
            headers=DISCOGS_HEADERS,
            params={
                "release_id": release_id,
                "status":     "For Sale",
                "sort":       "price",
                "sort_order": "asc",
                "per_page":   50,
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
        elif r.status_code == 404:
            # Endpoint niet gevonden of geen listings — probeer alternatief
            print(f"  marketplace/search 404 (body: {r.text[:120].strip()!r}) — probeer releases endpoint")
            data = None
        else:
            print(f"  marketplace/search {r.status_code}: {r.text[:120].strip()!r}")
            data = None
    except Exception as e:
        print(f"  marketplace/search fout: {e}")
        data = None

    # Poging 2: /releases/{id}/marketplace als fallback
    if data is None:
        try:
            r2 = std_requests.get(
                f"https://api.discogs.com/marketplace/listings",
                headers=DISCOGS_HEADERS,
                params={
                    "release_id": release_id,
                    "status":     "For Sale",
                    "sort":       "price",
                    "sort_order": "asc",
                    "per_page":   50,
                },
                timeout=15,
            )
            if r2.status_code == 200:
                data = r2.json()
                print(f"  releases-listings endpoint werkt (sleutels: {list(data.keys())[:5]})")
            else:
                print(f"  marketplace/listings {r2.status_code}: {r2.text[:120].strip()!r}")
                return []
        except Exception as e:
            print(f"  marketplace/listings fout: {e}")
            return []

    listings = []
    for item in data.get("listings", data.get("results", [])):
        media  = CONDITION_MAP.get(item.get("condition", ""),        item.get("condition", ""))
        sleeve = CONDITION_MAP.get(item.get("sleeve_condition", ""), item.get("sleeve_condition", "Generic"))
        price_obj    = item.get("price", {})
        price        = float(price_obj.get("value", 0))
        currency     = price_obj.get("currency", "EUR")
        seller_obj   = item.get("seller", {})
        seller       = seller_obj.get("username", "?")
        rating_count = int(seller_obj.get("stats", {}).get("total", 0))
        ships_from   = item.get("ships_from", "")
        ship_obj     = item.get("shipping_price", {})
        shipping     = float(ship_obj.get("value", 0.0)) if ship_obj else 0.0
        ship_cur     = ship_obj.get("currency", currency) if ship_obj else currency
        # Shipping mag een andere valuta hebben dan de listing; converteer naar EUR
        shipping_eur = _to_eur(shipping, ship_cur)
        total_eur    = _to_eur(price, currency) + shipping_eur
        posted       = item.get("posted", "")
        listed_date  = posted[:10] if posted else None
        if price > 0:
            listings.append({
                "media": media, "sleeve": sleeve,
                "price": price, "currency": currency,
                "shipping": shipping,
                "total_eur": total_eur,
                "rating_count": rating_count, "seller": seller,
                "ships_from": ships_from,
                "listed_date": listed_date,
            })
    return listings


LISTING_DATES_CACHE = "vinyl_listing_dates_cache.json"

def _is_investment_release(release_id):
    info = RELEASE_INFO.get(str(release_id)) or RELEASE_INFO.get(release_id)
    return bool(info and info[0].startswith("🏆"))

def fetch_release_listing_dates(release_id):
    """Haalt posted-datum op voor alle listings van een release via de Discogs API."""
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
        print(f"  Listing dates fout {release_id}: {e}")
        return {}

    dates = {}
    for item in data.get("listings", []):
        media    = CONDITION_MAP.get(item.get("condition",        ""), item.get("condition",        ""))
        sleeve   = CONDITION_MAP.get(item.get("sleeve_condition", ""), item.get("sleeve_condition", "Generic"))
        price    = float(item.get("price", {}).get("value", 0))
        currency = item.get("price",  {}).get("currency", "EUR")
        seller   = item.get("seller", {}).get("username", "?")
        posted   = item.get("posted", "")
        if price > 0 and posted:
            key = f"{release_id}_{seller}_{price}_{currency}_{media}_{sleeve}"
            dates[key] = posted[:10]  # YYYY-MM-DD
    return dates

def enrich_listing_dates(results):
    """Voegt plaatsingsdatum toe aan listings van beleggingsplaten via Discogs API (gecached per dag)."""
    dates_cache = load_cache(LISTING_DATES_CACHE)
    today   = datetime.now().strftime("%Y-%m-%d")
    changed = False

    for r in results:
        if not _is_investment_release(r["id"]):
            continue
        release_id = str(r["id"])
        fetch_key  = f"__fetched__{release_id}"
        if dates_cache.get(fetch_key) != today:
            print(f"  Datums ophalen: {r['group']} — {r['title']}")
            new_dates = fetch_release_listing_dates(release_id)
            dates_cache.update(new_dates)
            dates_cache[fetch_key] = today
            changed = True
            time.sleep(0.5)

        for listing in r.get("listings", []):
            key = (f"{release_id}_{listing['seller']}_{listing['price']}"
                   f"_{listing['currency']}_{listing['media']}_{listing['sleeve']}")
            if key in dates_cache:
                listing["listed_date"] = dates_cache[key]

    if changed:
        save_cache(LISTING_DATES_CACHE, dates_cache)
    return results

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

_BADGE_MAP = {
    "🏆 First pressing":            "rb-badge-first",
    "🎵 Luisterversie":             "rb-badge-listen",
    "🎵 Audiofiele versie":          "rb-badge-listen",
    "🎁 Limited edition":            "rb-badge-limited",
    "🎁 Jubileumeditie":             "rb-badge-limited",
    "🎁 Zeldzame variant":           "rb-badge-limited",
    "🎁 Gekleurd vinyl":             "rb-badge-limited",
    "📀 EU origineel (niet first)":  "rb-badge-missing",
    "📀 US origineel (niet first)":  "rb-badge-missing",
}

def _album_name(title_str):
    """Extract 'Album Name' from 'Album Name (pressing info)' format."""
    idx = title_str.find(" (")
    return title_str[:idx] if idx > 0 else title_str

def _pressing_info(title_str):
    """Extract 'pressing info' from 'Album Name (pressing info)' format."""
    start = title_str.find(" (")
    if start > 0 and title_str.endswith(")"):
        return title_str[start + 2:-1]
    return title_str

_VINYL_PLACEHOLDER_SVG = (
    '<svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="40" cy="40" r="40" fill="#0d1a2e"/>'
    '<circle cx="40" cy="40" r="32" fill="#111827"/>'
    '<circle cx="40" cy="40" r="28" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="1.5"/>'
    '<circle cx="40" cy="40" r="23" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="1.5"/>'
    '<circle cx="40" cy="40" r="18" fill="none" stroke="rgba(255,255,255,.05)" stroke-width="1.5"/>'
    '<circle cx="40" cy="40" r="13" fill="none" stroke="rgba(255,255,255,.04)" stroke-width="1.5"/>'
    '<circle cx="40" cy="40" r="8" fill="#0d1a2e"/>'
    '<circle cx="40" cy="40" r="3.5" fill="#1f2937"/>'
    '</svg>'
)

def _render_album_header(artist, album_name, thumb_url=None):
    """Render de klikbare album-header als <summary> — maakt het album inklapbaar."""
    if thumb_url:
        img_html = (
            f'<img class="album-cover" src="{thumb_url}" '
            f'alt="{album_name}" width="80" height="80" '
            f'loading="lazy" referrerpolicy="no-referrer">'
        )
    else:
        img_html = f'<div class="album-cover album-cover-ph">{_VINYL_PLACEHOLDER_SVG}</div>'
    return (
        f'<summary class="album-hdr">'
        f'{img_html}'
        f'<div class="album-hdr-text">'
        f'<div class="album-hdr-name">{album_name}</div>'
        f'<div class="album-hdr-artist">{artist}</div>'
        f'</div>'
        f'<div class="album-hdr-chevron">&#8250;</div>'
        f'</summary>'
    )


def _render_single_rb(r):
    """Render één release card (<div class='rb'>)."""
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
            mc      = best["media"].replace("+","p").replace("-","m")
            sc      = best["sleeve"].replace("+","p").replace("-","m")
            lhref   = f"https://www.discogs.com/sell/release/{r['id']}?sort=price%2Casc&limit=50"
            eur_tot = best.get("total_eur", best["price"])
            brkdwn  = _shipping_breakdown(best)
            non_eu_note = ""
            if not best.get("_is_eu", True) and best.get("ships_from"):
                imp_cost = _non_eu_adjusted_total(eur_tot) - eur_tot
                non_eu_note = (
                    f' <span style="background:#FEF9C3;color:#92400E;font-size:10px;font-weight:600;'
                    f'padding:1px 5px;border-radius:4px" '
                    f'title="Non-EU: geschatte invoerkosten ≈ +{_fmt_eur(imp_cost)}">'
                    f'🌍 {best["ships_from"]}</span>'
                )
            best_html = (
                f'<div class="best-listing">'
                f'<span class="best-label">Beste listing:</span> '
                f'<strong>{_fmt_eur(eur_tot)}</strong>{brkdwn}{non_eu_note}'
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
    info = RELEASE_INFO.get(r["id"])
    if info:
        label, desc = info
        badge_cls  = _BADGE_MAP.get(label, "rb-badge-orig")
        badge_html = f'<span class="rb-badge {badge_cls}">{label}</span>'
        desc_html  = f'<p class="rb-desc">{desc}</p>'
    else:
        badge_html = ""
        desc_html  = ""

    pressing = _pressing_info(r["title"])

    return (
        f'<div class="rb">'
        f'<div class="rb-head"><span class="rb-title">{pressing}</span>{badge_html}</div>'
        f'{desc_html}'
        f'<p class="market">{market}</p>'
        f'<div class="conds">{cond_blocks}</div>'
        f'</div>'
    )


def _build_release_cards(group_results, thumbs=None):
    """Render release cards met album-headers; gepaarde releases naast elkaar."""
    thumbs = thumbs or {}
    by_id  = {r["id"]: r for r in group_results}
    processed = set()
    html = ""

    for r in group_results:
        rid = r["id"]
        if rid in processed:
            continue

        partner_id = _PAIR_MAP.get(rid)
        if partner_id and partner_id in by_id:
            if rid in _LEFT_IDS:
                left_r, right_r = r, by_id[partner_id]
            else:
                left_r, right_r = by_id[partner_id], r

            album_name = _album_name(left_r["title"])
            thumb_url  = thumbs.get(left_r["id"]) or thumbs.get(right_r["id"])
            header     = _render_album_header(left_r["group"], album_name, thumb_url)

            left_html  = _render_single_rb(left_r)
            right_html = _render_single_rb(right_r)
            html += (
                f'<details class="album-block">'
                f'{header}'
                f'<div class="album-body">'
                f'<div class="rb-pair">'
                f'<div class="rb-pair-col">'
                f'<div class="rb-pair-role rb-role-invest">&#9830; Belegging</div>'
                f'{left_html}</div>'
                f'<div class="rb-pair-col">'
                f'<div class="rb-pair-role rb-role-listen">&#9834; Luisteren</div>'
                f'{right_html}</div>'
                f'</div>'
                f'</div>'
                f'</details>'
            )
            processed.add(rid)
            processed.add(partner_id)
        else:
            album_name = _album_name(r["title"])
            thumb_url  = thumbs.get(rid)
            header     = _render_album_header(r["group"], album_name, thumb_url)
            html += (
                f'<details class="album-block">'
                f'{header}'
                f'<div class="album-body">'
                f'{_render_single_rb(r)}'
                f'</div>'
                f'</details>'
            )
            processed.add(rid)

    return html

def compute_deals(results):
    """Bereken deals in twee tiers:
    - 'beste': listing onder laagste historische verkoop
    - 'goed':  listing >= DEALS_AVG_PCT% onder historisch gemiddelde (maar boven minimum)
    """
    deals = []
    for r in results:
        best_for_release = get_best_listings(r.get("listings", []))
        by_cond = {}
        for s in r["sales"]:
            by_cond.setdefault(s["media"], []).append(s)
        for cond, best in best_for_release.items():
            # cond is al de effectieve conditie (slechtste van disc/sleeve), zie get_best_listings.
            # Sla eff_cond op voor weergave (bijv. "(vergel. als VG)" badge in HTML).
            eff_cond   = _effective_cond(best["media"], best["sleeve"])  # == cond
            cond_sales = by_cond.get(cond, [])
            if not cond_sales:
                continue
            # Convert historical prices to EUR (old cache entries without "currency" default to EUR)
            prices_eur = [_to_eur(s["price"], s.get("currency", "EUR")) for s in cond_sales]
            mn         = min(prices_eur)
            avg        = sum(prices_eur) / len(prices_eur)
            total_eur  = best.get("total_eur") or _to_eur(
                best["price"] + best.get("shipping", 0.0), best["currency"]
            )
            # Use import-adjusted price for deal threshold comparison (non-EU listings include BTW + handling)
            adj_total  = best.get("_adj_total", total_eur)

            threshold = _deals_avg_pct(avg)
            if adj_total < mn:
                disc = (mn - adj_total) / mn * 100
                deals.append({"r": r, "cond": cond, "eff_cond": eff_cond, "best": best,
                              "mn": mn, "avg": avg,
                              "disc": disc, "disc_vs_avg": (avg - adj_total) / avg * 100,
                              "tier": "beste", "threshold": threshold})
            elif adj_total < avg * (1 - threshold / 100):
                disc_avg = (avg - adj_total) / avg * 100
                deals.append({"r": r, "cond": cond, "eff_cond": eff_cond, "best": best,
                              "mn": mn, "avg": avg,
                              "disc": disc_avg, "disc_vs_avg": disc_avg,
                              "tier": "goed", "threshold": threshold})

    # Beste eerst, daarna goed; binnen elke tier op kortings-% aflopend
    deals.sort(key=lambda x: (0 if x["tier"] == "beste" else 1, -x["disc"]))
    return deals


def _deal_key(d):
    return f"{d['r']['id']}_{d['cond']}"


def find_new_deals(deals, seen):
    """Geeft deals terug die nieuw zijn of waarvan de EUR-totaalprijs >3% gedaald is."""
    new = []
    for d in deals:
        key      = _deal_key(d)
        curr_eur = d["best"].get("total_eur", d["best"]["price"])
        if key not in seen:
            d = dict(d); d["tag"] = "NIEUW"
            new.append(d)
        elif curr_eur < seen[key].get("total_eur", seen[key]["price"]) * 0.97:
            d = dict(d); d["tag"] = "GOEDKOPER"; d["prev_total_eur"] = seen[key].get("total_eur", seen[key]["price"])
            new.append(d)
    return new


def _listing_key(release_id, listing):
    return f"{release_id}_{listing['seller']}_{listing['price']}_{listing['currency']}_{listing['media']}_{listing['sleeve']}"


def compute_new_listings(results, existing=None):
    """Accumuleert nieuwe listings over refreshes heen.
    Bestaande ongelezen items blijven staan; nieuwe worden toegevoegd.
    Items verdwijnen alleen via /mark-read (gebruikersbevestiging)."""
    seen          = load_cache(LISTINGS_SEEN_FILE)
    today         = datetime.now().strftime("%Y-%m-%d")
    cutoff        = (datetime.now() - timedelta(days=LISTINGS_SEEN_DAYS)).strftime("%Y-%m-%d")
    existing      = existing or []
    existing_keys = {nl["key"] for nl in existing}

    new_items = []
    for r in results:
        by_cond = {}
        for s in r["sales"]:
            eff = _effective_cond(s["media"], s.get("sleeve", s["media"]))
            by_cond.setdefault(eff, []).append(s)

        for listing in r.get("listings", []):
            if listing.get("rating_count", 0) < MIN_SELLER_RATINGS:
                continue
            key = _listing_key(r["id"], listing)
            if key in seen or key in existing_keys:
                continue

            eff_cond   = _effective_cond(listing["media"], listing["sleeve"])
            cond_sales = by_cond.get(eff_cond, [])
            if cond_sales:
                prices_eur = [_to_eur(s["price"], s.get("currency", "EUR")) for s in cond_sales]
                avg = sum(prices_eur) / len(prices_eur)
            else:
                avg = None

            total_eur  = listing.get("total_eur") or _to_eur(
                listing["price"] + listing.get("shipping", 0.0), listing["currency"]
            )
            ships_from = listing.get("ships_from", "")
            is_eu      = (ships_from in EU_COUNTRIES) if ships_from else listing.get("currency", "EUR") in ("EUR", "GBP")
            adj_total  = _non_eu_adjusted_total(total_eur) if not is_eu else total_eur
            pct        = (adj_total - avg) / avg * 100 if avg is not None else None

            new_items.append({
                "r":        r,
                "listing":  listing,
                "key":      key,
                "eff_cond": eff_cond,
                "avg":      avg,
                "pct":      pct,
                "total_eur": total_eur,
                "adj_total": adj_total,
                "is_eu":    is_eu,
            })

    # Prune oude entries uit seen file
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    if len(pruned) != len(seen):
        save_cache(LISTINGS_SEEN_FILE, pruned)

    # Bouw lookup: release_id -> by_cond, zodat retained listings herberekend kunnen worden
    by_cond_per_release = {}
    for r in results:
        bc = {}
        for s in r["sales"]:
            eff = _effective_cond(s["media"], s.get("sleeve", s["media"]))
            bc.setdefault(eff, []).append(s)
        by_cond_per_release[r["id"]] = bc

    # Behoud bestaande ongelezen items; herbereken pct als het ontbrak maar nu wel kan
    retained = []
    for nl in existing:
        if nl["key"] in seen:
            continue
        if nl["pct"] is None:
            bc = by_cond_per_release.get(nl["r"]["id"], {})
            cond_sales = bc.get(nl["eff_cond"], [])
            if cond_sales:
                prices_eur = [_to_eur(s["price"], s.get("currency", "EUR")) for s in cond_sales]
                avg = sum(prices_eur) / len(prices_eur)
                nl = dict(nl)
                nl["avg"] = avg
                nl["pct"] = (nl["adj_total"] - avg) / avg * 100
        retained.append(nl)

    combined = retained + new_items
    combined.sort(key=lambda x: (x["pct"] is None, x["pct"] or 0))
    return combined


def send_deals_email(deals, subject_prefix="Nieuwe deals"):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not deals:
        return

    subject = f"Vinyl Tracker — {subject_prefix} ({len(deals)})"
    rows = ""
    for d in deals:
        b        = d["best"]
        tag      = d.get("tag", "NIEUW")
        tier_lbl = "BESTE" if d.get("tier") == "beste" else "GOED"
        color    = "#10B981" if tag == "NIEUW" else "#F59E0B"
        tier_color = "#3B82F6" if tier_lbl == "BESTE" else "#8B5CF6"
        eur_tot  = b.get("total_eur", b["price"])
        prev_eur = d.get("prev_total_eur")
        prev     = (f' <span style="color:#94A3B8;font-size:11px">was {_fmt_eur(prev_eur)}</span>'
                    if prev_eur else "")
        brkdwn   = _shipping_breakdown(b)
        lhref    = (f"https://www.discogs.com/sell/release/{d['r']['id']}"
                    f"?sort=price%2Casc&limit=50")
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;white-space:nowrap">
            <span style="background:{tier_color};color:#fff;font-size:10px;font-weight:700;
                         padding:2px 7px;border-radius:4px;letter-spacing:.3px">{tier_lbl}</span>
            <span style="background:{color};color:#fff;font-size:10px;font-weight:700;
                         padding:2px 7px;border-radius:4px;letter-spacing:.3px;margin-left:3px">{tag}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-size:12px;color:#64748B;white-space:nowrap">{d["r"]["group"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;font-size:13px">{d["r"]["title"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-weight:600;white-space:nowrap">
            {b["media"]} / {b["sleeve"]}
            {f'<span style="font-size:10px;color:#94A3B8">(vergel. als {d.get("eff_cond", d["cond"])})</span>'
             if d.get("eff_cond") and d["eff_cond"] != b["media"] else ""}
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #F1F5F9;
                     font-weight:700;white-space:nowrap">
            {_fmt_eur(eur_tot)}{brkdwn}{prev}
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
                       border-bottom:1px solid #E2E8F0"
                title="Totaalprijs incl. verzending">Prijs incl. verzend</th>
            <th style="padding:9px 12px;text-align:left;font-size:10px;font-weight:600;
                       text-transform:uppercase;letter-spacing:.4px;color:#64748B;
                       border-bottom:1px solid #E2E8F0"
                title="% goedkoper dan historische prijs (excl. verzending)">Korting vs. hist.</th>
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
        with _smtp.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASS)
            smtp.send_message(msg)
        print(f"Email verstuurd: {subject}")
    except Exception as e:
        print(f"Email fout: {e}")


# ─── MIJN COLLECTIE ───────────────────────────────────────────────────────────

def get_discogs_username(token):
    """Haal de ingelogde Discogs-gebruikersnaam op via het token."""
    try:
        r = std_requests.get(
            "https://api.discogs.com/oauth/identity",
            headers={"Authorization": f"Discogs token={token}",
                     "User-Agent": "VinylTracker/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("username", "")
    except Exception as e:
        print(f"  Kon Discogs-gebruikersnaam niet ophalen: {e}")
        return ""


_DISCOGS_COND_MAP = {
    "mint (m)":                       "M",
    "near mint (nm or m-)":            "NM",
    "very good plus (vg+)":            "VG+",
    "very good (vg)":                  "VG",
    "good plus (g+)":                  "G+",
    "good (g)":                        "G",
    "fair (f)":                        "F",
    "poor (p)":                        "P",
    "m":  "M",  "nm": "NM", "vg+": "VG+", "vg": "VG",
    "g+": "G+", "g":  "G",  "f":   "F",   "p":  "P",
}


def _normalize_condition(raw):
    """Vertaal Discogs lange conditienamen naar de korte codes (NM, VG+, enz.)."""
    if not raw:
        return ""
    key = raw.strip().lower()
    return _DISCOGS_COND_MAP.get(key, raw.strip())


def fetch_discogs_collection(username, token):
    """
    Haal de volledige Discogs-collectie op voor `username`.
    Geeft lijst van dicts terug: release_id, artist, title, condition,
    purchase_price (€ of None), date_added.
    """
    headers = {"Authorization": f"Discogs token={token}",
               "User-Agent": "VinylTracker/1.0"}

    # 1. Collectievelden ophalen → zoek 'price paid' / 'betaald' veld-ID
    price_field_id = None
    try:
        fr = std_requests.get(
            f"https://api.discogs.com/users/{username}/collection/fields",
            headers=headers, timeout=10,
        )
        fr.raise_for_status()
        for f in fr.json().get("fields", []):
            name_lower = f.get("name", "").lower()
            if any(kw in name_lower for kw in ("price", "paid", "betaald", "prijs", "cost")):
                price_field_id = f["id"]
                print(f"  Collectie-veld gevonden: '{f['name']}' (id={price_field_id})")
                break
    except Exception as e:
        print(f"  Collectievelden ophalen mislukt: {e}")

    # 2. Alle collectie-items ophalen (gepagineerd)
    items = []
    page  = 1
    while True:
        try:
            cr = std_requests.get(
                f"https://api.discogs.com/users/{username}/collection/folders/0/releases",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            cr.raise_for_status()
            data  = cr.json()
            batch = data.get("releases", [])
            if not batch:
                break
            for item in batch:
                bi      = item.get("basic_information", {})
                rid     = str(bi.get("id", item.get("id", "")))
                title   = bi.get("title", "?")
                artists = bi.get("artists", [{}])
                artist  = re.sub(r'\s*\(\d+\)$', '', artists[0].get("name", "?")).strip() if artists else "?"
                date_added = item.get("date_added", "")[:10]

                # Conditie uit notities (veld 1 = media-conditie in Discogs standaard)
                notes   = {n["field_id"]: n["value"] for n in item.get("notes", [])}
                condition = _normalize_condition(notes.get(1, ""))

                # Aankoopprijs
                purchase_price = None
                if price_field_id and price_field_id in notes:
                    raw = notes[price_field_id].replace(",", ".").strip()
                    raw = re.sub(r"[^\d.]", "", raw)
                    try:
                        purchase_price = float(raw) if raw else None
                    except ValueError:
                        pass

                items.append({
                    "release_id":     rid,
                    "artist":         artist,
                    "title":          title,
                    "condition":      condition,
                    "purchase_price": purchase_price,
                    "date_added":     date_added,
                })

            pages = data.get("pagination", {}).get("pages", 1)
            if page >= pages:
                break
            page += 1
            time.sleep(0.4)
        except Exception as e:
            print(f"  Collectie pagina {page} mislukt: {e}")
            break

    print(f"  {len(items)} items opgehaald uit Discogs-collectie")
    return items


def load_collection():
    raw = load_cache(MY_COLLECTION_FILE)
    return raw if isinstance(raw, dict) else {}


def save_collection(data):
    save_cache(MY_COLLECTION_FILE, data)


def _collection_needs_refresh(data):
    fetched = data.get("fetched_at", "")
    if not fetched:
        return True
    try:
        age = (datetime.now() - datetime.strptime(fetched, "%Y-%m-%d")).days
        return age >= COLLECTION_CACHE_DAYS
    except Exception:
        return True


MY_PRICES_FILE = "my_collection_prices.json"


def load_price_overrides():
    """Laad handmatig ingestelde aankoopprijzen uit my_collection_prices.json."""
    return load_cache(MY_PRICES_FILE)


def import_collection(force=False):
    """
    Laad de collectie uit cache of haal hem op via de Discogs API.
    Voegt handmatige prijsoverschrijvingen toe uit my_collection_prices.json.
    Geeft lijst van collectie-items terug.
    """
    data = load_collection()
    if not force and not _collection_needs_refresh(data) and data.get("items"):
        print(f"  Collectie geladen uit cache ({len(data['items'])} items)")
        items = data["items"]
    else:
        username = DISCOGS_USERNAME
        if not username and DISCOGS_TOKEN:
            username = get_discogs_username(DISCOGS_TOKEN)
        if not username:
            print("  Geen Discogs-gebruikersnaam — collectie overgeslagen")
            items = data.get("items", [])
        else:
            print(f"  Discogs-collectie ophalen voor '{username}'...")
            items = fetch_discogs_collection(username, DISCOGS_TOKEN)
            save_collection({"fetched_at": datetime.now().strftime("%Y-%m-%d"),
                             "username": username,
                             "items": items})

    # Conditie normaliseren (backward compat: cache kan lange namen bevatten)
    for item in items:
        item["condition"] = _normalize_condition(item.get("condition", ""))

    # Prijsoverschrijvingen toepassen (release_id → prijs)
    overrides = load_price_overrides()
    if overrides:
        for item in items:
            rid = item["release_id"]
            if rid in overrides and item.get("purchase_price") is None:
                try:
                    v = overrides[rid]
                    item["purchase_price"] = float(v) if v is not None else None
                except (TypeError, ValueError):
                    pass

    # Nieuwe releases registreren in RELEASES zodat ze ook gescraped worden
    user_rel  = load_cache(USER_RELEASES_FILE)
    new_count = 0
    for item in items:
        rid = item["release_id"]
        if rid not in RELEASES:
            group = item["artist"]
            title = item["title"]
            RELEASES[rid] = (group, title)
        if rid not in user_rel:
            user_rel[rid] = [RELEASES[rid][0], RELEASES[rid][1]]
            new_count += 1
    if new_count:
        save_cache(USER_RELEASES_FILE, user_rel)
        print(f"  {new_count} collectie-releases toegevoegd aan tracker")

    return items


def compute_collection_value(item, sales_cache):
    """
    Bereken de huidige marktwaarde voor één collectie-item op basis van
    recente verkoopprijzen uit de sales-cache.
    Geeft (market_value, num_sales, is_exact_cond) terug.
    """
    rid   = item["release_id"]
    cond  = item.get("condition", "")
    entry = sales_cache.get(rid, {})
    sales = entry.get("sales", [])
    if not sales:
        return None, 0, False

    # Probeer exacte conditie-match, daarna alle beschikbare verkopen
    matches = [s for s in sales if s.get("media") == cond]
    exact   = bool(matches)
    if not matches:
        matches = sales

    prices = [s["price"] for s in matches if s.get("price", 0) > 0]
    if not prices:
        return None, 0, False

    return sum(prices) / len(prices), len(prices), exact


def _build_collection_page(collection_items, sales_cache):
    """Bouw de HTML-pagina 'Mijn Collectie'."""
    if not collection_items:
        return """
    <div class="page" id="mijn-collectie" style="display:none">
      <div class="page-header"><h2>Mijn Collectie</h2></div>
      <div class="card"><p class="no-data" style="padding:24px">
        Geen collectie gevonden. Zet <code>DISCOGS_USERNAME</code> in je config.py
        of voeg hem toe als GitHub Secret.
      </p></div>
    </div>"""

    rows      = []
    total_inv = 0.0
    total_val = 0.0
    unknown_price = 0
    unknown_val   = 0

    for item in collection_items:
        mv, nsales, exact = compute_collection_value(item, sales_cache)
        pp  = item.get("purchase_price")
        cond = item.get("condition", "—") or "—"
        cond_cls = cond.replace("+", "p").replace("-", "m")
        date_str = item.get("date_added", "")[:10] or "—"
        rid  = item["release_id"]
        name = f"{item['artist']} — {item['title']}"

        is_free = (pp == 0)
        pp_str  = "Cadeau" if is_free else (
            f"€ {pp:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if pp is not None else "—"
        )
        if mv is not None:
            mv_base = f"€ {mv:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            cond_lbl = "exact" if exact else "mix"
            mv_str  = f'{mv_base} <span class="mv-sales" title="{nsales} verkopen ({cond_lbl} conditie)">({nsales})</span>'
        else:
            mv_str = "—"

        if pp is not None:
            total_inv += pp
        else:
            unknown_price += 1

        if mv is not None:
            total_val += mv
        else:
            unknown_val += 1

        if pp is not None and mv is not None:
            diff = mv - pp
            if is_free:
                diff_str = f"+{mv:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                pct_str  = "&#x221E;"   # ∞
                diff_cls = "col-green"
            else:
                pct  = (diff / pp * 100) if pp > 0 else 0
                diff_str = f"{'+' if diff >= 0 else ''}{diff:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                pct_str  = f"{'+' if pct >= 0 else ''}{pct:.1f}%"
                if pct >= 10:
                    diff_cls = "col-green"
                elif pct >= 0:
                    diff_cls = "col-lime"
                elif pct >= -15:
                    diff_cls = "col-orange"
                else:
                    diff_cls = "col-red"
            pct = None if is_free else pct
        else:
            diff_str = "—"
            pct_str  = "—"
            diff_cls = ""
            diff = None
            pct  = None

        mv_hint  = ""
        mv_extra = ""

        rows.append({
            "artist": item["artist"], "title": item["title"],
            "cond": cond, "pp": pp, "mv": mv,
            "diff": diff, "pct": pct,
            "html": (
                f'<tr data-group="{item["artist"]}" data-title="{item["title"]}" '
                f'data-cond="{cond}" data-diff="{diff if diff is not None else ""}">'
                f'<td><a href="https://www.discogs.com/release/{rid}" target="_blank" '
                f'style="color:inherit;text-decoration:none">{item["artist"]}</a></td>'
                f'<td>{item["title"]}</td>'
                f'<td><span class="badge bd-{cond_cls}">{cond}</span></td>'
                f'<td class="td-num">{pp_str}</td>'
                f'<td class="td-num">{mv_str}</td>'
                f'<td class="td-num {diff_cls}">{diff_str}</td>'
                f'<td class="td-num {diff_cls}" style="font-weight:600">{pct_str}</td>'
                f'<td class="td-num muted">{date_str}</td>'
                f'</tr>'
            )
        })

    # Sorteer standaard op meeste winst
    rows.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))
    table_rows = "\n".join(r["html"] for r in rows)

    # Samenvattingscijfers
    def _fmt(v):
        s = f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return s

    total_diff = total_val - total_inv
    total_pct  = (total_diff / total_inv * 100) if total_inv > 0 else 0
    diff_sign  = "+" if total_diff >= 0 else ""
    pct_sign   = "+" if total_pct  >= 0 else ""
    diff_col   = "#10B981" if total_diff >= 0 else "#EF4444"

    return f"""
    <div class="page" id="mijn-collectie" style="display:none">
      <div class="page-header">
        <h2>Mijn Collectie</h2>
        <span class="sub">{len(collection_items)} platen</span>
      </div>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-val">{len(collection_items)}</div>
          <div class="stat-lbl">Platen</div>
        </div>
        <div class="stat-card">
          <div class="stat-val" style="font-size:16px">{_fmt(total_inv)}</div>
          <div class="stat-lbl">Totaal betaald</div>
        </div>
        <div class="stat-card">
          <div class="stat-val" style="font-size:16px">{_fmt(total_val)}</div>
          <div class="stat-lbl">Huidige waarde</div>
        </div>
        <div class="stat-card" style="border-top:3px solid {diff_col}">
          <div class="stat-val" style="font-size:16px;color:{diff_col}">{diff_sign}{_fmt(total_diff)}</div>
          <div class="stat-lbl">Winst / Verlies</div>
        </div>
        <div class="stat-card" style="border-top:3px solid {diff_col}">
          <div class="stat-val" style="color:{diff_col}">{pct_sign}{total_pct:.1f}%</div>
          <div class="stat-lbl">Return</div>
        </div>
      </div>
      <div class="card">
        <table class="ov-table sortable-table" id="tbl-coll">
          <thead><tr>
            <th class="th-sort" onclick="sortCollTable(0,this)">Artiest</th>
            <th class="th-sort" onclick="sortCollTable(1,this)">Album</th>
            <th>Cond.</th>
            <th class="th-sort th-r" onclick="sortCollTable(3,this)">Betaald</th>
            <th class="th-sort th-r" onclick="sortCollTable(4,this)">Marktwaarde</th>
            <th class="th-sort th-r" onclick="sortCollTable(5,this)">+/- €</th>
            <th class="th-sort th-r" onclick="sortCollTable(6,this)">+/- %</th>
            <th class="th-sort th-r" onclick="sortCollTable(7,this)">Datum</th>
          </tr></thead>
          <tbody>{table_rows}</tbody>
        </table>
        <p class="muted" style="padding:8px 0 0;font-size:11px">Marktwaarde = gemiddelde van alle beschikbare Discogs-verkopen. Getal tussen haakjes = aantal verkopen (betrouwbaarheid). "mix" = andere condities meegenomen.</p>
      </div>
    </div>"""


def _build_favorites_page(favorites):
    """Bouw de HTML-pagina 'Favoriete Listings' op basis van vinyl_favorites.json."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not favorites:
        rows_html = '<tr id="fav-empty"><td colspan="11" class="no-data">Geen favorieten opgeslagen. Like een listing via ♥ in Nieuwe Listings of Top Deals.</td></tr>'
    else:
        rows = []
        for key, snap in sorted(favorites.items(), key=lambda x: x[1].get("added_at",""), reverse=True):
            pct      = snap.get("pct")
            total    = snap.get("total_eur", 0)
            avg      = snap.get("avg")
            pct_sign = "+" if pct is not None and pct > 0 else ""
            pct_str  = f"{pct_sign}{pct:.0f}%" if pct is not None else "—"
            pct_col  = "#10B981" if pct is not None and pct < -5 else ("#EF4444" if pct is not None and pct > 15 else "inherit")
            avg_str  = f"€ {avg:,.2f}".replace(",","X").replace(".",",").replace("X",".") if avg else "—"
            tot_str  = f"€ {total:,.2f}".replace(",","X").replace(".",",").replace("X",".")
            eff      = snap.get("eff_cond","?")
            ec_cls   = eff.replace("+","p").replace("-","m")
            type_badge = ('<span style="background:#10B981;color:#fff;font-size:10px;font-weight:600;'
                         'padding:1px 6px;border-radius:10px">Deal</span>' if snap.get("type") == "deal"
                         else '<span style="background:#3B82F6;color:#fff;font-size:10px;font-weight:600;'
                         'padding:1px 6px;border-radius:10px">Listing</span>')
            rid      = snap.get("release_id","")
            lhref    = f"https://www.discogs.com/sell/release/{rid}?sort=price%2Casc&limit=50"
            key_safe = key.replace("'", "\\'")
            rows.append(
                f'<tr id="fav-row-{key}">'
                f'<td><span class="rb-group">{snap.get("group","")}</span></td>'
                f'<td class="td-title">{snap.get("title","")}</td>'
                f'<td><span class="badge bd-{ec_cls}">{eff}</span></td>'
                f'<td class="td-num"><strong>{tot_str}</strong></td>'
                f'<td class="td-num muted">{avg_str}</td>'
                f'<td class="td-num" style="font-weight:600;color:{pct_col}">{pct_str}</td>'
                f'<td class="td-seller">{snap.get("seller","")}</td>'
                f'<td>{type_badge}</td>'
                f'<td class="muted" style="font-size:11px">{snap.get("added_at","")}</td>'
                f'<td><button class="deal-dismiss" onclick="event.stopPropagation();removeFavFromPage(\'{key_safe}\',event)" title="Verwijder uit favorieten">&#10005;</button></td>'
                f'<td><a class="btn-link" href="{lhref}" target="_blank" onclick="event.stopPropagation()">Koop &rarr;</a></td>'
                f'</tr>'
            )
        if not rows:
            rows_html = '<tr id="fav-empty"><td colspan="11" class="no-data">Geen favorieten opgeslagen.</td></tr>'
        else:
            rows_html = '<tr id="fav-empty" style="display:none"><td colspan="11" class="no-data">Geen favorieten opgeslagen.</td></tr>' + "\n".join(rows)

    return f"""
    <div class="page" id="favorieten" style="display:none">
      <div class="page-header">
        <h2>Favoriete Listings</h2>
        <span class="sub">Gelikete listings van Nieuwe Listings en Top Deals</span>
      </div>
      <div class="card">
        <table class="ov-table" id="fav-tbl">
          <thead><tr>
            <th>Artiest</th><th>Release</th><th>Conditie</th>
            <th class="th-r">Prijs incl. verzend</th>
            <th class="th-r">Gem. verkoop</th>
            <th class="th-r">% vs gem.</th>
            <th>Verkoper</th><th>Type</th><th>Datum</th><th></th><th></th>
          </tr></thead>
          <tbody id="fav-tbody">{rows_html}</tbody>
        </table>
      </div>
    </div>"""


def build_html(results, static=False, new_listings=None, collection=None):
    now    = datetime.now().strftime("%d/%m/%Y %H:%M")
    groups = list(dict.fromkeys(r["group"] for r in results))
    thumbs = load_cache(THUMB_CACHE)

    # ── Top Deals berekening (nodig voor home stats) ───────────────────────
    deals = compute_deals(results)

    # ── Mijn Collectie pagina ──────────────────────────────────────────────
    sales_cache       = load_cache(SALES_CACHE)
    collection_items  = collection or []
    collection_page   = _build_collection_page(collection_items, sales_cache)
    coll_count        = len(collection_items)

    # ── Favoriete Listings ────────────────────────────────────────────────
    favorites         = load_cache(FAVORITES_FILE)
    favorites_page    = _build_favorites_page(favorites)
    fav_count         = len(favorites)
    fav_saved_keys    = set(favorites.keys())  # voor fav-active check

    # ── Per-artiest pagina's ───────────────────────────────────────────────
    artist_parts = []
    for group in groups:
        gresult = [r for r in results if r["group"] == group]
        cards   = _build_release_cards(gresult, thumbs)
        artist_parts.append(f"""
        <div class="page" id="{_gid(group)}" style="display:none">
          <div class="page-header">
            <h2>{group}</h2>
            <span class="sub">{len(gresult)} release(s)</span>
          </div>
          {cards}
        </div>""")
    artist_pages = "".join(artist_parts)

    # ── Home-overzicht pagina ──────────────────────────────────────────────
    releases_with_listings = sum(1 for r in results if r.get("listings"))
    home_row_parts = []
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
        cheapest = min(all_best, key=lambda x: x.get("total_eur", x["price"])) if all_best else None
        listing_cell = _fmt_eur(cheapest.get("total_eur", cheapest["price"])) if cheapest else "—"
        stats = r["stats"]
        gid   = _gid(r["group"])
        home_row_parts.append(
            f'<tr onclick="showPage(\'{gid}\')" class="home-row">'
            f'<td><span class="rb-group">{r["group"]}</span></td>'
            f'<td class="td-title">{r["title"]}</td>'
            f'<td>{cond_badges}</td>'
            f'<td class="td-num">{listing_cell}</td>'
            f'<td class="td-num">{stats.get("num_for_sale","?")}</td>'
            f'</tr>'
        )
    home_rows = "".join(home_row_parts)
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
          <div class="stat-val">{sum(1 for d in deals if d["tier"]=="beste")}</div>
          <div class="stat-lbl">Beste deals</div>
        </div>
        <div class="stat-card" style="border-top:3px solid #f59e0b">
          <div class="stat-val">{sum(1 for d in deals if d["tier"]=="goed")}</div>
          <div class="stat-lbl">Goede deals</div>
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
    def _deal_rows_html(tier_deals, ref_label):
        if not tier_deals:
            return f'<tr><td colspan="10" class="no-data">Geen deals gevonden.</td></tr>'
        row_parts = []
        for d in tier_deals:
            b       = d["best"]
            r       = d["r"]
            mc      = b["media"].replace("+","p").replace("-","m")
            sc      = b["sleeve"].replace("+","p").replace("-","m")
            cc      = d["cond"].replace("+","p").replace("-","m")
            lhref   = f"https://www.discogs.com/sell/release/{r['id']}?sort=price%2Casc&limit=50"
            eur_tot = b.get("total_eur", b["price"])
            brkdwn  = _shipping_breakdown(b)
            ref_val = fmt(d["mn"]) if ref_label == "Laagste ooit" else fmt(d["avg"])
            non_eu_badge = ""
            if not b.get("_is_eu", True) and b.get("ships_from"):
                imp_cost = _non_eu_adjusted_total(eur_tot) - eur_tot
                non_eu_badge = (
                    f' <span title="Non-EU: geschatte invoerkosten ≈ +{_fmt_eur(imp_cost)} '
                    f'(21% BTW + bpost verwerking)" style="background:#FEF9C3;color:#92400E;'
                    f'font-size:10px;font-weight:600;padding:1px 5px;border-radius:4px;'
                    f'white-space:nowrap;cursor:help">🌍 {b["ships_from"]} +est.{_fmt_eur(imp_cost)}</span>'
                )
            ri_info = RELEASE_INFO.get(str(r["id"]))
            if not ri_info:
                ri_info = RELEASE_INFO.get(r["id"])
            if ri_info:
                ri_label, ri_desc = ri_info
                ri_cls  = _BADGE_MAP.get(ri_label, "rb-badge-orig")
                ri_badge = (f'<br><span class="rb-badge {ri_cls}" style="font-size:9px;margin-left:0;margin-top:3px"'
                            f' title="{ri_desc}">{ri_label}</span>')
            else:
                ri_badge = ""
            deal_key   = f"{r['id']}_{d['cond']}"
            fav_key    = _listing_key(r["id"], b)
            fav_active = "fav-active" if fav_key in fav_saved_keys else ""
            fav_title  = "Verwijder uit favorieten" if fav_active else "Voeg toe aan favorieten"
            _snap = {
                "type": "deal", "added_at": datetime.now().strftime("%Y-%m-%d"),
                "group": r["group"], "title": r["title"], "release_id": str(r["id"]),
                "seller": b["seller"], "rating_count": b.get("rating_count", 0),
                "media": b["media"], "sleeve": b["sleeve"], "eff_cond": d.get("eff_cond", d["cond"]),
                "total_eur": eur_tot, "avg": d["avg"], "pct": -d["disc"],
                "ships_from": b.get("ships_from", ""), "is_eu": b.get("_is_eu", True),
            }
            _snap_json = _html_mod.escape(json.dumps(_snap))
            row_parts.append(
                f'<tr onclick="showPage(\'{_gid(r["group"])}\')" class="home-row" data-deal-key="{deal_key}">'
                f'<td><span class="rb-group">{r["group"]}</span></td>'
                f'<td class="td-title">{r["title"]}{ri_badge}</td>'
                f'<td>'
                f'<span class="badge bd-{mc}">{b["media"]}</span>'
                f' / <span class="badge bd-{sc}">{b["sleeve"]}</span>'
                + (f' <span class="muted" style="font-size:10px">(vergel. als {d["eff_cond"]})</span>'
                   if d.get("eff_cond") and d["eff_cond"] != b["media"] else "")
                + f'</td>'
                f'<td class="td-num"><strong>{_fmt_eur(eur_tot)}</strong>{brkdwn}{non_eu_badge}</td>'
                f'<td class="td-num">{ref_val}</td>'
                f'<td class="td-num"><span class="deal-pct">-{d["disc"]:.0f}%</span></td>'
                f'<td class="td-seller">{b["seller"]} <span class="muted">({b["rating_count"]:,})</span></td>'
                f'<td><button class="fav-btn {fav_active}" data-fav-key="{fav_key}" data-snapshot="{_snap_json}" onclick="event.stopPropagation();toggleFavorite(\'{fav_key}\',event)" title="{fav_title}">&#9829;</button></td>'
                f'<td><button class="deal-dismiss" onclick="event.stopPropagation();dismissDeal(\'{deal_key}\',event)" title="Verbergen">&#10005;</button></td>'
                f'<td><a class="btn-link" href="{lhref}" target="_blank" onclick="event.stopPropagation()">Koop &rarr;</a></td>'
                f'</tr>'
            )
        return "".join(row_parts)

    def _deal_table(tier_deals, ref_label):
        return f"""
        <div class="card" style="margin-bottom:24px">
          <table class="ov-table">
            <thead><tr>
              <th>Artiest</th><th>Release</th><th>Disc / Hoes</th>
              <th class="th-r" title="Totaalprijs incl. verzending">Listing incl. verzend</th><th class="th-r">{ref_label}</th>
              <th class="th-r" title="% goedkoper dan historische prijs (excl. verzending)">Korting vs. hist.</th><th>Verkoper</th><th></th><th></th><th></th>
            </tr></thead>
            <tbody>{_deal_rows_html(tier_deals, ref_label)}</tbody>
          </table>
        </div>"""

    beste_deals = [d for d in deals if d["tier"] == "beste"]
    goede_deals = [d for d in deals if d["tier"] == "goed"]

    # ── Nieuwe Listings pagina ────────────────────────────────────────────
    nl = new_listings or []
    _is_investment = _is_investment_release

    def _nl_pct_cell(pct):
        if pct is None:
            return '<td class="td-num muted">—</td>'
        color = "#10B981" if pct < -5 else ("#EF4444" if pct > 15 else "var(--text)")
        sign  = "+" if pct > 0 else ""
        return f'<td class="td-num" style="font-weight:600;color:{color}">{sign}{pct:.0f}%</td>'

    def _nl_rows(items, with_snapshot=True):
        if not items:
            return '<tr><td colspan="9" class="no-data">Geen nieuwe listings.</td></tr>'
        rows = ""
        for nl_item in items:
            r       = nl_item["r"]
            lst     = nl_item["listing"]
            key     = nl_item["key"]
            mc      = lst["media"].replace("+","p").replace("-","m")
            sc      = lst["sleeve"].replace("+","p").replace("-","m")
            lhref   = f"https://www.discogs.com/sell/release/{r['id']}?sort=price%2Casc&limit=50"
            eur_tot = nl_item["total_eur"]
            brkdwn  = _shipping_breakdown(lst)
            non_eu_badge = ""
            if not nl_item["is_eu"] and lst.get("ships_from"):
                imp = nl_item["adj_total"] - eur_tot
                non_eu_badge = (
                    f' <span title="Non-EU: est. +{_fmt_eur(imp)}" style="background:#FEF9C3;'
                    f'color:#92400E;font-size:10px;font-weight:600;padding:1px 5px;'
                    f'border-radius:4px;white-space:nowrap;cursor:help">'
                    f'🌍 {lst["ships_from"]}</span>'
                )
            avg_cell   = _fmt_eur(nl_item["avg"]) if nl_item["avg"] else "—"
            ri_info    = RELEASE_INFO.get(str(r["id"])) or RELEASE_INFO.get(r["id"])
            ri_badge   = ""
            if ri_info:
                ri_cls  = _BADGE_MAP.get(ri_info[0], "rb-badge-orig")
                ri_badge = (f'<br><span class="rb-badge {ri_cls}" style="font-size:9px;margin-left:0;margin-top:3px"'
                            f' title="{ri_info[1]}">{ri_info[0]}</span>')
            fav_active = "fav-active" if key in fav_saved_keys else ""
            fav_title  = "Verwijder uit favorieten" if fav_active else "Voeg toe aan favorieten"
            if with_snapshot:
                _snap = {
                    "type": "listing", "added_at": datetime.now().strftime("%Y-%m-%d"),
                    "group": r["group"], "title": r["title"], "release_id": str(r["id"]),
                    "seller": lst["seller"], "rating_count": lst.get("rating_count", 0),
                    "media": lst["media"], "sleeve": lst["sleeve"], "eff_cond": nl_item["eff_cond"],
                    "total_eur": eur_tot, "avg": nl_item["avg"], "pct": nl_item["pct"],
                    "ships_from": lst.get("ships_from", ""), "is_eu": nl_item["is_eu"],
                }
                snap_attr = f' data-snapshot="{_html_mod.escape(json.dumps(_snap))}"'
            else:
                snap_attr = ""
            rows += (
                f'<tr onclick="showPage(\'{_gid(r["group"])}\')" class="home-row" data-nl-key="{key}"'
                f' data-group="{r["group"]}" data-title="{r["title"]}" data-cond="{nl_item["eff_cond"]}">'
                f'<td><span class="rb-group">{r["group"]}</span></td>'
                f'<td class="td-title">{r["title"]}{ri_badge}</td>'
                f'<td><span class="badge bd-{mc}">{lst["media"]}</span>'
                f' / <span class="badge bd-{sc}">{lst["sleeve"]}</span>'
                + (f' <span class="muted" style="font-size:10px">({nl_item["eff_cond"]})</span>'
                   if nl_item["eff_cond"] != lst["media"] else "")
                + f'</td>'
                f'<td class="td-num"><strong>{_fmt_eur(eur_tot)}</strong>{brkdwn}{non_eu_badge}</td>'
                f'<td class="td-num muted">{avg_cell}</td>'
                + _nl_pct_cell(nl_item["pct"])
                + f'<td class="td-seller">{lst["seller"]} <span class="muted">({lst["rating_count"]:,})</span></td>'
                f'<td><button class="fav-btn {fav_active}" data-fav-key="{key}"{snap_attr} onclick="event.stopPropagation();toggleFavorite(\'{key}\',event)" title="{fav_title}">&#9829;</button></td>'
                f'<td><button class="deal-dismiss" onclick="event.stopPropagation();dismissNewListing(\'{key}\',event)" title="Verbergen">&#10005;</button></td>'
                f'<td><a class="btn-link" href="{lhref}" target="_blank" onclick="event.stopPropagation()">Koop &rarr;</a></td>'
                f'</tr>'
            )
        return rows

    nl_invest = [x for x in nl if _is_investment(x["r"]["id"])]
    nl_overig  = [x for x in nl if not _is_investment(x["r"]["id"])]

    def _nl_table(items, title, color, tid="tbl", with_snapshot=True):
        conds = sorted(set(x["eff_cond"] for x in items if x.get("eff_cond")))
        cond_opts = "\n".join(f'<option value="{c}">{c}</option>' for c in conds)
        rows_html = _nl_rows(items, with_snapshot=with_snapshot)
        return f"""
        <h3 style="margin:0 0 4px;font-size:15px;color:{color}">{title}
          <span style="font-weight:400;color:var(--muted);font-size:12px">({len(items)})</span>
        </h3>
        <div class="filter-bar">
          <input class="filter-input" type="search" placeholder="Zoek artiest of album..."
                 oninput="applyFilters('{tid}')" id="fi-{tid}-q" autocomplete="off">
          <select class="filter-select" onchange="applyFilters('{tid}')" id="fi-{tid}-cond">
            <option value="">Alle condities</option>
            {cond_opts}
          </select>
          <span class="filter-count" id="fi-{tid}-cnt">{len(items)} listings</span>
          <button class="filter-clear" onclick="clearFilters('{tid}')">Wis filters</button>
        </div>
        <div class="card" style="margin-bottom:24px">
          <table class="ov-table" id="{tid}">
            <thead><tr>
              <th class="th-sort" onclick="sortTable('{tid}',0,this)">Artiest<span class="sort-icon"></span></th>
              <th>Release</th>
              <th class="th-sort" onclick="sortTable('{tid}',2,this)">Disc / Hoes<span class="sort-icon"></span></th>
              <th class="th-r th-sort" onclick="sortTable('{tid}',3,this)">Prijs incl. verzend<span class="sort-icon"></span></th>
              <th class="th-r">Gem. verkoop</th>
              <th class="th-r th-sort" onclick="sortTable('{tid}',5,this)">% vs gem.<span class="sort-icon"></span></th>
              <th>Verkoper</th><th></th><th></th><th></th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>"""

    new_listings_page = f"""
    <div class="page" id="new-listings" style="display:none">
      <div class="page-header" style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">
        <h2 style="margin:0">Nieuwe Listings</h2>
        <span class="sub">{now} &nbsp;&middot;&nbsp; {len(nl_invest)} nieuw &nbsp;&middot;&nbsp; verkopers &ge;{MIN_SELLER_RATINGS} ratings</span>
        <button class="btn-mark-all" onclick="markAllRead()" style="margin-left:auto">&#10003; Alles gelezen</button>
      </div>
      <div class="dismissed-bar" id="nl-dismissed-bar">
        <span id="nl-dismissed-n">0</span> listing(s) verborgen &mdash;
        <a href="#" onclick="showHiddenNl();return false" style="color:var(--accent);font-weight:600">Toon alles</a>
      </div>
      {_nl_table(nl_invest, "&#127942; Beleggingen", "#92400E", "nl-invest-tbl")}
    </div>"""

    # ── Beleggingen Listings pagina ──────────────────────────────────────────
    invest_all = []
    for r in results:
        if not _is_investment(r["id"]):
            continue
        by_cond = {}
        for s in r["sales"]:
            eff = _effective_cond(s["media"], s.get("sleeve", s["media"]))
            by_cond.setdefault(eff, []).append(s)
        for listing in r.get("listings", []):
            if listing.get("rating_count", 0) < MIN_SELLER_RATINGS:
                continue
            eff_cond   = _effective_cond(listing["media"], listing["sleeve"])
            cond_sales = by_cond.get(eff_cond, [])
            avg        = (sum(_to_eur(s["price"], s.get("currency", "EUR")) for s in cond_sales)
                          / len(cond_sales)) if cond_sales else None
            total_eur  = listing.get("total_eur") or _to_eur(
                listing["price"] + listing.get("shipping", 0.0), listing["currency"]
            )
            ships_from = listing.get("ships_from", "")
            is_eu      = (ships_from in EU_COUNTRIES) if ships_from else listing.get("currency", "EUR") in ("EUR", "GBP")
            adj_total  = _non_eu_adjusted_total(total_eur) if not is_eu else total_eur
            pct        = (adj_total - avg) / avg * 100 if avg is not None else None
            invest_all.append({
                "r":         r,
                "listing":   listing,
                "key":       _listing_key(r["id"], listing),
                "eff_cond":  eff_cond,
                "avg":       avg,
                "pct":       pct,
                "total_eur": total_eur,
                "adj_total": adj_total,
                "is_eu":     is_eu,
            })
    invest_all.sort(key=lambda x: (x["pct"] is None, x["pct"] or 0))
    invest_release_count = len(set(x["r"]["id"] for x in invest_all))

    invest_listings_page = f"""
    <div class="page" id="invest-listings" style="display:none">
      <div class="page-header">
        <h2>&#127942; Beleggingen &mdash; Alle Listings</h2>
        <span class="sub">{now} &nbsp;&middot;&nbsp; {len(invest_all)} listings &nbsp;&middot;&nbsp; verkopers &ge;{MIN_SELLER_RATINGS} ratings &nbsp;&middot;&nbsp; gesorteerd op % vs gem.</span>
      </div>
      {_nl_table(invest_all, "&#127942; Alle listings beleggingsplaten", "#92400E", "invest-all-tbl", with_snapshot=False)}
    </div>"""

    deals_page = f"""
    <div class="page" id="deals" style="display:none">
      <div class="page-header">
        <h2>Deals</h2>
        <span class="sub">{now} &nbsp;&middot;&nbsp; verkopers &ge;{MIN_SELLER_RATINGS} ratings</span>
      </div>
      <div class="dismissed-bar" id="dismissed-bar">
        <span id="dismissed-n">0</span> deal(s) verborgen &mdash;
        <a href="#" onclick="showHiddenDeals();return false" style="color:var(--accent);font-weight:600">Toon alles</a>
      </div>
      <h3 style="margin:0 0 8px;font-size:15px;color:var(--accent)">
        &#9650; Beste deals
        <span style="font-weight:400;color:var(--muted);font-size:12px">— totaalprijs incl. verzending onder laagste historische verkoopprijs ({len(beste_deals)})</span>
      </h3>
      {_deal_table(beste_deals[:30], "Laagste ooit")}
      <h3 style="margin:16px 0 8px;font-size:15px;color:#f59e0b">
        &#9733; Goede deals
        <span style="font-weight:400;color:var(--muted);font-size:12px">— totaalprijs incl. verzending onder hist. gem. (staffel: &lt;€30→25% / €30–75→20% / €75–200→15% / &gt;€200→10%) ({len(goede_deals)})</span>
      </h3>
      {_deal_table(goede_deals[:30], "Gem. verkoop")}
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
    group_title_map = {}
    for r in results:
        group_title_map.setdefault(r["group"], []).append(r["title"])
    for genre in ordered_genres:
        items = ""
        for group in genre_groups[genre]:
            titles_str = " | ".join(group_title_map.get(group, []))
            items += (
                f'<div class="nav-item" data-page="{_gid(group)}" '
                f'data-titles="{titles_str}" '
                f'onclick="showPage(\'{_gid(group)}\')">{group}</div>\n'
            )
        nav_genres += f"""<details class="nav-genre">
          <summary>{genre}</summary>
          {items}
        </details>\n"""

    nav = f"""
    <nav>
      <div class="nav-logo"><span class="nav-logo-icon">&#9679;</span> Vinyl</div>
      <div class="nav-search-wrap">
        <input class="nav-search" type="search" placeholder="Zoek artiest of album..." oninput="filterNav(this.value)" autocomplete="off">
      </div>
      <div class="nav-item active" data-page="home" onclick="showPage('home')">
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path d="M10 2L2 9h2v9h5v-6h2v6h5V9h2z"/></svg>
        Home
      </div>
      <div class="nav-item" data-page="new-listings" onclick="showPage('new-listings')">
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z"/></svg>
        Nieuwe Listings
        {f'<span class="nav-badge" id="nl-nav-badge" style="background:#3B82F6">{len(nl_invest)}</span>' if nl_invest else ''}
      </div>
      <div class="nav-item" data-page="invest-listings" onclick="showPage('invest-listings')">
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path d="M5 3a2 2 0 00-2 2v1H1v3a3 3 0 002.83 2.98A5 5 0 009 14.9V16H8a1 1 0 000 2h4a1 1 0 000-2h-1v-1.1A5 5 0 0016.17 11.98 3 3 0 0019 9V6h-2V5a2 2 0 00-2-2H5zm11 3h1v2.17A1 1 0 0116 9v-3zm-13 0V9a1 1 0 01-1-.83V6h1zm2-1h10v5a3 3 0 01-10 0V5z"/></svg>
        Beleggingen
      </div>
      <div class="nav-item" data-page="deals" onclick="showPage('deals')">
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M12 7a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0V8.414l-4.293 4.293a1 1 0 01-1.414 0L8 10.414l-4.293 4.293a1 1 0 01-1.414-1.414l5-5a1 1 0 011.414 0L11 10.586 14.586 7H12z" clip-rule="evenodd"/></svg>
        Top Deals
        <span class="nav-badge">{len(deals)}</span>
      </div>
      <div class="nav-item" data-page="favorieten" onclick="showPage('favorieten')">
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M3.172 5.172a4 4 0 015.656 0L10 6.343l1.172-1.171a4 4 0 115.656 5.656L10 17.657l-6.828-6.829a4 4 0 010-5.656z" clip-rule="evenodd"/></svg>
        Favorieten
        {f'<span class="nav-badge" id="fav-nav-badge" style="background:#EF4444">{fav_count}</span>' if fav_count else f'<span class="nav-badge" id="fav-nav-badge" style="background:#EF4444;display:none">0</span>'}
      </div>
      <div class="nav-item" data-page="mijn-collectie" onclick="showPage('mijn-collectie')">
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor"><path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z"/><path fill-rule="evenodd" d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z" clip-rule="evenodd"/></svg>
        Mijn Collectie
        {f'<span class="nav-badge" style="background:#8B5CF6">{coll_count}</span>' if coll_count else ''}
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{{
    --navy:#0B1D3A;--navy2:#162d54;--accent:#10B981;--accent-dim:#059669;
    --bg:#F0F4F8;--surface:#ffffff;--border:#DDE3EC;
    --text:#0F172A;--muted:#5A6A84;--muted2:#94A3B8;
    --deal-bg:#D1FAE5;--deal-fg:#065F46;
    --warn-bg:#FFFBEB;--warn-bdr:#FDE68A;
    --purple:#7C3AED;--purple2:#6D28D9;
    --shadow-sm:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
    --shadow-md:0 4px 8px rgba(0,0,0,.08),0 2px 4px rgba(0,0,0,.05);
    --radius:12px;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
        font-size:14px;color:var(--text);display:flex;height:100vh;height:100dvh;
        overflow:hidden;background:var(--bg);-webkit-font-smoothing:antialiased}}

  /* ── Sidebar ── */
  nav{{width:200px;min-width:200px;background:var(--navy);color:#fff;
       display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0;
       border-right:1px solid rgba(0,0,0,.15)}}
  .nav-logo{{padding:20px 16px 16px;font-size:15px;font-weight:700;letter-spacing:-.3px;
             border-bottom:1px solid rgba(255,255,255,.08);display:flex;align-items:center;gap:10px}}
  .nav-logo-icon{{color:var(--accent);font-size:20px;line-height:1;flex-shrink:0}}
  .nav-section{{padding:16px 16px 5px;font-size:10px;font-weight:600;
                text-transform:uppercase;letter-spacing:.8px;color:rgba(255,255,255,.3)}}
  .nav-item{{padding:9px 14px 9px 16px;cursor:pointer;font-size:13px;
             border-left:3px solid transparent;transition:background .15s,color .15s;
             color:rgba(255,255,255,.65);display:flex;align-items:center;gap:8px;border-radius:0}}
  .nav-item:hover{{background:rgba(255,255,255,.08);color:rgba(255,255,255,.95)}}
  .nav-item.active{{background:rgba(16,185,129,.15);border-left-color:var(--accent);
                   color:#fff;font-weight:600}}
  .nav-icon{{width:14px;height:14px;opacity:.6;flex-shrink:0}}
  .nav-badge{{margin-left:auto;background:var(--accent);color:#fff;
              font-size:10px;font-weight:700;padding:1px 7px;border-radius:10px;min-width:20px;text-align:center}}
  .nav-sep{{height:1px;background:rgba(255,255,255,.08);margin:8px 0}}
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
  .topbar-wrap{{position:sticky;top:0;z-index:50;background:var(--surface)}}
  .topbar{{padding:10px 24px;display:flex;align-items:center;justify-content:flex-end;
           gap:8px;border-bottom:1px solid var(--border);background:var(--surface);
           box-shadow:0 1px 0 var(--border)}}
  .add-panel{{padding:10px 24px;display:none;align-items:center;gap:10px;
              border-bottom:1px solid var(--border);background:var(--surface)}}

  /* ── Buttons ── */
  .btn{{border:none;padding:8px 14px;border-radius:8px;font-size:12.5px;cursor:pointer;
        font-weight:600;display:inline-flex;align-items:center;gap:6px;
        transition:background .15s,opacity .15s,transform .1s;white-space:nowrap}}
  .btn:active{{transform:scale(.97)}}
  .btn-pdf{{background:var(--navy);color:#fff}}
  .btn-pdf:hover{{background:var(--navy2)}}
  .btn-add{{background:var(--purple);color:#fff}}
  .btn-add:hover{{background:var(--purple2)}}
  .btn-refresh{{background:var(--accent);color:#fff}}
  .btn-refresh:hover{{background:var(--accent-dim)}}
  .btn-push{{background:#16a34a;color:#fff}}
  .btn-push:hover{{background:#15803d}}
  .btn-push:disabled{{background:var(--muted2);color:#fff;cursor:not-allowed;opacity:.7}}
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
  .page{{padding:24px 28px 40px;flex:1}}
  .page-header{{display:flex;align-items:baseline;gap:12px;margin-bottom:22px;
                padding-bottom:18px;border-bottom:1px solid var(--border)}}
  h2{{color:var(--text);font-size:22px;font-weight:800;letter-spacing:-.5px}}
  .sub{{color:var(--muted);font-size:12.5px;font-weight:400}}
  .muted{{color:var(--muted)}}

  /* ── Stat cards (home) ── */
  .stat-grid{{display:flex;gap:12px;margin-bottom:22px;flex-wrap:wrap}}
  .stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
              padding:18px 20px;min-width:120px;box-shadow:var(--shadow-sm);
              border-top:3px solid var(--border)}}
  .stat-card-accent{{border-color:#A7F3D0;border-top-color:var(--accent);background:#ECFDF5}}
  .stat-val{{font-size:28px;font-weight:800;color:var(--text);line-height:1;
             font-variant-numeric:tabular-nums;letter-spacing:-1px}}
  .stat-card-accent .stat-val{{color:#065F46}}
  .stat-lbl{{font-size:11px;color:var(--muted);margin-top:6px;font-weight:500;
             text-transform:uppercase;letter-spacing:.4px}}

  /* ── Card wrapper ── */
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
         overflow:hidden;box-shadow:var(--shadow-sm)}}

  /* ── Album blocks (inklapbaar via <details>) ── */
  details.album-block{{margin-bottom:10px}}
  details.album-block:last-child{{margin-bottom:0}}
  /* <summary> = klikbare album-header */
  details.album-block > summary.album-hdr{{
    display:flex;align-items:center;gap:16px;
    padding:14px 20px;
    background:linear-gradient(140deg,var(--navy2) 0%,var(--navy) 100%);
    border:1px solid rgba(255,255,255,.07);
    border-radius:12px;
    position:relative;overflow:hidden;
    cursor:pointer;list-style:none;
    transition:border-radius 0s .18s;
    -webkit-tap-highlight-color:transparent;
    user-select:none}}
  details.album-block > summary.album-hdr::-webkit-details-marker{{display:none}}
  details.album-block > summary.album-hdr::marker{{display:none}}
  details.album-block > summary.album-hdr:hover{{
    background:linear-gradient(140deg,#1f3d73 0%,#132b55 100%)}}
  details.album-block > summary.album-hdr:active{{opacity:.92}}
  /* Open-state: hoekige onderkant (verbindt met body) */
  details.album-block[open] > summary.album-hdr{{
    border-radius:12px 12px 0 0;
    transition:border-radius 0s 0s}}
  .album-hdr::after{{
    content:'';position:absolute;right:16px;top:50%;
    transform:translateY(-50%);
    width:100px;height:100px;border-radius:50%;pointer-events:none;
    background:transparent;
    box-shadow:
      inset 0 0 0 1px rgba(255,255,255,.06),
      0 0 0 12px rgba(255,255,255,.04),
      0 0 0 24px rgba(255,255,255,.03),
      0 0 0 36px rgba(255,255,255,.02)}}
  .album-cover{{
    width:80px;height:80px;border-radius:8px;object-fit:cover;flex-shrink:0;
    box-shadow:0 4px 16px rgba(0,0,0,.55),0 0 0 2px rgba(255,255,255,.12);
    position:relative;z-index:1}}
  .album-cover-ph{{
    width:80px;height:80px;border-radius:8px;flex-shrink:0;
    background:#0d1a2e;display:flex;align-items:center;justify-content:center;
    box-shadow:0 4px 16px rgba(0,0,0,.55);
    position:relative;z-index:1;overflow:hidden}}
  .album-cover-ph svg{{width:80px;height:80px;display:block}}
  .album-hdr-text{{flex:1;min-width:0;position:relative;z-index:1}}
  .album-hdr-name{{
    font-size:17px;font-weight:700;color:#fff;
    letter-spacing:-.3px;line-height:1.2;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .album-hdr-artist{{
    font-size:10.5px;font-weight:700;color:var(--accent);
    text-transform:uppercase;letter-spacing:.9px;margin-top:5px}}
  /* Pijl-indicator rechtsboven */
  .album-hdr-chevron{{
    flex-shrink:0;color:rgba(255,255,255,.45);
    font-size:22px;font-weight:300;line-height:1;
    margin-left:4px;position:relative;z-index:1;
    transition:transform .2s ease}}
  details.album-block[open] .album-hdr-chevron{{transform:rotate(90deg)}}
  /* Body (inhoud) — verbonden aan header */
  .album-body{{
    border:1px solid var(--border);border-top:none;
    border-radius:0 0 12px 12px;overflow:hidden;background:var(--bg)}}
  .album-body > .rb{{
    margin-bottom:0;border-radius:0;border:none;
    border-bottom:1px solid var(--border)}}
  .album-body > .rb:last-child{{border-bottom:none}}

  /* ── Release cards ── */
  .rb{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
       padding:18px 20px;margin-bottom:12px;
       box-shadow:var(--shadow-sm)}}
  .rb-head{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
  .rb-group{{background:var(--navy);color:#fff;font-size:10px;font-weight:700;
             padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.5px;
             white-space:nowrap}}
  .rb-title{{font-size:13.5px;font-weight:600;color:var(--muted);line-height:1.3}}
  .rb-badge{{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:99px;margin-left:4px;vertical-align:middle;white-space:nowrap}}
  .rb-badge-first{{background:#fef3c7;color:#92400e}}
  .rb-badge-listen{{background:#d1fae5;color:#065f46}}
  .rb-badge-limited{{background:#ede9fe;color:#4c1d95}}
  .rb-badge-orig{{background:#e0f2fe;color:#0c4a6e}}
  .rb-badge-missing{{background:#fff7ed;color:#9a3412}}
  .rb-desc{{font-size:12px;color:var(--muted);margin:2px 0 8px;line-height:1.4}}
  /* ── Pair layout ── */
  .rb-pair{{display:flex;gap:0;align-items:stretch}}
  .rb-pair-col{{flex:1;min-width:0;display:flex;flex-direction:column;
               border-right:1px solid var(--border)}}
  .rb-pair-col:last-child{{border-right:none}}
  .rb-pair-col .rb{{margin-bottom:0;border-radius:0;border:none;
                    border-top:1px solid var(--border);flex:1}}
  .rb-pair-col .rb:first-child{{border-top:none}}
  .rb-pair-role{{
    font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;
    padding:6px 16px;border-bottom:1px solid transparent;
    flex-shrink:0}}
  .rb-role-invest{{background:#fef9ec;color:#92400e;border-color:#fde68a}}
  .rb-role-listen{{background:#f0fdf4;color:#065f46;border-color:#bbf7d0}}
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
  table{{border-collapse:collapse;width:100%;font-size:13px}}
  thead th{{background:#F6F8FB;color:var(--muted);padding:10px 14px;text-align:left;
            font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;
            border-bottom:2px solid var(--border)}}
  .th-r{{text-align:right!important}}
  td{{padding:10px 14px;border-bottom:1px solid #EEF2F7;vertical-align:middle}}
  tbody tr:last-child td{{border-bottom:none}}
  .td-num{{text-align:right;font-variant-numeric:tabular-nums}}
  .td-title{{font-size:13px;color:var(--text)}}
  .td-seller{{font-size:12px}}
  .home-row{{cursor:pointer;transition:background .12s}}
  .home-row:hover td{{background:rgba(16,185,129,.04)}}
  .no-data{{color:var(--muted);font-style:italic;font-size:13px;padding:20px 14px}}
  .col-green{{color:#059669;font-weight:600}}
  .col-lime{{color:#16A34A}}
  .col-orange{{color:#D97706}}
  .col-red{{color:#DC2626;font-weight:600}}
  .mv-sales{{font-size:11px;color:var(--muted);font-weight:400;margin-left:3px}}

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
  .deal-dismiss{{background:none;border:none;color:#CBD5E1;cursor:pointer;
                 font-size:13px;padding:2px 6px;border-radius:4px;line-height:1}}
  .deal-dismiss:hover{{background:#FEE2E2;color:#EF4444}}
  .fav-btn{{background:none;border:none;color:#CBD5E1;cursor:pointer;
            font-size:16px;padding:2px 6px;border-radius:4px;line-height:1;transition:color .15s}}
  .fav-btn:hover{{color:#F87171}}
  .fav-btn.fav-active{{color:#EF4444}}
  .btn-mark-all{{background:#3B82F6;border:none;color:#fff;cursor:pointer;
                 font-size:12px;font-weight:600;padding:4px 12px;border-radius:6px;line-height:1.4}}
  .btn-mark-all:hover{{background:#2563EB}}
  .dismissed-bar{{display:none;font-size:12px;color:var(--muted);
                  margin-bottom:12px;padding:6px 12px;background:#F8FAFC;
                  border-radius:6px;border:1px solid var(--border)}}

  /* ── Hamburger ── */
  .hamburger{{display:none;border:none;background:none;cursor:pointer;
              padding:10px;flex-direction:column;justify-content:center;
              align-items:center;gap:5px;min-width:44px;min-height:44px;
              border-radius:6px;-webkit-tap-highlight-color:transparent}}
  .hamburger:active{{background:rgba(0,0,0,.06)}}
  .hamburger span{{display:block;width:20px;height:2px;background:var(--navy);border-radius:2px}}

  /* ── Nav overlay (mobile) ── */
  .nav-overlay{{display:none;position:fixed;top:0;right:0;bottom:0;left:0;background:rgba(0,0,0,.45);
                z-index:199;-webkit-tap-highlight-color:transparent}}
  .nav-overlay.open{{display:block}}

  /* ── Mobile layout ── */
  @media(max-width:768px){{
    html{{height:100%;overflow:hidden}}
    body{{display:block;overflow:hidden;height:100%;width:100%}}
    nav{{position:fixed;top:0;left:0;bottom:0;width:224px;min-width:224px;z-index:200;
         transform:translateX(-100%);transition:transform .25s ease;
         box-shadow:4px 0 24px rgba(0,0,0,.22)}}
    nav.open{{transform:translateX(0)}}
    .hamburger{{display:flex}}
    main{{height:100%;overflow-y:scroll;
          -webkit-overflow-scrolling:touch;overscroll-behavior:contain;width:100%}}
    .topbar{{padding:10px 14px;gap:6px}}
    .topbar .btn,.topbar a.btn{{font-size:11.5px;padding:6px 10px}}
    .page{{padding:16px 14px 52px}}
    h2{{font-size:17px}}
    .sub{{font-size:11px}}
    .page-header{{flex-wrap:wrap;gap:4px}}
    .stat-grid{{gap:8px}}
    .stat-card{{flex:1;min-width:calc(50% - 4px);padding:11px 12px}}
    .stat-val{{font-size:20px}}
    .card{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
    .card table{{min-width:500px}}
    /* Album block mobile */
    details.album-block{{margin-bottom:20px}}
    details.album-block > summary.album-hdr{{padding:12px 14px;gap:12px}}
    details.album-block > summary.album-hdr::after{{display:none}}
    .album-cover,.album-cover-ph{{width:60px;height:60px}}
    .album-cover-ph svg{{width:60px;height:60px}}
    .album-hdr-name{{font-size:15px}}
    /* Pair stacks vertically on mobile */
    .rb-pair{{flex-direction:column}}
    .rb-pair-col{{border-right:none;border-bottom:1px solid var(--border)}}
    .rb-pair-col:last-child{{border-bottom:none}}
    .rb-pair-col .rb{{border-top:1px solid var(--border)}}
    .rb{{padding:14px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch}}
    .conds{{flex-direction:column}}
    .cb{{min-width:0;width:100%}}
    .cb table{{min-width:280px}}
    .best-listing{{font-size:11px}}
    .market{{font-size:11.5px}}
    .rb-title{{font-size:13px}}
    .td-title{{max-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .nav-item{{padding:12px 16px;font-size:13.5px;min-height:44px}}
    .nav-genre summary{{padding:12px 16px;min-height:44px}}
    .add-panel{{flex-wrap:wrap;padding:8px 12px}}
    .add-input{{width:100%;min-width:0}}
  }}

  /* ── Print ── */
  @media print{{
    html{{height:auto;overflow:visible}}
    body{{display:block;height:auto;overflow:visible}}
    nav,.topbar-wrap{{display:none}}
    main{{overflow:visible}}
    .page{{display:block!important;padding:10px}}
    .rb{{box-shadow:none;break-inside:avoid}}
    .cb{{break-inside:avoid}}
    .card{{box-shadow:none}}
  }}

  /* ── Focus ── */
  *:focus-visible{{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}}

  /* ── Page transition ── */
  @keyframes fadeIn{{from{{opacity:0;transform:translateY(4px)}}to{{opacity:1;transform:none}}}}
  .page{{animation:fadeIn 120ms ease-out}}

  /* ── Nav search ── */
  .nav-search-wrap{{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.1)}}
  .nav-search{{width:100%;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.15);
               border-radius:6px;padding:6px 10px;color:#fff;font-size:12px;outline:none}}
  .nav-search::placeholder{{color:rgba(255,255,255,.4)}}
  .nav-search:focus{{border-color:var(--accent);background:rgba(255,255,255,.15)}}

  /* ── Filter bar ── */
  .filter-bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 0 14px}}
  .filter-input{{padding:6px 10px;border:1.5px solid var(--border);border-radius:6px;
                 font-size:12px;color:var(--text);background:var(--surface);outline:none;min-width:140px}}
  .filter-input:focus{{border-color:var(--accent)}}
  .filter-select{{padding:6px 10px;border:1.5px solid var(--border);border-radius:6px;
                  font-size:12px;color:var(--text);background:var(--surface);outline:none;cursor:pointer}}
  .filter-select:focus{{border-color:var(--accent)}}
  .filter-count{{font-size:12px;color:var(--muted);margin-left:auto}}
  .filter-clear{{padding:5px 12px;border:1.5px solid var(--border);border-radius:6px;
                 font-size:12px;color:var(--muted);background:none;cursor:pointer}}
  .filter-clear:hover{{border-color:var(--accent);color:var(--accent)}}

  /* ── Sortable headers ── */
  .th-sort{{cursor:pointer;user-select:none;white-space:nowrap}}
  .th-sort:hover{{color:var(--accent)}}
  .sort-icon{{display:inline-block;margin-left:3px;opacity:.45;font-size:9px}}
  .th-sort.asc .sort-icon::after{{content:"\\25B2"}}
  .th-sort.desc .sort-icon::after{{content:"\\25BC"}}
  .th-sort:not(.asc):not(.desc) .sort-icon::after{{content:"\\21C5"}}

  /* ── Dark mode ── */
  @media(prefers-color-scheme:dark){{
    :root{{
      --bg:#0B1120;--surface:#131C2E;--border:#253550;
      --text:#E8EFF8;--muted:#8A9AB5;--muted2:#4A5A75;
      --deal-bg:#064E3B;--deal-fg:#6EE7B7;
      --warn-bg:#451A03;--warn-bdr:#92400E;
      --shadow-sm:0 1px 3px rgba(0,0,0,.3),0 1px 2px rgba(0,0,0,.2);
      --shadow-md:0 4px 8px rgba(0,0,0,.35),0 2px 4px rgba(0,0,0,.2);
    }}
    /* Hamburger lijnen zichtbaar op donkere topbar */
    .hamburger span{{background:#CBD5E1}}
    .hamburger:active{{background:rgba(255,255,255,.08)}}
    .card{{box-shadow:0 1px 4px rgba(0,0,0,.4)}}
    .topbar-wrap{{background:var(--surface);border-bottom-color:var(--border)}}
    .filter-input,.filter-select{{background:var(--bg);color:var(--text);border-color:var(--border)}}
    /* Tabel */
    table thead th{{background:#0d1628;color:var(--muted)}}
    td{{border-bottom-color:var(--border)}}
    .home-row:hover td{{background:rgba(16,185,129,.06)}}
    /* Condition boxes */
    .cb{{background:#192236;border-color:var(--border)}}
    /* Role headers */
    .rb-role-invest{{background:#2d1700;color:#fbbf24;border-color:#92400e}}
    .rb-role-listen{{background:#091a10;color:#6EE7B7;border-color:#064E3B}}
    /* Release badges */
    .rb-badge-first{{background:#3d2500;color:#fbbf24}}
    .rb-badge-listen{{background:#082014;color:#6EE7B7}}
    .rb-badge-limited{{background:#251648;color:#c4b5fd}}
    .rb-badge-orig{{background:#071b2c;color:#93c5fd}}
    .rb-badge-missing{{background:#2a1000;color:#fb923c}}
    /* Staat-badges */
    .bd-M,.bd-NM{{background:#064E3B;color:#6EE7B7}}
    .bd-VGp{{background:#14532d;color:#86efac}}
    .bd-VG{{background:#3d2800;color:#fbbf24}}
    .bd-Gp{{background:#3a2200;color:#f59e0b}}
    .bd-G{{background:#3d1010;color:#fca5a5}}
    .bd-F{{background:#350a0a;color:#f87171}}
    .bd-P{{background:#7f1d1d;color:#fecaca}}
    .bd-Generic,.bd-NoCover{{background:#1e2d45;color:var(--muted)}}
    /* Link-knop */
    .btn-link{{background:#0f2744;color:#93c5fd;border-color:#1d4ed8}}
    .btn-link:hover{{background:#163560}}
    /* Deal dismiss */
    .deal-dismiss:hover{{background:#3d1010;color:#f87171}}
    /* Dismissed bar */
    .dismissed-bar{{background:var(--surface)}}
    /* Stat cards */
    .nav-search{{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.1)}}
    .stat-card{{background:#152035}}
    .stat-card-accent{{background:#0d2a1a;border-color:#10B981}}
    .stat-card-accent .stat-val{{color:#6EE7B7}}
    /* Dropdown menu (static site) */
    #gh-menu{{background:var(--surface)!important;border-color:var(--border)!important;box-shadow:0 4px 24px rgba(0,0,0,.5)!important}}
    #gh-menu button{{color:var(--text)!important}}
    #gh-menu div{{border-color:var(--border)!important}}
    /* Token modal */
    #gh-modal > div{{background:var(--surface)!important}}
    #gh-modal h3{{color:var(--text)!important}}
    #gh-modal p{{color:var(--muted)!important}}
    #gh-modal input{{background:var(--bg)!important;border-color:var(--border)!important;color:var(--text)!important}}
  }}
</style>
</head>
<body>
<div class="nav-overlay" id="nav-overlay" onclick="toggleNav()"></div>
{nav}
<main>
  <div class="topbar-wrap">
    {"" if not static else f'<div class="topbar"><button class="hamburger" onclick="toggleNav()" aria-label="Menu"><span></span><span></span><span></span></button><span class="sub" style="margin-right:auto">Snapshot: {now}</span><span style="display:inline-flex;gap:1px;position:relative"><button id="gh-refresh-btn" class="btn btn-refresh" style="border-radius:6px 0 0 6px" onclick="ghRefresh(false)">&#8635; Vernieuwen</button><button class="btn btn-refresh" style="border-radius:0 6px 6px 0;padding:6px 8px;border-left:1px solid rgba(255,255,255,.35)" onclick="ghToggleMenu()" aria-label="Opties">&#9662;</button><div id="gh-menu" style="display:none;position:absolute;right:0;top:calc(100% + 6px);background:#fff;border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.18);z-index:110;min-width:220px;overflow:hidden;border:1px solid #E2E8F0"><button onclick="ghRefresh(false)" style="display:block;width:100%;text-align:left;padding:11px 16px;border:none;background:none;cursor:pointer;font-size:13px;color:#1E293B;font-weight:500">&#8635; Normaal vernieuwen</button><button onclick="ghRefresh(true)" style="display:block;width:100%;text-align:left;padding:11px 16px;border:none;background:none;cursor:pointer;font-size:13px;color:#DC2626;font-weight:500">&#9889; Alles ophalen (cache negeren)</button><div style="border-top:1px solid #F1F5F9"></div><button onclick="ghShowTokenModal()" style="display:block;width:100%;text-align:left;padding:11px 16px;border:none;background:none;cursor:pointer;font-size:13px;color:#64748B">&#128273; Token beheren</button></div></span><button class="btn btn-pdf" onclick="window.print()">&#128438; PDF</button></div>'}
    {"" if static else """<div class="topbar">
      <button class="hamburger" onclick="toggleNav()" aria-label="Menu"><span></span><span></span><span></span></button>
      <button class="btn btn-pdf" onclick="window.print()">&#128438; PDF</button>
      <button class="btn btn-add" id="abtn" onclick="toggleAdd()">&#43; Toevoegen</button>
      <button class="btn btn-refresh" id="rbtn" onclick="doRefresh()">&#8635; Vernieuwen</button>
      <button class="btn btn-push" id="pbtn" onclick="doPush()">&#8679; Push GitHub</button>
    </div>
    <div class="add-panel" id="add-panel">
      <input type="text" id="add-url" class="add-input" placeholder="Plak een Discogs release-URL..." />
      <button class="btn-go" onclick="doAdd()">Toevoegen</button>
      <button class="btn-x" onclick="toggleAdd()" title="Sluiten">&#215;</button>
    </div>"""}
  </div>
  {home_page}
  {new_listings_page}
<script>
function doRefresh(){{var b=document.getElementById('rbtn');if(b){{b.disabled=true;b.textContent='Bezig...';}}window.location.href='/refresh';}}
function doPush(){{var b=document.getElementById('pbtn');if(b){{b.disabled=true;b.textContent='Pushen...';}}fetch('/push').then(function(){{var t=0;var iv=setInterval(function(){{fetch('/status').then(function(r){{return r.json();}}).then(function(d){{t++;if(!d.pushing||t>30){{clearInterval(iv);if(b){{b.disabled=false;b.textContent='✓ Gepushed!';setTimeout(function(){{b.textContent='↹ Push GitHub';}},3000);}}}}}});}},1000);}});}}
function toggleAdd(){{var p=document.getElementById('add-panel');if(!p)return;var open=p.style.display==='flex';p.style.display=open?'none':'flex';if(!open){{var u=document.getElementById('add-url');if(u){{u.focus();u.value='';}}}}}}
function showPage(id){{document.querySelectorAll('.page').forEach(function(p){{p.style.display='none';}});var el=document.getElementById(id);if(el){{el.style.display='block';}}history.replaceState(null,'','#'+id);}}
</script>
  {invest_listings_page}
  {deals_page}
  {favorites_page}
  {collection_page}
  {artist_pages}
</main>
{"" if not static else """
<div id="gh-modal" style="display:none;position:fixed;top:0;right:0;bottom:0;left:0;background:rgba(15,23,42,.6);z-index:200;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(2px)">
  <div style="background:#fff;border-radius:14px;padding:24px 20px;max-width:380px;width:100%;box-shadow:0 12px 40px rgba(0,0,0,.25)">
    <h3 style="margin:0 0 6px;font-size:15px;font-weight:700;color:#1E293B">GitHub token vereist</h3>
    <p style="margin:0 0 12px;font-size:12.5px;color:#64748B;line-height:1.5">Maak een <strong>Classic Personal Access Token</strong> aan met scope <code style="background:#F1F5F9;padding:1px 4px;border-radius:3px">workflow</code> op <a href="https://github.com/settings/tokens/new?scopes=workflow&description=Vinyl+Tracker" target="_blank" style="color:#10B981;font-weight:600">github.com/settings/tokens</a>. Het token wordt enkel in je browser bewaard (localStorage).</p>
    <input id="gh-token-input" type="password" placeholder="ghp_xxxxxxxxxxxx" autocomplete="off" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1.5px solid #CBD5E1;border-radius:8px;font-size:13px;margin-bottom:10px;outline:none;font-family:monospace"/>
    <div style="display:flex;gap:8px;justify-content:flex-end;align-items:center">
      <button onclick="ghClearToken()" title="Bewaard token wissen" style="padding:8px 10px;border:none;background:none;color:#94A3B8;cursor:pointer;font-size:12px">Wissen</button>
      <button onclick="document.getElementById('gh-modal').style.display='none'" style="padding:8px 14px;border:1.5px solid #CBD5E1;background:#fff;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;color:#475569">Annuleren</button>
      <button onclick="ghSaveToken()" style="padding:8px 18px;background:#10B981;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Opslaan &amp; starten</button>
    </div>
    <p id="gh-modal-err" style="margin:8px 0 0;font-size:11.5px;color:#EF4444;display:none"></p>
  </div>
</div>"""}
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
function doPush(){{
  var b=document.getElementById('pbtn');
  b.disabled=true; b.textContent='Pushen...';
  fetch('/push').then(function(){{
    var t=0;
    var iv=setInterval(function(){{
      fetch('/status').then(function(r){{return r.json();}}).then(function(d){{
        t++;
        if(!d.pushing||t>30){{
          clearInterval(iv);
          b.disabled=false; b.textContent='♹ Push GitHub';
          b.textContent='✓ Gepushed!';
          setTimeout(function(){{b.textContent='♹ Push GitHub';}},3000);
        }}
      }});
    }},1000);
  }});
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
var FAV_KEYS=new Set({json.dumps(list(favorites.keys()))});
function updateFavBadge(){{
  var b=document.getElementById('fav-nav-badge');
  if(b){{b.textContent=FAV_KEYS.size;b.style.display=FAV_KEYS.size?'':'none';}}
}}
function toggleFavorite(key,e){{
  e.stopPropagation();
  var btn=e.currentTarget;
  var snapshot=null;
  try{{snapshot=JSON.parse(btn.dataset.snapshot||'null');}}catch(_){{}}
  var adding=!FAV_KEYS.has(key);
  document.querySelectorAll('.fav-btn[data-fav-key="'+key+'"]').forEach(function(b){{
    b.classList.toggle('fav-active',adding);
    b.title=adding?'Verwijder uit favorieten':'Voeg toe aan favorieten';
  }});
  if(adding){{FAV_KEYS.add(key);_addFavRow(key,snapshot);}}
  else{{FAV_KEYS.delete(key);_removeFavRow(key);}}
  updateFavBadge();
  fetch('/toggle-favorite',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{key:key,action:adding?'add':'remove',snapshot:snapshot}})
  }});
}}
function _addFavRow(key,snap){{
  if(!snap)return;
  var tbody=document.getElementById('fav-tbody');if(!tbody)return;
  var empty=document.getElementById('fav-empty');if(empty)empty.style.display='none';
  if(document.getElementById('fav-row-'+key))return;
  var pct=snap.pct!=null?(snap.pct>0?'+':'')+Math.round(snap.pct)+'%':'—';
  var pctCol=snap.pct!=null?(snap.pct<-5?'#10B981':(snap.pct>15?'#EF4444':'inherit')):'inherit';
  var avg=snap.avg?'€ '+snap.avg.toFixed(2).replace('.',','):'—';
  var tot='€ '+snap.total_eur.toFixed(2).replace('.',',');
  var rid=snap.release_id;
  var lhref='https://www.discogs.com/sell/release/'+rid+'?sort=price%2Casc&limit=50';
  var typeBadge=snap.type==='deal'?'<span style="background:#10B981;color:#fff;font-size:10px;font-weight:600;padding:1px 6px;border-radius:10px">Deal</span>':'<span style="background:#3B82F6;color:#fff;font-size:10px;font-weight:600;padding:1px 6px;border-radius:10px">Listing</span>';
  var today=new Date().toISOString().slice(0,10);
  var tr=document.createElement('tr');
  tr.id='fav-row-'+key;
  tr.innerHTML='<td><span class="rb-group">'+snap.group+'</span></td>'+
    '<td class="td-title">'+snap.title+'</td>'+
    '<td><span class="badge">'+snap.eff_cond+'</span></td>'+
    '<td class="td-num"><strong>'+tot+'</strong></td>'+
    '<td class="td-num muted">'+avg+'</td>'+
    '<td class="td-num" style="font-weight:600;color:'+pctCol+'">'+pct+'</td>'+
    '<td class="td-seller">'+snap.seller+'</td>'+
    '<td>'+typeBadge+'</td>'+
    '<td class="muted" style="font-size:11px">'+today+'</td>'+
    '<td><button class="deal-dismiss" onclick="removeFavFromPage(\''+key+'\',event)" title="Verwijder">&#10005;</button></td>'+
    '<td><a class="btn-link" href="'+lhref+'" target="_blank" onclick="event.stopPropagation()">Koop &rarr;</a></td>';
  tbody.insertBefore(tr,tbody.firstChild);
}}
function _removeFavRow(key){{
  var row=document.getElementById('fav-row-'+key);if(row)row.remove();
  var tbody=document.getElementById('fav-tbody');
  var realRows=tbody?Array.from(tbody.rows).filter(function(r){{return r.id!=='fav-empty';}}).length:0;
  var empty=document.getElementById('fav-empty');
  if(empty)empty.style.display=realRows===0?'':'none';
}}
function removeFavFromPage(key,e){{
  if(e)e.stopPropagation();
  FAV_KEYS.delete(key);
  document.querySelectorAll('.fav-btn[data-fav-key="'+key+'"]').forEach(function(btn){{
    btn.classList.remove('fav-active');
    btn.title='Voeg toe aan favorieten';
  }});
  _removeFavRow(key);
  updateFavBadge();
  fetch('/toggle-favorite',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{key:key,action:'remove'}})
  }});
}}
function _markReadServer(keys){{
  // Probeer lokale server; als dat mislukt, gebruik GitHub Actions (statische site)
  fetch('/mark-read',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{keys:keys}})}})
    .then(function(r){{if(!r.ok)throw new Error('no local server');}})
    .catch(function(){{
      var token=localStorage.getItem('gh_pat');
      if(!token)return;
      fetch('https://api.github.com/repos/schaekentuur-bit/vinyl-tracker/actions/workflows/vinyl.yml/dispatches',{{
        method:'POST',
        headers:{{'Authorization':'Bearer '+token,'Accept':'application/vnd.github.v3+json','Content-Type':'application/json'}},
        body:JSON.stringify({{ref:'master',inputs:{{mark_read_keys:JSON.stringify(keys)}}}})
      }});
    }});
}}
function dismissNewListing(key,e){{
  e.stopPropagation();
  _markReadServer([key]);
  var list=JSON.parse(localStorage.getItem('dismissed_nl')||'[]');
  if(!list.includes(key))list.push(key);
  localStorage.setItem('dismissed_nl',JSON.stringify(list));
  var row=document.querySelector('tr[data-nl-key="'+key+'"]');
  if(row)row.style.display='none';
  _updateNlBar();
  _updateNlNavBadge(-1);
}}
function markAllRead(){{
  var rows=document.querySelectorAll('tr[data-nl-key]');
  var keys=[];
  rows.forEach(function(r){{if(r.style.display!=='none')keys.push(r.getAttribute('data-nl-key'));}});
  if(!keys.length)return;
  _markReadServer(keys);
  rows.forEach(function(r){{r.style.display='none';}});
  var list=JSON.parse(localStorage.getItem('dismissed_nl')||'[]');
  keys.forEach(function(k){{if(!list.includes(k))list.push(k);}});
  localStorage.setItem('dismissed_nl',JSON.stringify(list));
  _updateNlBar();
  var badge=document.getElementById('nl-nav-badge');
  if(badge)badge.style.display='none';
}}
function _updateNlNavBadge(delta){{
  var badge=document.getElementById('nl-nav-badge');
  if(!badge)return;
  var n=(parseInt(badge.textContent)||0)+delta;
  if(n<=0)badge.style.display='none';
  else badge.textContent=n;
}}
function showHiddenNl(){{
  localStorage.removeItem('dismissed_nl');
  document.querySelectorAll('tr[data-nl-key]').forEach(function(r){{r.style.display='';}});
  _updateNlBar();
}}
function _updateNlBar(){{
  var list=JSON.parse(localStorage.getItem('dismissed_nl')||'[]');
  var bar=document.getElementById('nl-dismissed-bar');
  var cnt=document.getElementById('nl-dismissed-n');
  if(bar)bar.style.display=list.length?'block':'none';
  if(cnt)cnt.textContent=list.length;
}}
function dismissDeal(key,e){{
  e.stopPropagation();
  var list=JSON.parse(localStorage.getItem('dismissed_deals')||'[]');
  if(!list.includes(key))list.push(key);
  localStorage.setItem('dismissed_deals',JSON.stringify(list));
  var row=document.querySelector('tr[data-deal-key="'+key+'"]');
  if(row)row.style.display='none';
  _updateDismissedBar();
}}
function showHiddenDeals(){{
  localStorage.removeItem('dismissed_deals');
  document.querySelectorAll('tr[data-deal-key]').forEach(function(r){{r.style.display='';}});
  _updateDismissedBar();
}}
function _updateDismissedBar(){{
  var list=JSON.parse(localStorage.getItem('dismissed_deals')||'[]');
  var bar=document.getElementById('dismissed-bar');
  var cnt=document.getElementById('dismissed-n');
  if(bar)bar.style.display=list.length?'block':'none';
  if(cnt)cnt.textContent=list.length;
}}
var _ghTriggerTime=0,_ghForce=false;
function ghRefresh(force){{
  var m=document.getElementById('gh-menu');
  if(m)m.style.display='none';
  _ghForce=!!force;
  var btn=document.getElementById('gh-refresh-btn');
  if(!btn)return;
  var token=localStorage.getItem('gh_pat');
  if(!token){{
    var md=document.getElementById('gh-modal');
    if(md){{md.style.display='flex';setTimeout(function(){{document.getElementById('gh-token-input').focus();}},50);}}
    return;
  }}
  ghTrigger(token,_ghForce);
}}
function ghToggleMenu(){{
  var m=document.getElementById('gh-menu');
  if(m)m.style.display=m.style.display==='none'?'block':'none';
}}
function ghShowTokenModal(){{
  var m=document.getElementById('gh-menu');
  if(m)m.style.display='none';
  var md=document.getElementById('gh-modal');
  if(md){{md.style.display='flex';setTimeout(function(){{document.getElementById('gh-token-input').focus();}},50);}}
}}
function ghSaveToken(){{
  var t=document.getElementById('gh-token-input').value.trim();
  var err=document.getElementById('gh-modal-err');
  if(!t){{err.style.display='block';err.textContent='Voer een token in.';return;}}
  localStorage.setItem('gh_pat',t);
  document.getElementById('gh-modal').style.display='none';
  document.getElementById('gh-token-input').value='';
  err.style.display='none';
  ghTrigger(t,_ghForce);
}}
function ghClearToken(){{
  localStorage.removeItem('gh_pat');
  document.getElementById('gh-token-input').value='';
}}
document.addEventListener('DOMContentLoaded',function(){{
  var inp=document.getElementById('gh-token-input');
  if(inp)inp.addEventListener('keydown',function(e){{if(e.key==='Enter')ghSaveToken();}});
  document.addEventListener('click',function(e){{
    var m=document.getElementById('gh-menu');
    if(m&&m.style.display!=='none'&&!m.contains(e.target)&&!e.target.closest('[onclick="ghToggleMenu()"]')){{m.style.display='none';}}
  }});
  // Herstel verborgen deals vanuit localStorage
  var dismissed=JSON.parse(localStorage.getItem('dismissed_deals')||'[]');
  dismissed.forEach(function(key){{
    var row=document.querySelector('tr[data-deal-key="'+key+'"]');
    if(row)row.style.display='none';
  }});
  _updateDismissedBar();
  // Herstel verborgen nieuwe listings
  var dismissedNl=JSON.parse(localStorage.getItem('dismissed_nl')||'[]');
  dismissedNl.forEach(function(key){{
    var row=document.querySelector('tr[data-nl-key="'+key+'"]');
    if(row)row.style.display='none';
  }});
  _updateNlBar();
}});
function ghTrigger(token,force){{
  var btn=document.getElementById('gh-refresh-btn');
  btn.disabled=true; btn.textContent=force?'⏳ Starten (volledig)…':'⏳ Starten…';
  _ghTriggerTime=Date.now();
  fetch('https://api.github.com/repos/schaekentuur-bit/vinyl-tracker/actions/workflows/vinyl.yml/dispatches',{{
    method:'POST',
    headers:{{'Authorization':'Bearer '+token,'Accept':'application/vnd.github.v3+json','Content-Type':'application/json'}},
    body:JSON.stringify({{ref:'master',inputs:{{force_refresh:force?'true':'false',mark_read_keys:''}}}})
  }}).then(function(r){{
    if(r.status===204){{
      btn.textContent='⏳ Bezig…';
      setTimeout(function(){{ghPoll(token,0);}},10000);
    }}else if(r.status===401||r.status===403){{
      localStorage.removeItem('gh_pat');
      btn.disabled=false; btn.textContent='⚠ Token ongeldig';
      setTimeout(function(){{btn.textContent='↻ Vernieuwen';btn.disabled=false;}},3000);
    }}else{{
      btn.disabled=false; btn.textContent='⚠ Fout '+r.status;
      setTimeout(function(){{btn.textContent='↻ Vernieuwen';}},3000);
    }}
  }}).catch(function(){{
    btn.disabled=false; btn.textContent='⚠ Netwerkfout';
    setTimeout(function(){{btn.textContent='↻ Vernieuwen';}},3000);
  }});
}}
function ghPoll(token,n){{
  if(n>240){{
    var btn=document.getElementById('gh-refresh-btn');
    btn.disabled=false; btn.textContent='⚠ Timeout — probeer opnieuw';
    return;
  }}
  fetch('https://api.github.com/repos/schaekentuur-bit/vinyl-tracker/actions/runs?per_page=5',{{
    headers:{{'Authorization':'Bearer '+token,'Accept':'application/vnd.github.v3+json'}}
  }}).then(function(r){{return r.json();}}).then(function(d){{
    var runs=d.workflow_runs||[];
    var run=null;
    for(var i=0;i<runs.length;i++){{
      if(new Date(runs[i].created_at).getTime()>=_ghTriggerTime-5000){{run=runs[i];break;}}
    }}
    var btn=document.getElementById('gh-refresh-btn');
    if(!run||run.status==='queued'||run.status==='in_progress'){{
      var elapsed=Math.floor((Date.now()-_ghTriggerTime)/1000);
      var mins=Math.floor(elapsed/60);
      var secs=elapsed%60;
      btn.textContent='⏳ Bezig ('+(mins>0?mins+'min ':'')+(mins>0||secs>0?secs+'s':'<1s')+')…';
      var delay=elapsed<120?5000:10000;
      setTimeout(function(){{ghPoll(token,n+1);}},delay);
    }}else if(run.conclusion==='success'){{
      btn.disabled=false; btn.textContent='✓ Klaar — herlaad';
      btn.onclick=function(){{window.location.reload();}};
    }}else{{
      btn.disabled=false; btn.textContent='⚠ Workflow mislukt';
      setTimeout(function(){{btn.textContent='↻ Vernieuwen';btn.onclick=function(){{ghRefresh(false);}};}},4000);
    }}
  }}).catch(function(){{
    setTimeout(function(){{ghPoll(token,n+1);}},5000);
  }});
}}
function filterNav(q){{
  q=(q||'').toLowerCase().trim();
  document.querySelectorAll('.nav-genre').forEach(function(det){{
    var anyVis=false;
    det.querySelectorAll('.nav-item').forEach(function(item){{
      var text=item.textContent.toLowerCase();
      var titles=(item.getAttribute('data-titles')||'').toLowerCase();
      var show=!q||text.includes(q)||titles.includes(q);
      item.style.display=show?'':'none';
      if(show)anyVis=true;
    }});
    det.style.display=(q&&!anyVis)?'none':'';
    if(q&&anyVis)det.open=true;
  }});
}}
function sortTable(tid,col,th){{
  var table=document.getElementById(tid);
  if(!table)return;
  var tbody=table.querySelector('tbody');
  var rows=Array.from(tbody.querySelectorAll('tr[data-group]'));
  var asc=!th.classList.contains('asc');
  table.querySelectorAll('.th-sort').forEach(function(h){{h.classList.remove('asc','desc');}});
  th.classList.add(asc?'asc':'desc');
  var numCols=[3,4,5];
  var dateCols=[6];
  rows.sort(function(a,b){{
    var ca=a.cells[col]?a.cells[col].textContent.trim():'';
    var cb=b.cells[col]?b.cells[col].textContent.trim():'';
    if(numCols.indexOf(col)>=0){{
      var na=parseFloat(ca.replace(/[^0-9.+-]/g,''))||(asc?Infinity:-Infinity);
      var nb=parseFloat(cb.replace(/[^0-9.+-]/g,''))||(asc?Infinity:-Infinity);
      return asc?na-nb:nb-na;
    }}
    if(dateCols.indexOf(col)>=0){{
      var da=ca==='—'?'':ca.split('/').reverse().join('');
      var db=cb==='—'?'':cb.split('/').reverse().join('');
      return asc?da.localeCompare(db):db.localeCompare(da);
    }}
    return asc?ca.localeCompare(cb,'nl'):cb.localeCompare(ca,'nl');
  }});
  rows.forEach(function(r){{tbody.appendChild(r);}});
}}
function applyFilters(tid){{
  var table=document.getElementById(tid);
  if(!table)return;
  var qEl=document.getElementById('fi-'+tid+'-q');
  var cEl=document.getElementById('fi-'+tid+'-cond');
  var q=qEl?qEl.value.toLowerCase():'';
  var cond=cEl?cEl.value:'';
  var rows=table.querySelectorAll('tbody tr[data-group]');
  var vis=0;
  rows.forEach(function(r){{
    var group=(r.getAttribute('data-group')||'').toLowerCase();
    var title=(r.getAttribute('data-title')||'').toLowerCase();
    var rcond=r.getAttribute('data-cond')||'';
    var show=(!q||(group.includes(q)||title.includes(q)))&&(!cond||rcond===cond);
    r.style.display=show?'':'none';
    if(show)vis++;
  }});
  var cnt=document.getElementById('fi-'+tid+'-cnt');
  if(cnt)cnt.textContent=vis+' listings';
}}
function clearFilters(tid){{
  var q=document.getElementById('fi-'+tid+'-q');
  var c=document.getElementById('fi-'+tid+'-cond');
  if(q)q.value='';
  if(c)c.value='';
  applyFilters(tid);
}}
function sortCollTable(col,th){{
  var tbody=document.querySelector('#tbl-coll tbody');
  if(!tbody)return;
  var rows=Array.from(tbody.querySelectorAll('tr'));
  var asc=!th.classList.contains('asc');
  document.querySelectorAll('#tbl-coll .th-sort').forEach(function(h){{h.classList.remove('asc','desc');}});
  th.classList.add(asc?'asc':'desc');
  var numCols=[3,4,5,6,7];
  rows.sort(function(a,b){{
    var ca=a.cells[col]?a.cells[col].textContent.trim():'';
    var cb=b.cells[col]?b.cells[col].textContent.trim():'';
    if(ca==='—'&&cb!=='—')return asc?1:-1;
    if(cb==='—'&&ca!=='—')return asc?-1:1;
    if(ca==='—'&&cb==='—')return 0;
    if(numCols.indexOf(col)>=0){{
      var na=parseFloat(ca.replace(/[^0-9.+-]/g,''));
      var nb=parseFloat(cb.replace(/[^0-9.+-]/g,''));
      if(isNaN(na))na=asc?Infinity:-Infinity;
      if(isNaN(nb))nb=asc?Infinity:-Infinity;
      return asc?na-nb:nb-na;
    }}
    return asc?ca.localeCompare(cb,'nl'):cb.localeCompare(ca,'nl');
  }});
  rows.forEach(function(r){{tbody.appendChild(r);}});
}}
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
    """Scrapes alle releases parallel en geeft results terug. Respecteert cache tenzij force=True."""
    sales_cache    = load_cache(SALES_CACHE)
    stats_cache    = load_cache(STATS_CACHE)
    listings_cache = load_cache(LISTINGS_CACHE)
    if force_stats:
        stats_cache = {}
    today = datetime.now().isoformat(timespec="seconds")

    # Thread-local curl_cffi sessies (Session is niet thread-safe om te delen)
    _local    = threading.local()
    # Max 4 gelijktijdige HTML-scrapes — voorkomt Cloudflare rate-limiting
    _html_sem = threading.Semaphore(4)
    # API-calls serialiseren met minimale tussentijd
    _api_lock  = threading.Lock()
    _api_last  = [0.0]
    # Cache-dict schrijven vanuit meerdere threads
    _sales_lock = threading.Lock()
    _stats_lock = threading.Lock()
    _lst_lock   = threading.Lock()
    # In GitHub Actions: Discogs website geblokkeerd (CF) en marketplace/search API verwijderd
    _is_ga = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    # Zodra de listings API 404 geeft, overslaan we hem voor alle volgende releases
    _listing_api_dead = [False]

    def _session():
        if not hasattr(_local, "sess"):
            s = cf_requests.Session(impersonate="chrome124")
            s.headers.update({"Accept-Language": "nl-BE,nl;q=0.9"})
            _local.sess = s
        return _local.sess

    def _throttled_api(release_id):
        with _api_lock:
            gap = time.time() - _api_last[0]
            if gap < 1.0:
                time.sleep(1.0 - gap)
            result = get_market_stats(release_id)
            _api_last[0] = time.time()
        return result

    def _process(release_id, group, title):
        print(f">> {group} - {title}")

        # Verkoophistorie (gecached, SALES_CACHE_DAYS TTL)
        sc_entry = sales_cache.get(release_id, {})
        if _is_ga or cache_is_fresh(sc_entry, max_days=SALES_CACHE_DAYS):
            sales = sc_entry.get("sales", [])
            print(f"  Geschiedenis cache: {len(sales)} verkopen")
        else:
            with _html_sem:
                sales = scrape_history(release_id, cookies, _session())
            time.sleep(1.2)
            print(f"  Geschiedenis gescraped: {len(sales)} verkopen")
            if sales:
                with _sales_lock:
                    sales_cache[release_id] = {"fetched_at": today, "sales": sales}
                    save_cache(SALES_CACHE, sales_cache)
            else:
                print(f"  Lege scrape — bestaande cache bewaard")

        # Marktstatistieken API (gecached, 7 dagen TTL — zelfde als verkoop- en listingscache)
        sc_stats = stats_cache.get(release_id, {})
        if cache_is_fresh(sc_stats):
            stats = sc_stats.get("stats")
        else:
            stats = _throttled_api(release_id)
            if stats:
                with _stats_lock:
                    stats_cache[release_id] = {"fetched_at": today, "stats": stats}
                    save_cache(STATS_CACHE, stats_cache)

        # Marketplace listings
        lc_entry = listings_cache.get(release_id, {})
        use_cache = (not force_listings) and cache_is_fresh(lc_entry, max_hours=LISTINGS_CACHE_HOURS) and lc_entry.get("listings")
        if use_cache:
            raw_listings = lc_entry["listings"]
            print(f"  Listings cache: {len(raw_listings)} listings")
        elif _is_ga:
            # GitHub Actions: Discogs-website geblokkeerd door Cloudflare én
            # marketplace/search API verwijderd door Discogs — gebruik cache.
            # Ververs listings lokaal via `python vinyl_tracker.py`.
            raw_listings = lc_entry.get("listings", [])
            print(f"  Listings uit cache (GA: HTML+API niet beschikbaar): {len(raw_listings)}")
        else:
            # Lokaal: HTML scraping proberen
            with _html_sem:
                raw_listings = scrape_listings(release_id, cookies, _session())
            time.sleep(1.2)
            if raw_listings:
                with _lst_lock:
                    listings_cache[release_id] = {"fetched_at": today, "listings": raw_listings}
                    save_cache(LISTINGS_CACHE, listings_cache)
                print(f"  Listings gescraped: {len(raw_listings)} listings")
            elif not _listing_api_dead[0]:
                # API-fallback (één poging; als 404 → markeer als dood voor alle releases)
                with _api_lock:
                    gap = time.time() - _api_last[0]
                    if gap < 1.0:
                        time.sleep(1.0 - gap)
                    api_result = scrape_listings_api(release_id)
                    _api_last[0] = time.time()
                if api_result:
                    raw_listings = api_result
                    with _lst_lock:
                        listings_cache[release_id] = {"fetched_at": today, "listings": raw_listings}
                        save_cache(LISTINGS_CACHE, listings_cache)
                    print(f"  Listings via API: {len(raw_listings)} listings")
                else:
                    _listing_api_dead[0] = True
                    raw_listings = lc_entry.get("listings", [])
                    print(f"  Listings leeg (HTML + API), cache bewaard: {len(raw_listings)} listings")
            else:
                raw_listings = lc_entry.get("listings", [])
                print(f"  Listings cache (API onbeschikbaar): {len(raw_listings)} listings")

        return {
            "id":       release_id,
            "group":    group,
            "title":    title,
            "sales":    sales,
            "stats":    stats or {},
            "listings": raw_listings,
        }

    results = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(_process, rid, grp, ttl): rid
            for rid, (grp, ttl) in RELEASES.items()
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                rid = futures[future]
                print(f"  Fout bij release {rid}: {e}")

    # Thumbnails ophalen (eenmalig, permanent gecached — covers veranderen niet)
    thumb_cache = load_cache(THUMB_CACHE)
    updated = False
    for res in results:
        rid = res["id"]
        if not thumb_cache.get(rid):
            print(f"  Thumbnail: {res['group']} — {_album_name(res['title'])}")
            thumb = get_release_thumb(rid)
            thumb_cache[rid] = thumb
            updated = True
            time.sleep(0.4)
    if updated:
        save_cache(THUMB_CACHE, thumb_cache)

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


def run_server(initial_results, cookies, session, collection=None):
    state = {"results": initial_results, "refreshing": False,
             "new_listings": compute_new_listings(initial_results),
             "collection": collection or []}

    def _push_to_github():
        """Genereer docs/index.html lokaal en push alles naar GitHub Pages."""
        import subprocess
        repo = os.path.dirname(os.path.abspath(__file__))

        # 1. Genereer docs/index.html vanuit huidige data
        try:
            results = state.get("results") or build_from_cache()
            html = build_html(results, static=True,
                              new_listings=state.get("new_listings", []),
                              collection=state.get("collection", []))
            docs_dir = os.path.join(repo, "docs")
            os.makedirs(docs_dir, exist_ok=True)
            with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as fh:
                fh.write(html)
            _log("docs/index.html aangemaakt")
        except Exception as e:
            _log(f"HTML genereren mislukt: {e}")
            return

        # 2. Stage alles wat relevant is
        files = [
            "vinyl_tracker.py",
            "generate_report.py",
            os.path.join("docs", "index.html"),
            SALES_CACHE, STATS_CACHE, LISTINGS_CACHE,
            DEALS_SEEN_FILE, LISTINGS_SEEN_FILE, USER_RELEASES_FILE, THUMB_CACHE, MY_COLLECTION_FILE,
        ]
        existing = [f for f in files if os.path.exists(os.path.join(repo, f))]
        try:
            subprocess.run(["git", "-C", repo, "add"] + existing, check=True,
                           capture_output=True)
            r = subprocess.run(["git", "-C", repo, "diff", "--staged", "--quiet"],
                               capture_output=True)
            if r.returncode == 0:
                _log("GitHub: geen wijzigingen om te pushen")
                return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "-C", repo, "commit", "-m",
                            f"Lokale refresh {ts}"], check=True, capture_output=True)
            # Rebase op remote (GitHub Actions kan ondertussen commits gemaakt hebben).
            # -X ours: bij conflict wint onze lokale versie (verse listings/HTML).
            subprocess.run(["git", "-C", repo, "pull", "--rebase", "-X", "ours",
                            "origin", "master"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "push", "origin", "HEAD"],
                           check=True, capture_output=True)
            _log("Gepushed naar GitHub — live site is nu bijgewerkt")
        except subprocess.CalledProcessError as e:
            _log(f"GitHub push mislukt: {e.stderr.decode(errors='replace').strip()}")

    def do_refresh():
        try:
            _log("Vernieuwen gestart")
            results = scrape_all(cookies, session, force_listings=True, force_stats=False)
            state["results"] = results
            state["new_listings"] = compute_new_listings(results, state.get("new_listings", []))
            _log("Scrapen klaar, email berekenen")

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
            _log("Vernieuwen klaar")
            _push_to_github()
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
                html = build_html(state["results"], new_listings=state.get("new_listings", []),
                                   collection=state.get("collection", []))
                self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))
            elif self.path == "/refresh":
                if not state["refreshing"]:
                    state["refreshing"] = True
                    threading.Thread(target=do_refresh, daemon=True).start()
                self._redirect("/refreshing")
            elif self.path == "/refreshing":
                self._respond(200, "text/html; charset=utf-8",
                              LOADING_HTML.encode("utf-8"))
            elif self.path == "/push":
                def _do_push_bg():
                    _push_to_github()
                    state["pushing"] = False
                if not state.get("pushing"):
                    state["pushing"] = True
                    threading.Thread(target=_do_push_bg, daemon=True).start()
                body = json.dumps({"ok": True}).encode()
                self._respond(200, "application/json", body)
            elif self.path == "/status":
                body = json.dumps({"refreshing": state["refreshing"], "pushing": state.get("pushing", False)}).encode()
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

        def do_POST(self):
            if self.path == "/mark-read":
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                try:
                    keys = json.loads(body).get("keys", [])
                except Exception:
                    self.send_response(400); self.end_headers(); return
                if keys:
                    today = datetime.now().strftime("%Y-%m-%d")
                    seen  = load_cache(LISTINGS_SEEN_FILE)
                    for k in keys:
                        seen[k] = today
                    save_cache(LISTINGS_SEEN_FILE, seen)
                    state["new_listings"] = [
                        nl for nl in state.get("new_listings", [])
                        if nl["key"] not in keys
                    ]
                resp = json.dumps({"ok": True}).encode()
                self._respond(200, "application/json", resp)
            elif self.path == "/toggle-favorite":
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                try:
                    data     = json.loads(body)
                    key      = data.get("key", "")
                    action   = data.get("action", "")
                    snapshot = data.get("snapshot")
                except Exception:
                    self.send_response(400); self.end_headers(); return
                if key and action:
                    favs = load_cache(FAVORITES_FILE)
                    if action == "add" and snapshot:
                        favs[key] = snapshot
                    elif action == "remove":
                        favs.pop(key, None)
                    save_cache(FAVORITES_FILE, favs)
                resp = json.dumps({"ok": True, "count": len(load_cache(FAVORITES_FILE))}).encode()
                self._respond(200, "application/json", resp)
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
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Server gestopt.")
        server.server_close()

# ─── HOOFDLOGICA ──────────────────────────────────────────────────────────────

def build_from_cache():
    """Bouw results direct uit cache zonder netwerk — voor snelle startup."""
    sales_cache    = load_cache(SALES_CACHE)
    stats_cache    = load_cache(STATS_CACHE)
    listings_cache = load_cache(LISTINGS_CACHE)
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    for release_id, (group, title) in RELEASES.items():
        sales        = sales_cache.get(release_id, {}).get("sales", [])
        stats        = stats_cache.get(release_id, {}).get("stats") or {}
        lc_entry     = listings_cache.get(release_id, {})
        raw_listings = lc_entry.get("listings", [])
        results.append({
            "id":       release_id,
            "group":    group,
            "title":    title,
            "sales":    sales,
            "stats":    stats or {},
            "listings": raw_listings,
        })
    return results


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
    print(f"{len(RELEASES)} releases te verwerken")
    print("Cache laden en server starten...\n")

    # Snelle start: cache inlezen, geen netwerk
    results    = build_from_cache()
    collection = import_collection()
    run_server(results, cookies, session, collection=collection)

if __name__ == "__main__":
    main()
