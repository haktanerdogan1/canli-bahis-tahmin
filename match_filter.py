"""Veri kaynagindan BAGIMSIZ mac filtreleme mantigi (RapidAPI, TheSports, ne olursa).

NEDEN AYRI DOSYA: v4_api_bot.py (RapidAPI) ve thesports_bot.py (TheSports) ayni
"bu mac takip edilmeye deger mi" mantigina ihtiyac duyuyor. Bu mantik hangi API'den
geldigimizden bagimsiz (sadece takim/lig ADINA bakiyor), o yuzden iki kez yazip
zamanla birbirinden sapmasina izin vermek yerine tek yerde tutuluyor.
"""

KNOWN_LEAGUE_NAMES = {
    "champions league", "europa league", "conference league",
    "uefa champions league", "uefa europa league", "uefa conference league",
}

# Kullanici talebi (2026-08-28, acil): sike iddialari okunan ligler - takim
# tanidik olsa bile (KNOWN_TEAMS/arsiv uzerinden gecebilirdi) bu liglerden
# ARTIK HIC sinyal uretilmesin. is_known_match() en basta kontrol eder,
# diger tum kademelerden ONCE reddeder.
BLOCKED_LEAGUE_NAMES = {
    "concacaf central american cup",
    # Avustralya NPL (National Premier Leagues) - eyalet bazli amator/yari-
    # amator yapi, denetimi zayif (bkz. NPL Victoria sike iddiasi, 2026-08-29,
    # 0/5 isabet). Ayni sisteme ait TUM eyaletler tek seferde bloke edildi -
    # tek tek sikayet gelmesini beklemeye gerek yok, kullanici talebi.
    "npl victoria",
    "npl act",
    "npl nsw",
    "npl new south wales",
    "npl queensland",
    "npl south australia",
    "npl western australia",
    "npl wa",
    "npl tasmania",
    "npl northern territory",
    "npl nt",
    # Ayni tema: baska ulkelerin alt-bolgesel/yari-amator yapilar (2026-08-29,
    # olcum: English Northern Premier League 0/3, South Australia State
    # League 1 1/3 - kucuk ornekle ama NPL ile ayni yapisal risk kategorisi).
    "english northern premier league",
    "northern premier league",
    "south australia state league 1",
    "south australia state league",
    # 2026-08-29: ayni ligin FARKLI kaynaklarda FARKLI isimlerle gectigi
    # gorulunce (GoalGPT uygulamasi "Australia National Premier Leagues
    # Capital Football 1" olarak gosteriyordu - bizim "npl act" tam
    # eslesmesi bunu YAKALAMIYORDU) fark edilen ek varyasyonlar:
    "australia national premier leagues capital football 1",
    "australia brisbane capital league 1",
    "australia capital territory u23 league",
}

# Tek tek isim ekleyip yetismek surdurulemez (ayni lig onlarca farkli
# yaziliskla gelebiliyor) - bu alt-dizeler gecen HERHANGI bir lig adi da
# bloke edilir. Hepsi Avustralya'nin bolgesel/yari-amator yapilarina ozgu,
# yanlis-pozitif riski dusuk (buyuk/tanidik bir ligin adinda bu ifadeler
# gecmez).
BLOCKED_LEAGUE_SUBSTRINGS = (
    "capital football",
    "national premier leagues",
    "npl ",
    "state league",
    # 2026-08-29: kadin futbolu tamamen bloke edildi (kullanici talebi).
    # Sebep: takim eslestirmemiz (prematch.resolve_team) SADECE isme bakiyor,
    # lig/cinsiyet baglamini hic bilmiyor - "Real Sociedad" (kadin, Liga F)
    # gibi bir isim, buyuk ihtimalle arsivimizdeki ayni isimli ERKEK takimin
    # profiliyle eslesiyor (arsiv agirlikli olarak erkek futbolu). Bu, kadin
    # maclarina YANLIS istatistiksel baglamla sinyal uretildigi anlamina
    # gelir - duzgun (cinsiyete duyarli) bir arsiv/taban oran sistemi
    # kurulana kadar toptan disarida tutuluyor.
    "women",
    "female",
    "femenino",
    "femminile",
    "feminine",
    "frauen",
    "liga f ",
    "nwsl",
    "wsl",
    "w-league",
    "dames",
)

KNOWN_TEAMS = {
    # Süper Lig
    "galatasaray", "fenerbahçe", "fenerbahce", "beşiktaş", "besiktas", "trabzonspor",
    "başakşehir", "basaksehir", "adana demirspor", "antalyaspor", "kasımpaşa", "kasimpasa",
    "kayserispor", "konyaspor", "sivasspor", "alanyaspor", "gaziantep", "çaykur rizespor", "rizespor",
    "göztepe", "goztepe", "samsunspor", "eyüpspor", "eyupspor", "kocaelispor",
    "gençlerbirliği", "genclerbirligi", "karagümrük", "karagumruk",
    # Premier League
    "manchester united", "manchester city", "liverpool", "chelsea", "arsenal", "tottenham",
    "newcastle", "aston villa", "west ham", "brighton", "everton", "wolverhampton", "wolves",
    "crystal palace", "fulham", "brentford", "nottingham forest", "bournemouth", "burnley",
    "leeds united", "sunderland",
    # La Liga
    "real madrid", "barcelona", "atletico madrid", "atlético madrid", "sevilla", "real sociedad",
    "real betis", "villarreal", "athletic bilbao", "athletic club", "valencia", "girona",
    "celta vigo", "osasuna", "getafe", "mallorca", "rayo vallecano", "alaves", "alavés",
    "las palmas", "espanyol", "levante", "elche", "real oviedo",
    # Serie A
    "juventus", "inter milan", "internazionale", "ac milan", "napoli", "as roma", " roma",
    "lazio", "atalanta", "fiorentina", "bologna", "torino", "udinese", "sassuolo", "genoa",
    "cagliari", "parma", "hellas verona", "lecce", "empoli", "como 1907", "cremonese", "pisa",
    # Bundesliga
    "bayern münchen", "bayern munich", "borussia dortmund", "rb leipzig", "bayer leverkusen",
    "eintracht frankfurt", "wolfsburg", "mönchengladbach", "monchengladbach", "union berlin",
    "freiburg", "hoffenheim", "mainz", "fc augsburg", "vfb stuttgart", "werder bremen",
    "köln", "koln", "heidenheim", "st. pauli", "hamburger sv",
    # Ligue 1
    "paris saint-germain", "psg", "marseille", "monaco", "olympique lyonnais", "lille",
    "nice", "rennes", "lens", "strasbourg", "toulouse", "nantes", "reims", "montpellier",
    "brest", "le havre", "angers", "auxerre", "metz", "paris fc",
    # Portekiz
    "benfica", "porto", "sporting cp", "sporting lisbon", "braga", "vitoria guimaraes",
    # Hollanda
    "ajax", "psv", "psv eindhoven", "feyenoord", "az alkmaar", "twente",
    # Belcika
    "club brugge", "anderlecht", "genk", "union saint-gilloise",
    # Iskocya
    "celtic", "rangers",
    # Suudi Pro Ligi
    "al hilal", "al nassr", "al ittihad", "al ahli", "al shabab",
    # MLS
    "inter miami", "la galaxy", "lafc",
    # Brezilya
    "flamengo", "palmeiras", "sao paulo", "corinthians", "santos", "gremio",
    "internacional", "fluminense", "botafogo", "vasco da gama", "cruzeiro",
    # Arjantin
    "boca juniors", "river plate", "racing club", "independiente",
    # Avustralya (NT - Darwin Premier Ligi)
    "hellenic", "casuarina",
}


def arsivde_var_mi(takim_adi):
    """Takim, İddaa arsivimizden cikarilan profillerde var mi?

    Elle takim adi listesi tutmak yerine VERIYE dayali bir olcut: bir takim
    33 bin maclik bultene girmisse, zaten takip edilmeye deger tanidik bir
    takimdir. Sonuc team_aliases tablosunda onbelleklenir (bkz. prematch.py).
    """
    try:
        import prematch
        return prematch.resolve_team(takim_adi) is not None
    except Exception:
        return False


def is_known_match(league_name, home_name, away_name):
    """Mac takip edilmeye deger mi?

    GERI ALINDI (kullanici talebi, 2026-08-21): "her maci cekme" - kisaca
    filtre TAMAMEN ACIK denendi (bkz. git log, 2026-08-13), ama disiplinli
    dort kademeli filtre zaten iyi calisiyordu (arsiv tabanli tier'lar
    araciligiyla kucuk/az bilinen ligler de geciyor, bkz. bugunku sinyaller:
    Tepatitlan de Morelos, Botafogo RJ vb.) - genis acmaya gerek olmadigi
    goruldu, eski mantiga donuldu.

    Dort kademe:
      1. Lig adi tanidik bir turnuva mi (Sampiyonlar/Avrupa/Konferans Ligi)
      2. Lig adi DENEY kapsamindaki bir ulkeye mi ait (Romanya, 2026-08-12)
      3. Takim adi elle tutulan buyuk kulup listesinde mi
      4. Takim Iddaa arsivinde var mi (veriye dayali, en genis kapsam)
    """
    ln = (league_name or "").strip().lower()
    if ln in BLOCKED_LEAGUE_NAMES:
        return False
    if any(sub in ln for sub in BLOCKED_LEAGUE_SUBSTRINGS):
        return False
    if ln in KNOWN_LEAGUE_NAMES:
        return True
    if "romania" in ln:
        return True

    hn = (home_name or "").strip().lower()
    an = (away_name or "").strip().lower()
    if any(kw in hn or kw in an for kw in KNOWN_TEAMS):
        return True

    return arsivde_var_mi(home_name) or arsivde_var_mi(away_name)
