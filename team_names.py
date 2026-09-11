"""Takim adi normalizasyonu - kaynaklar arasi fikstur eslestirmesi icin.

NEDEN AYRI MODUL (2026-09-11): ayni mantik api.py'de vardi ve SADECE
/api/admin/live-sync yolunda calisiyordu. v4_api_bot ise `matches`
tablosuna DOGRUDAN yaziyor (bkz. v4_api_bot.py, "INSERT INTO matches"),
yani onun satirlari fs_/ss_ satirlariyla HIC karsilastirilmiyordu.
Sonuc: ayni gercek mac iki ayri match_id ile kaydedildi ve orchestrator'in
tekrar kontrolu (match_id bazliydi) ikisini de gecirdi - ayni mac
Telegram'a IKI KEZ paylasildi (kullanici raporu 2026-09-11:
"Rapperswil-Jona" + "FC Rapperswil-Jona", "Stellenbosch FC" +
"Stellenbosch", msg 1076/1077 ve 1075/1078).

Bu modul kaynagi ne olursa olsun her yerden ayni anahtari uretsin diye
ortak yere alindi. api.py'deki surumle AYNI kural seti.
"""
import re
import unicodedata

# fc/sc/nk gibi kulup ekleri kaynaktan kaynaga degisiyor ("FC Rapperswil-Jona"
# vs "Rapperswil-Jona"), atiliyor.
_TEAM_SUFFIX_WORDS = re.compile(
    r'\b(fc|cf|sc|ac|cd|afc|sk|fk|nk|hnk|club|sporting club|sports club)\b'
)


def normalize_team_name(name):
    """Aksan/buyuk-kucuk harf/kulup eki farklarini eleyip karsilastirilabilir
    bir anahtar uretir. Bulanik eslesme YAPMAZ - iki farkli gercek takimin
    yanlislikla birlesmemesi icin sadece bu deterministik sadelestirme."""
    if not name:
        return ""
    s = unicodedata.normalize('NFKD', str(name))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _TEAM_SUFFIX_WORDS.sub(' ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    return s


def fixture_key(home, away):
    """Bir macin kaynaktan bagimsiz kimligi. Ev/deplasman SIRASI korunur -
    ters cevirmek gercekten farkli iki maci (A-B ve B-A) birlestirirdi."""
    return (normalize_team_name(home), normalize_team_name(away))


def normalize_loose(name):
    """Kelime KUMESI - saf rakam token'lari (yil) atilir. Alt-kume
    karsilastirmasi icin; tek basina kullanilmaz (bkz. ayni_fikstur)."""
    s = normalize_team_name(name)
    if not s:
        return frozenset()
    return frozenset(tok for tok in s.split() if not tok.isdigit())


# Genel-gecer kulup kelimeleri - alt-kume kuralinin YANLIS POZITIF riski
# tasidigi durum tam olarak bunlar: farkli ulkelerdeki iki farkli kulup
# ayni jenerik kelimeyi tasiyabilir ("Racing" Arjantin'de de Fransa'da da
# var). Lig bilgisi guvenilir DEGILSE (2026-09-11: kaynaklardan biri bos/
# farkli lig adi gonderebiliyor, bkz. asagidaki not), eslesen kelime
# kumesinin en az biri SADECE bu listeden olusmuyorsa (yani ozgun/ayirt
# edici en az bir kelime var - "Maastricht", "Vukovar" gibi) kabul edilir.
_JENERIK_KELIMELER = frozenset({
    "racing", "union", "unione", "city", "real", "national", "nacional",
    "internacional", "atletico", "athletic", "athletico", "united",
    "dynamo", "dinamo", "inter", "sport", "sporting", "olympic",
    "olympique", "rangers", "wanderers", "rovers", "town", "county",
    "central", "north", "south", "east", "west", "boys", "young",
    "youth", "junior", "juniors", "reserve", "reserves", "ii", "b",
    "deportivo", "deportes", "independiente", "estrella", "juventus",
    "milan", "porto", "boca", "river", "america",
})


def ayni_fikstur(home_a, away_a, home_b, away_b, league_a=None, league_b=None):
    """Iki mac ayni gercek fiksturu mu anlatiyor?

    IKI KADEME:
      1. TAM eslesme - normalize edilmis ev VE deplasman ayni. Lig sarti
         aranmaz; "FC Rapperswil-Jona" = "Rapperswil-Jona" gibi.
      2. GEVSEK (alt-kume) - "Sekhukhune United"/"Sekhukhune", "Radnik"/
         "Radnik Surdulica", "Maastricht"/"MVV Maastricht" gibi bir
         kaynagin ekstra/farkli kelime ekledigi durumlar. SADECE eslesen
         kelime kumelerinden EN AZ BIRI jenerik-olmayan (ozgun) bir kelime
         iceriyorsa kabul edilir - "Racing"/"Union" gibi SADECE jenerik
         kelimelerden olusan bir eslesme HER ZAMAN reddedilir.

    LIG ADI ARTIK KOSUL DEGIL (2026-09-11, uretimde bulundu): "lig
    biliniyor ve esitse gevsek kurali uygula, biliniyor ve FARKLIYSA
    reddet" kurali vardi - ama kaynaklar (v4_/fs_/ss_) ayni gercek lig
    icin TAMAMEN FARKLI stringler gonderiyor (biri "Eerste Divisie" digeri
    bombos/tanimsiz, sitede "Unknown League" olarak gorunuyor). Bu, iki
    KESIN ayni-fikstur cifti (Maastricht/MVV Maastricht, Jong Ajax/
    Waalwijk vs RKC Waalwijk) icin "lig farkli" dalina dusup HARD REJECT
    edilmesine ve AYNI MACIN IKI KEZ PAYLASILMASINA yol acti (msg 1086,
    msg 1098 - kullanici raporu). Lig adi kaynaklar arasi guvenilir bir
    sinyal degil; tek gercek guvence artik ozgun-kelime sarti.

    NOT - bilinen kalinti risk: bu artik "Racing" + "Union Berlin" gibi,
    bir tarafta ozgun kelime olan ama GERCEKTEN farkli iki maci da
    (farkli ulke/lig) BIRLESTIREBILIR - cok dusuk olasilikli, gozlemlenen
    "ayni maci iki kez paylasma" sorunundan (sik, kanitlanmis) daha az
    onemli kabul edildi. league_a/league_b parametreleri geriye-uyumluluk
    icin duruyor, karar artik onlara bakmiyor.

    Bos/eksik isimde False - bilmiyorsak birlestirmeyiz."""
    a, b = fixture_key(home_a, away_a), fixture_key(home_b, away_b)
    if not a[0] or not a[1] or not b[0] or not b[1]:
        return False
    if a == b:
        return True

    for x, y in ((home_a, home_b), (away_a, away_b)):
        sx, sy = normalize_loose(x), normalize_loose(y)
        if not sx or not sy:
            return False
        if not (sx <= sy or sy <= sx):
            return False

    ozgun_kelime_var = any(
        (normalize_loose(x) - _JENERIK_KELIMELER) or (normalize_loose(y) - _JENERIK_KELIMELER)
        for x, y in ((home_a, home_b), (away_a, away_b))
    )
    return ozgun_kelime_var
