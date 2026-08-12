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

    Dort kademe:
      1. Lig adi tanidik bir turnuva mi (Sampiyonlar/Avrupa/Konferans Ligi)
      2. Lig adi DENEY kapsamindaki bir ulkeye mi ait (bkz. asagida)
      3. Takim adi elle tutulan buyuk kulup listesinde mi
      4. Takim Iddaa arsivinde var mi (veriye dayali, en genis kapsam)
    """
    ln = (league_name or "").strip().lower()
    if ln in KNOWN_LEAGUE_NAMES:
        return True

    # DENEY (kullanici talebi, 2026-08-12): "Romanya maclarini da cekelim,
    # sisteme analiz ettirelim, deneyelim bakalim" - Romanya Kupasi gibi
    # bolgesel/amator maclar bilerek genisletildi. UYARI: bu kulupler
    # buyuk ihtimalle prematch.py'nin Iddaa-arsiv-tabanli takim profillerinde
    # HIC YOK - botlarin cogu "insufficient_data" donebilir, bu BEKLENEN bir
    # sonuc (veri gercekten yoksa uydurulmuyor), hata degil.
    if "romania" in ln:
        return True

    hn = (home_name or "").strip().lower()
    an = (away_name or "").strip().lower()
    if any(kw in hn or kw in an for kw in KNOWN_TEAMS):
        return True

    return arsivde_var_mi(home_name) or arsivde_var_mi(away_name)
