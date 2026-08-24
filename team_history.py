"""team_match_history tablosunu KENDI canli takip ettigimiz bitmis
maclardan doldurur.

NEDEN VAR (kullanici raporu 2026-08-24): /api/match/{id} endpoint'i
(mac detay modalindaki "Form" kartlari) team_match_history'den takim
formu okuyor, ama bu tablo init_db.py'de SADECE sema olarak olusturuluyor -
hicbir yerde INSERT edilmiyordu. Sonuc: her zaman "Veri Bekleniyor"
gosteriyordu, veri gelmeyecegi icin de HIC duzelmiyordu.

Bu modul, matches tablomuzdaki (status='FINISHED', first_half_home_score
dolu - bkz. api.py:_capture_fh_score, o da ayni oturumda eklendi) her
maci IKI satir olarak (ev sahibi + deplasman perspektifi) yazar - baserates.py/
odds_profile.py ile AYNI "her aciliste yeniden kur" ilkesi (veri hacmimiz
buyuk degil, tam yeniden kurmak ucuz, karmasik increment mantigina gerek yok).
"""
from db_config import connect


def build(verbose=True):
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, home_team_id, away_team_id, first_half_home_score, first_half_away_score,
               home_score, away_score, last_seen_at, league_name
        FROM matches
        WHERE status='FINISHED' AND first_half_home_score IS NOT NULL AND home_score IS NOT NULL
    ''')
    rows = cur.fetchall()

    # Idempotent: bu turda ele alinacak (FINISHED) maclara ait ONCEKI kayitlari
    # silip yeniden yaziyoruz - orchestrator her aciliste cagirdigi icin
    # tekrar tekrar cift kayit birikmesin diye.
    cur.execute("DELETE FROM team_match_history WHERE match_id IN (SELECT id FROM matches WHERE status='FINISHED')")

    yazilan = 0
    for mid, home, away, fhh, fha, hs, aws, tarih, lig in rows:
        for team, opp, is_home, fh_s, fh_c, ft_s, ft_c in (
            (home, away, 1, fhh, fha, hs, aws),
            (away, home, 0, fha, fhh, aws, hs),
        ):
            if not team:
                continue
            cur.execute('''
                INSERT INTO team_match_history
                    (team_id, match_id, opponent_team_id, league_id, match_date, is_home,
                     fh_goals_scored, fh_goals_conceded, ft_goals_scored, ft_goals_conceded)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', (team, mid, opp, lig or '', tarih, is_home, fh_s, fh_c, ft_s, ft_c))
            yazilan += 1
    conn.commit()
    conn.close()
    if verbose:
        print(f"[team_history] {yazilan} takim-mac kaydi yazildi ({len(rows)} bitmis mac).", flush=True)
    return yazilan
