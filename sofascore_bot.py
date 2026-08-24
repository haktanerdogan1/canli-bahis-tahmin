"""SofaScore canli veri kazima yardimcilari - YEREL makinede
(sofascore_client.py icinde) Playwright uzerinden calisir. Bu dosya
DOGRUDAN CALISTIRILMAZ - flashscore_xg_bot.py ile ayni ayrim ilkesi.

NEDEN VAR: Flashscore tek canli veri kaynagimiz - SofaScore ikinci/bagimsiz
bir kaynak olarak eklendi (bkz. api.py: /api/admin/live-sync'in coklu-kaynak
genellemesi, "source" alani). SofaScore'un genel JSON API'si duz HTTP
istekleriyle (curl/requests) 403 donuyor (TLS parmak izi kontrolu) ama
gercek bir tarayici motoruyla (Playwright) once siteyi yukleyip sonra sayfa
icinden fetch() cagirmak calisiyor - DOM kazima YOK, Flashscore'dan farkli
olarak dogrudan temiz JSON aliyoruz.

ONEMLI SINIRLAMA: SofaScore bu tur otomatik istemcileri IP bazli hizla
(gozlemlenen: ~10-15 istek / birkac dakika icinde) engelliyor. Bu yuzden
sofascore_client.py Flashscore'un 15sn dongusu yerine COK DAHA SEYREK
calisir (varsayilan 300sn) ve engellenirse uzun bir backoff'a girer -
bkz. o dosyanin docstring'i.

ISTATISTIK ANAHTAR ESLEMESI: SofaScore'un /event/{id}/statistics ucundaki
'key' alanlari (topluluk kaynakli bilinen degerler, ornegin ballPossession,
expectedGoals) _STAT_KEY_MAP'te bizim alan adlarimiza (api.py:_FS_STAT_FIELDS
ile ayni sozlesme) eslenir. TANIMADIGIMIZ bir key icin deger UYDURULMUYOR -
Flashscore tarafinin "gorulmeyen kategori icin 0 yazma" ilkesiyle ayni."""
import time as _time

LIVE_URL = "https://api.sofascore.com/api/v1/sport/football/events/live"
STATS_URL_TMPL = "https://api.sofascore.com/api/v1/event/{eid}/statistics"
WARMUP_URL = "https://www.sofascore.com/"

# NOT: bu eslemeler SofaScore'un topluluk kaynakli bilinen anahtar adlarina
# dayaniyor - ilk gercek calismada _STAT_KEY_MAP'te olmayan bir key
# gorulurse asagida stderr'e basiliyor (bkz. scrape_stats), o loglara
# bakip gerekirse esleme genisletilmeli.
_STAT_KEY_MAP = {
    "ballPossession": "possession",
    "expectedGoals": "xg",
    "totalShotsOnGoal": "shots",
    "onTargetScoringAttempt": "shots_on_target",
    "shotOffTarget": "shots_off_target",
    "offTargetScoringAttempt": "shots_off_target",
    "cornerKick": "corners",
    "cornerKicks": "corners",
    "redCard": "red_cards",
    "redCards": "red_cards",
    "bigChanceCreated": "big_chances",
    "dangerousAttacks": "dangerous_attacks",
}
_UNKNOWN_KEYS_SEEN = set()


def get_live_summary(page):
    """TEK bir fetch ile TUM canli maclarin skor/dakika/lig ozetini ceker -
    flashscore_xg_bot.get_live_summary ile ayni rol, farkli mekanizma (DOM
    yerine dogrudan JSON)."""
    events = page.evaluate(
        f"() => fetch('{LIVE_URL}').then(r => r.ok ? r.json() : Promise.reject(new Error('http_'+r.status)))"
    )
    out = []
    for e in (events or {}).get("events", []) or []:
        home = e.get("homeTeam") or {}
        away = e.get("awayTeam") or {}
        if not home.get("name") or not away.get("name") or not e.get("id"):
            continue
        status = e.get("status") or {}
        out.append({
            "mid": str(e["id"]), "home": home["name"], "away": away["name"],
            "league": (e.get("tournament") or {}).get("name") or "Unknown League",
            "home_logo": f"https://api.sofascore.com/api/v1/team/{home['id']}/image" if home.get("id") else "",
            "away_logo": f"https://api.sofascore.com/api/v1/team/{away['id']}/image" if away.get("id") else "",
            "score_h": (e.get("homeScore") or {}).get("current") or 0,
            "score_a": (e.get("awayScore") or {}).get("current") or 0,
            "stage": _stage_text(status, e.get("time") or {}),
        })
    return out


def _stage_text(status, time_obj):
    """SofaScore'un status/time nesnelerini api.py:_fs_parse_stage'in
    anladigi "stage" metnine cevirir (rakam=dakika, "Half Time"=HT,
    "Finished"=MS, geri kalani oldugu gibi gecilir - "penalt"/"extra time"
    alt-dizeleri zaten taniniyor, tanimayanlar sunucuda loglanir)."""
    stype = (status.get("type") or "").lower()
    sdesc = (status.get("description") or "").strip()
    sdescl = sdesc.lower()
    if stype == "finished":
        return "Finished"
    if sdescl in ("halftime", "half time"):
        return "Half Time"
    if stype == "inprogress" and sdescl in ("1st half", "2nd half"):
        minute = _compute_minute(time_obj)
        if minute is not None:
            return str(minute)
    return sdesc or stype


def _compute_minute(time_obj):
    """SofaScore dakikayi dogrudan vermiyor - 'bu yarinin basladigi zaman'
    (currentPeriodStartTimestamp) + 'bu yari basladiginda kac dakikaydi'
    (initial, saniye) uzerinden hesaplanir. Flashscore'un aksine (dogrudan
    dakika verir) burasi bir TAHMIN - ama Flashscore ile ayni hassasiyet
    seviyesinde (topluluk kaynakli, dogrulanmis formul)."""
    try:
        start = time_obj.get("currentPeriodStartTimestamp")
        if not start:
            return None
        initial = time_obj.get("initial") or 0
        elapsed = _time.time() - start
        if elapsed < 0:
            return None
        return int(initial // 60 + elapsed // 60)
    except Exception:
        return None


def scrape_stats(page, eid):
    """Bir macin istatistik ucunu ceker, SADECE _STAT_KEY_MAP'te taninan
    anahtarlari dondurur. GORULMEYEN alan icin deger UYDURULMUYOR."""
    url = STATS_URL_TMPL.format(eid=eid)
    data = page.evaluate(
        f"() => fetch('{url}').then(r => r.ok ? r.json() : Promise.reject(new Error('http_'+r.status)))"
    )
    out = {}
    for block in (data or {}).get("statistics", []) or []:
        if block.get("period") != "ALL":
            continue
        for group in block.get("groups", []) or []:
            for item in group.get("statisticsItems", []) or []:
                key = item.get("key")
                field = _STAT_KEY_MAP.get(key)
                if not field:
                    if key and key not in _UNKNOWN_KEYS_SEEN:
                        _UNKNOWN_KEYS_SEEN.add(key)
                        print(f"ℹ️  [sofascore] bilinmeyen istatistik anahtari: {key} ({item.get('name')})", flush=True)
                    continue
                if field in out:
                    continue
                hv, av = item.get("homeValue"), item.get("awayValue")
                if hv is None or av is None:
                    continue
                out[field] = [hv, av]
    return out
