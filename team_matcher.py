"""
Takim ismi eslestirme: arsiv (Turkce adlar) <-> canli API (yerel/Ingilizce adlar).

SORUN:
  Arsiv "bayern münih", "paok selanik", "dinamo kiev" gibi TURKCE EXONIM kullaniyor.
  Canli API ise "Bayern München", "PAOK Thessaloniki", "Dynamo Kyiv" donuyor.
  Duz metin karsilastirmasi bunlarin yalnizca yarisini yakaliyor.

TEHLIKE:
  Gevsek bulanik eslestirme YANLIS eslesme uretiyor - olculdu:
    'Valur' (Izlanda)   -> 'valour'      (Kanada kulubu)   YANLIS
    'Györi ETO'         -> 'gyori eto ii' (rezerv takim)   YANLIS
  Yanlis profil, profilsizlikten DAHA KOTUDUR: bot yanlis veriyle guvenle sinyal uretir.
  Bu yuzden asagida hem exonim sozlugu hem de rezerv/altyapi koruma kurali var.
"""
import difflib
import re
import unicodedata

# Turkce exonimler - bulanik eslestirmenin yakalayamadigi, elle dogrulanmis karsiliklar
EXONYMS = {
    "dynamo kyiv": "dinamo kiev",
    "paok thessaloniki": "paok selanik",
    "partizan beograd": "partizan belgrad",
    "bayern munchen": "bayern munih",
    "dinamo tbilisi": "dinamo tiflis",
    "cska sofia": "cska sofya",
    "qarabag": "karabag",
    "crvena zvezda": "kizilyildiz",
    "red star belgrade": "kizilyildiz",
    "sporting cp": "sporting lizbon",
    "benfica": "benfica",
    "olympiacos": "olympiakos",
    "steaua bucuresti": "steaua bukres",
    "fcsb": "steaua bukres",
    "slovan bratislava": "slovan bratislava",
    "shakhtar donetsk": "shakhtar donetsk",
    "kobenhavn": "kopenhag",
    "copenhagen": "kopenhag",
    "malmo ff": "malmo",
    "young boys": "young boys",
    "salzburg": "salzburg",
    "genk": "genk",
    "gent": "gent",
    "anderlecht": "anderlecht",
}

# Rezerv / altyapi / kadin takim isaretleri.
# Bir tarafta varsa digerinde de OLMALI - yoksa eslestirme REDDEDILIR.
SQUAD_MARKERS = re.compile(
    r"\b(ii|iii|b|u1[5-9]|u2[0-3]|reserves?|reserve|rezerv|akademi|academy|ak|"
    r"youth|genclik|altyapi|jong|w|women|kadin|femenino|feminin)\b"
)

_NOISE = re.compile(
    r"\b(fc|sc|sk|cf|ac|as|fk|if|bk|afc|cd|ca|nk|sv|vfb|fsv|tsv|ks|mfk|"
    r"club|kulubu|spor|sportif|calcio|ud|rc|sd|cs|ss|ssc|aek|apoel)\b"
)


def normalize(name: str) -> str:
    s = (name or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    for a, b in [("ı","i"),("ş","s"),("ğ","g"),("ç","c"),("ö","o"),("ü","u"),
                 ("ø","o"),("æ","ae"),("å","a"),("ß","ss"),("đ","d")]:
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def squad_kind(name: str) -> str:
    """Takimin 'turu': A takimi mi, rezerv/altyapi/kadin mi."""
    n = normalize(name)
    m = SQUAD_MARKERS.search(n)
    return m.group(1) if m else "main"


def _core(name: str) -> str:
    """Kulup ekleri ve gurultu temizlenmis cekirdek ad."""
    n = normalize(name)
    n = _NOISE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def _compatible(a: str, b: str) -> bool:
    """Rezerv/kadin takim ile A takimi eslestirilemez."""
    return squad_kind(a) == squad_kind(b)


def build_index(archive_names):
    """Arsiv adlarindan arama indeksi kurar.

    token_df: her token'in KAC FARKLI kulupte gectigi. Sehir adlari ("cluj",
    "tel aviv") birden fazla kulupte geciyor; bu tur BELIRSIZ token'lar uzerinden
    tek basina eslestirme yapmak yanlis sonuc verir (ornek: 'Universitatea Cluj'
    ile 'CFR Cluj' ayni sehirde AYRI kuluplerdir).
    """
    idx = {"exact": {}, "core": {}, "names": [], "token_df": {}}
    for name in archive_names:
        idx["exact"].setdefault(normalize(name), name)
        core = _core(name)
        idx["core"].setdefault(core, name)
        idx["names"].append(name)
    for core in idx["core"]:
        for t in {t for t in core.split() if len(t) >= 4}:
            idx["token_df"][t] = idx["token_df"].get(t, 0) + 1
    return idx


def match(live_name: str, idx, cutoff: float = 0.93):
    """Canli ada karsilik arsiv adini bulur.

    Doner: (arsiv_adi, yontem, skor) veya (None, 'yok', 0.0)
    Kademeli ve giderek daha temkinli: once kesin, sonra sozluk, sonra token, en son bulanik.
    """
    if not live_name:
        return None, "yok", 0.0

    n = normalize(live_name)
    core = _core(live_name)

    # 1) Birebir
    hit = idx["exact"].get(n) or idx["core"].get(core)
    if hit and _compatible(live_name, hit):
        return hit, "kesin", 1.0

    # 2) Exonim sozlugu
    tr = EXONYMS.get(core) or EXONYMS.get(n)
    if tr:
        hit = idx["exact"].get(normalize(tr)) or idx["core"].get(_core(tr))
        if hit and _compatible(live_name, hit):
            return hit, "sozluk", 1.0

    # 3) Token alt kumesi: "Ludogorets Razgrad" <-> "ludogorets"
    #    En az 4 harflik ayirt edici bir token paylasmalari sarti var.
    live_tok = {t for t in core.split() if len(t) >= 4}
    if live_tok:
        best, best_len = None, 0
        for cand_core, cand_name in idx["core"].items():
            cand_tok = {t for t in cand_core.split() if len(t) >= 4}
            if not cand_tok:
                continue
            if not (live_tok <= cand_tok or cand_tok <= live_tok):
                continue
            ortak = live_tok & cand_tok
            # GUVENLIK 1: eslestirme TEK token'a dayaniyorsa, o token arsivde
            # BIRDEN FAZLA kulupte geciyorsa (sehir adi gibi) reddet.
            if len(ortak) == 1:
                tek = next(iter(ortak))
                if idx["token_df"].get(tek, 0) > 1:
                    continue

            # GUVENLIK 2: IKI TARAFIN DA kendine ozgu ayirt edici token'i varsa reddet.
            # Tek tarafli fazlalik sorun degil (kisaltilmis ad):
            #   'Ludogorets Razgrad' vs 'ludogorets'  -> sadece canlida fazla, KABUL
            # Ama iki tarafta da farkli kimlik token'i varsa AYRI kuluplerdir:
            #   'KuPS Akatemia' vs 'sjk akatemia'     -> kups / sjk, RED
            live_all = set(core.split()) - {""}
            cand_all = set(cand_core.split()) - {""}
            if (live_all - cand_all) and (cand_all - live_all):
                continue
            if len(ortak) > best_len and _compatible(live_name, cand_name):
                best, best_len = cand_name, len(ortak)
        if best:
            return best, "token", 0.95

    # 4) Bulanik - YUKSEK esik, ayrica tur uyumu sart
    cands = difflib.get_close_matches(core, list(idx["core"].keys()), n=3, cutoff=cutoff)
    for cnd in cands:
        cand_name = idx["core"][cnd]
        if _compatible(live_name, cand_name):
            score = difflib.SequenceMatcher(None, core, cnd).ratio()
            return cand_name, "bulanik", round(score, 3)

    return None, "yok", 0.0
