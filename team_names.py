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


def ayni_fikstur(home_a, away_a, home_b, away_b, league_a=None, league_b=None):
    """Iki mac ayni gercek fiksturu mu anlatiyor?

    IKI KURAL:
      1. TAM eslesme - normalize edilmis ev VE deplasman ayni. Lig sarti
         aranmaz; "FC Rapperswil-Jona" = "Rapperswil-Jona" gibi.
      2. GEVSEK (alt-kume) - bir kaynagin ekstra kelime eklediği durumlar:
         "Sekhukhune United" / "Sekhukhune", "Radnik" / "Radnik Surdulica".
         Bu kural YANLIS POZITIF riski tasir (farkli ulkelerdeki "Racing",
         "Union", "Independiente"), o yuzden SADECE lig adi da esitse
         uygulanir - api.py:_dedup_gevsek_eslesme ile ayni guvence.
         Lig bilgisi verilmemisse gevsek kural HIC calismaz.

    Bos/eksik isimde False - bilmiyorsak birlestirmeyiz."""
    a, b = fixture_key(home_a, away_a), fixture_key(home_b, away_b)
    if not a[0] or not a[1] or not b[0] or not b[1]:
        return False
    if a == b:
        return True

    if league_a is None or league_b is None:
        return False
    if normalize_team_name(league_a) != normalize_team_name(league_b):
        return False
    if not normalize_team_name(league_a):
        return False  # iki tarafta da lig adi bos - guvence yok, birlestirme
    for x, y in ((home_a, home_b), (away_a, away_b)):
        sx, sy = normalize_loose(x), normalize_loose(y)
        if not sx or not sy:
            return False
        if not (sx <= sy or sy <= sx):
            return False
    return True
