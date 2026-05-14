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
from concurrent.futures import ThreadPoolExecutor, as_completed
import webbrowser
from datetime import datetime
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
CACHE_DAYS         = 7   # verkoopdata na X dagen opnieuw ophalen
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

EMAIL_FROM = os.getenv("EMAIL_FROM", "").lstrip("﻿").strip()
EMAIL_TO   = os.getenv("EMAIL_TO",   "").lstrip("﻿").strip()
EMAIL_PASS = os.getenv("EMAIL_PASS", "").lstrip("﻿").strip()

# Lokale overrides uit config.py (staat in .gitignore, nooit op GitHub)
try:
    import config as _cfg
    DISCOGS_TOKEN = DISCOGS_TOKEN or getattr(_cfg, "DISCOGS_TOKEN", "")
    EMAIL_FROM    = EMAIL_FROM    or getattr(_cfg, "EMAIL_FROM",    "")
    EMAIL_TO      = EMAIL_TO      or getattr(_cfg, "EMAIL_TO",      "")
    EMAIL_PASS    = EMAIL_PASS    or getattr(_cfg, "EMAIL_PASS",    "")
except ImportError:
    pass

# ─── RELEASE BESCHRIJVINGEN ──────────────────────────────────────────────────
RELEASE_INFO = {
    # Oasis
    "939519":   ("🏆 First pressing", "Originele UK Damont-persing — de meest gezochte Oasis-versie. Bewaren."),
    "517224":   ("🏆 First pressing", "Originele UK Damont-persing van het debuut — zeldzamer dan Morning Glory. Top collector's item."),
    "6127871":  ("🎵 Luisterversie", "Moderne EU reissue — prima geluid voor dagelijks gebruik."),
    "12864584": ("🎵 Luisterversie", "Moderne reissue — ideaal om af te spelen zonder origineel te slijten."),
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
}

# ─── ALBUM PAIRINGS ─────────────────────────────────────────────────────────────
# Elk tupel: (belegging_id, luister_id) — links=belegging, rechts=luisteren
RELEASE_PAIRS = [
    # Rock
    ("939519",   "6127871"),   # Oasis — Morning Glory
    ("517224",   "12864584"),  # Oasis — Definitely Maybe
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
}
GENRE_ORDER = ["Rock", "Hard Rock / Metal", "Pop", "Soul / R&B", "Reggae", "Ska", "Hip-Hop", "Rock & Roll", "Nederlandstalig"]

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
        with _smtp.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASS)
            smtp.send_message(msg)
        print(f"Email verstuurd: {subject}")
    except Exception as e:
        print(f"Email fout: {e}")


def build_html(results, static=False):
    now    = datetime.now().strftime("%d/%m/%Y %H:%M")
    groups = list(dict.fromkeys(r["group"] for r in results))
    thumbs = load_cache(THUMB_CACHE)

    # ── Top Deals berekening (nodig voor home stats) ───────────────────────
    deals = compute_deals(results)

    # ── Per-artiest pagina's ───────────────────────────────────────────────
    artist_pages = ""
    for group in groups:
        gresult = [r for r in results if r["group"] == group]
        cards   = _build_release_cards(gresult, thumbs)
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
        cheapest = min(all_best, key=lambda x: x.get("total_eur", x["price"])) if all_best else None
        listing_cell = _fmt_eur(cheapest.get("total_eur", cheapest["price"])) if cheapest else "—"
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
            return f'<tr><td colspan="9" class="no-data">Geen deals gevonden.</td></tr>'
        rows = ""
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
            deal_key = f"{r['id']}_{d['cond']}"
            rows += (
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
                f'<td><button class="deal-dismiss" onclick="dismissDeal(\'{deal_key}\',event)" title="Verbergen">&#10005;</button></td>'
                f'<td><a class="btn-link" href="{lhref}" target="_blank" onclick="event.stopPropagation()">Koop &rarr;</a></td>'
                f'</tr>'
            )
        return rows

    def _deal_table(tier_deals, ref_label):
        return f"""
        <div class="card" style="margin-bottom:24px">
          <table class="ov-table">
            <thead><tr>
              <th>Artiest</th><th>Release</th><th>Disc / Hoes</th>
              <th class="th-r" title="Totaalprijs incl. verzending">Listing incl. verzend</th><th class="th-r">{ref_label}</th>
              <th class="th-r" title="% goedkoper dan historische prijs (excl. verzending)">Korting vs. hist.</th><th>Verkoper</th><th></th><th></th>
            </tr></thead>
            <tbody>{_deal_rows_html(tier_deals, ref_label)}</tbody>
          </table>
        </div>"""

    beste_deals = [d for d in deals if d["tier"] == "beste"]
    goede_deals = [d for d in deals if d["tier"] == "goed"]

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
  .rb{{background:var(--surface);border:1px solid var(--border);border-radius:10px;
       padding:18px 20px;margin-bottom:12px;
       box-shadow:0 1px 3px rgba(0,0,0,.04)}}
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
  .deal-dismiss{{background:none;border:none;color:#CBD5E1;cursor:pointer;
                 font-size:13px;padding:2px 6px;border-radius:4px;line-height:1}}
  .deal-dismiss:hover{{background:#FEE2E2;color:#EF4444}}
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
    .page{{padding:14px 12px 52px}}
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
  {deals_page}
  {artist_pages}
</main>
{"" if not static else """
<div id="gh-modal" style="display:none;position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:200;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(2px)">
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
}});
function ghTrigger(token,force){{
  var btn=document.getElementById('gh-refresh-btn');
  btn.disabled=true; btn.textContent=force?'⏳ Starten (volledig)…':'⏳ Starten…';
  _ghTriggerTime=Date.now();
  fetch('https://api.github.com/repos/schaekentuur-bit/vinyl-tracker/actions/workflows/vinyl.yml/dispatches',{{
    method:'POST',
    headers:{{'Authorization':'Bearer '+token,'Accept':'application/vnd.github.v3+json','Content-Type':'application/json'}},
    body:JSON.stringify({{ref:'master',inputs:{{force:force?'true':'false'}}}})
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
  if(n>80){{
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
      var mins=Math.floor(n*5/60);
      btn.textContent='⏳ Bezig ('+(mins>0?mins+'min':'<1min')+')…';
      setTimeout(function(){{ghPoll(token,n+1);}},5000);
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
    today = datetime.now().strftime("%Y-%m-%d")

    # Thread-local curl_cffi sessies (Session is niet thread-safe om te delen)
    _local    = threading.local()
    # Max 3 gelijktijdige HTML-scrapes — voorkomt Cloudflare rate-limiting
    _html_sem = threading.Semaphore(3)
    # API-calls serialiseren met minimale tussentijd
    _api_lock  = threading.Lock()
    _api_last  = [0.0]
    # Cache-dict schrijven vanuit meerdere threads
    _sales_lock = threading.Lock()
    _stats_lock = threading.Lock()
    _lst_lock   = threading.Lock()

    def _session():
        if not hasattr(_local, "sess"):
            s = cf_requests.Session(impersonate="chrome124")
            s.headers.update({"Accept-Language": "nl-BE,nl;q=0.9"})
            _local.sess = s
        return _local.sess

    def _throttled_api(release_id):
        with _api_lock:
            gap = time.time() - _api_last[0]
            if gap < 0.5:
                time.sleep(0.5 - gap)
            result = get_market_stats(release_id)
            _api_last[0] = time.time()
        return result

    def _process(release_id, group, title):
        print(f">> {group} - {title}")

        # Verkoophistorie (gecached, 7 dagen TTL)
        sc_entry = sales_cache.get(release_id, {})
        if cache_is_fresh(sc_entry):
            sales = sc_entry["sales"]
            print(f"  Geschiedenis cache: {len(sales)} verkopen")
        else:
            with _html_sem:
                sales = scrape_history(release_id, cookies, _session())
                time.sleep(1.5)
            print(f"  Geschiedenis gescraped: {len(sales)} verkopen")
            with _sales_lock:
                sales_cache[release_id] = {"fetched_at": today, "sales": sales}
                save_cache(SALES_CACHE, sales_cache)

        # Marktstatistieken API (gecached per dag)
        stats_key = f"{release_id}_{today}"
        stats = stats_cache.get(stats_key)
        if not stats:
            stats = _throttled_api(release_id)
            if stats:
                with _stats_lock:
                    stats_cache[stats_key] = stats
                    save_cache(STATS_CACHE, stats_cache)

        # Marketplace listings (gecached 7 dagen; fresh scrape bij force of verlopen cache)
        lc_entry = listings_cache.get(release_id, {})
        use_cache = (not force_listings) and cache_is_fresh(lc_entry, max_days=7) and lc_entry.get("listings")
        if use_cache:
            raw_listings = lc_entry["listings"]
            print(f"  Listings cache: {len(raw_listings)} listings")
        else:
            with _html_sem:
                raw_listings = scrape_listings(release_id, cookies, _session())
                time.sleep(1.5)
            if raw_listings:
                with _lst_lock:
                    listings_cache[release_id] = {"fetched_at": today, "listings": raw_listings}
                    save_cache(LISTINGS_CACHE, listings_cache)
                print(f"  Listings gescraped: {len(raw_listings)} listings")
            else:
                raw_listings = lc_entry.get("listings", [])
                print(f"  Listings scrape leeg (Cloudflare?), cache bewaard: {len(raw_listings)} listings")

        return {
            "id":       release_id,
            "group":    group,
            "title":    title,
            "sales":    sales,
            "stats":    stats or {},
            "listings": raw_listings,
        }

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
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


def run_server(initial_results, cookies, session):
    state = {"results": initial_results, "refreshing": False}

    def _push_to_github():
        """Genereer docs/index.html lokaal en push alles naar GitHub Pages."""
        import subprocess
        repo = os.path.dirname(os.path.abspath(__file__))

        # 1. Genereer docs/index.html vanuit huidige data
        try:
            results = state.get("results") or build_from_cache()
            html = build_html(results, static=True)
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
            DEALS_SEEN_FILE, USER_RELEASES_FILE, THUMB_CACHE,
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
            # Fetch eerst zodat de tracking-ref actueel is; daarna force-with-lease.
            # (GitHub Actions pusht dagelijks — zonder fetch krijg je 'stale info'.)
            subprocess.run(["git", "-C", repo, "fetch", "origin"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "push", "--force-with-lease",
                            "origin", "HEAD"],
                           check=True, capture_output=True)
            _log("Gepushed naar GitHub — live site is nu bijgewerkt")
        except subprocess.CalledProcessError as e:
            _log(f"GitHub push mislukt: {e.stderr.decode(errors='replace').strip()}")

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

def build_from_cache():
    """Bouw results direct uit cache zonder netwerk — voor snelle startup."""
    sales_cache    = load_cache(SALES_CACHE)
    stats_cache    = load_cache(STATS_CACHE)
    listings_cache = load_cache(LISTINGS_CACHE)
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    for release_id, (group, title) in RELEASES.items():
        sales        = sales_cache.get(release_id, {}).get("sales", [])
        stats_key    = f"{release_id}_{today}"
        # Probeer eerst vandaag, daarna meest recente beschikbare stats
        stats = stats_cache.get(stats_key)
        if not stats:
            candidates = {k: v for k, v in stats_cache.items()
                          if k.startswith(release_id + "_")}
            if candidates:
                stats = candidates[max(candidates)]
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
    results = build_from_cache()
    run_server(results, cookies, session)

if __name__ == "__main__":
    main()
