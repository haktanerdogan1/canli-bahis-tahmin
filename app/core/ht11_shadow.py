"""Persistent, bounded HT11 shadow experiment; never writes the live DB.

Uses a sidecar on the same persistent volume. FT labels are provisional until
independently confirmed: matches.status can also be closed by stale-feed logic.
"""
import argparse
import json
import os
import sqlite3
import statistics
import time
from pathlib import Path

from app.bots.bot_ht11_next_goal import HT11NextGoalBot, score, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS halftimes (
 source_match_id TEXT PRIMARY KEY, home_score INTEGER NOT NULL,
 away_score INTEGER NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL, source_match_id TEXT NOT NULL,
 checkpoint INTEGER NOT NULL, minute INTEGER NOT NULL, home_score INTEGER NOT NULL,
 away_score INTEGER NOT NULL, initial_goals INTEGER NOT NULL, goal_line REAL NOT NULL,
 market TEXT NOT NULL, observed_at TEXT NOT NULL, bot_version TEXT NOT NULL,
 probability REAL, features_json TEXT NOT NULL,
 observed_outcome TEXT, outcome TEXT, result_evidence TEXT,
 UNIQUE(source_match_id, checkpoint, bot_version)
);
CREATE INDEX IF NOT EXISTS ht11_pending ON observations(outcome, source_match_id);
CREATE TABLE IF NOT EXISTS monitor (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def store_path(live_path):
    return str(Path(live_path).with_name("ht11_shadow.sqlite3"))


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


class ShadowRunner:
    def __init__(self, live_path, shadow_path=None):
        self.live_path = live_path
        self.shadow_path = shadow_path or store_path(live_path)
        self.next_run = 0
        self.ready = False
        self.pending_after = 0
        self.bot = HT11NextGoalBot()

    def tick(self, now=None):
        if os.environ.get("HT11_SHADOW_ENABLED", "1") == "0":
            return
        now = time.time() if now is None else now
        if now < self.next_run:
            return
        self.next_run = now + 30
        # This boundary cannot break the existing orchestrator.
        try:
            count = self.run_once(now)
            if count:
                print(f"[ht11_shadow] yeni_gozlem={count} mode=shadow", flush=True)
        except Exception as exc:
            print(f"[ht11_shadow] ertelendi: {type(exc).__name__}: {exc}", flush=True)

    def run_once(self, now):
        store = sqlite3.connect(self.shadow_path, timeout=.25)
        store.row_factory = sqlite3.Row
        try:
            if not self.ready:
                store.executescript(SCHEMA)
                self.ready = True
            pending = [r[0] for r in store.execute(
                "SELECT DISTINCT match_id FROM observations WHERE outcome IS NULL "
                "AND observed_outcome IS NULL AND match_id>? ORDER BY match_id LIMIT 100",
                (self.pending_after,))]
            self.pending_after = pending[-1] if pending else 0
            live = readonly(self.live_path)
            read_deadline = time.monotonic() + .5
            live.set_progress_handler(lambda: int(time.monotonic() > read_deadline), 1000)
            try:
                matches = [dict(r) for r in live.execute(
                    "SELECT id,source_match_id,status,minute,home_score,away_score,last_seen_at "
                    "FROM matches WHERE status IN ('HT','LIVE') AND substr(source_match_id,1,3)='v4_' "
                    "AND last_seen_at >= ? LIMIT 500",
                    (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 120)),))]
                finals = []
                if pending:
                    finals = [dict(r) for r in live.execute(
                        "SELECT id,source_match_id,status,home_score,away_score FROM matches "
                        "WHERE id IN (" + ",".join("?" for _ in pending) + ")", pending)]
                # Read at most one optional snapshot per eligible match; no historical scan.
                snapshots = {}
                for m in matches:
                    if m["status"] == "LIVE" and m["minute"] in (60,61,65,66,70,71,75,76):
                        row = live.execute("SELECT * FROM live_snapshots WHERE match_id=? "
                                           "ORDER BY id DESC LIMIT 1", (m["id"],)).fetchone()
                        if row:
                            snapshots[m["id"]] = dict(row)
            finally:
                live.close()
            # No live DB connection is held during sidecar writes.
            for m in matches:
                seen = timestamp(m["last_seen_at"])
                if m["status"] != "HT" or seen is None or not 0 <= now - seen <= 120:
                    continue
                h, a = score(m["home_score"]), score(m["away_score"])
                if h is None or a is None:
                    continue
                store.execute("INSERT INTO halftimes VALUES (?,?,?,?) "
                              "ON CONFLICT(source_match_id) DO UPDATE SET "
                              "home_score=excluded.home_score,away_score=excluded.away_score,"
                              "observed_at=excluded.observed_at",
                              (m["source_match_id"], h, a, m["last_seen_at"]))
            count = 0
            for m in matches:
                ht = store.execute("SELECT * FROM halftimes WHERE source_match_id=?",
                                   (m["source_match_id"],)).fetchone()
                candidate = self.bot.evaluate(m, dict(ht) if ht else None, now)
                if not candidate:
                    continue
                snap = snapshots.get(m["id"], {})
                # Score observations do NOT imply fresh shot/xG stats. Keep provenance.
                features = dict(snapshot=snap, stats_used_for_prediction=False,
                                home_half_score=1, away_half_score=1,
                                halftime_observed_at=ht["observed_at"])
                fields = ("match_id","source_match_id","checkpoint","minute","home_score",
                          "away_score","initial_goals","goal_line","market","observed_at",
                          "bot_version","probability")
                cur = store.execute("INSERT OR IGNORE INTO observations (" + ",".join(fields) +
                                    ",features_json) VALUES (" + ",".join("?" for _ in range(13)) + ")",
                                    tuple(candidate[k] for k in fields) +
                                    (json.dumps(features, ensure_ascii=False),))
                count += cur.rowcount
            for m in finals:
                h, a = score(m["home_score"]), score(m["away_score"])
                if m["status"] == "FINISHED" and h is not None and a is not None:
                    store.execute("UPDATE observations SET observed_outcome=CASE "
                                  "WHEN home_score>? OR away_score>? THEN 'INVALID_SCORE' "
                                  "WHEN initial_goals<? THEN 'WON' ELSE 'LOST' END "
                                  "WHERE source_match_id=? AND outcome IS NULL "
                                  "AND observed_outcome IS NULL", (h,a,h+a,m["source_match_id"]))
                elif m["status"] in ("ABANDONED","Canceled"):
                    store.execute("UPDATE observations SET observed_outcome='UNRESOLVED' "
                                  "WHERE source_match_id=? AND outcome IS NULL "
                                  "AND observed_outcome IS NULL", (m["source_match_id"],))
            store.execute("INSERT INTO monitor VALUES ('last_tick',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            store.commit()
            return count
        finally:
            store.close()


def confirm_result(conn, source_id, home, away, evidence):
    if score(home) is None or score(away) is None or not evidence.strip():
        raise ValueError("Doğrulanmış maç sonu skoru ve kaynak açıklaması gerekli.")
    conn.execute("UPDATE observations SET outcome=CASE "
                 "WHEN home_score>? OR away_score>? THEN 'VOID' "
                 "WHEN initial_goals<? THEN 'WON' ELSE 'LOST' END, result_evidence=? "
                 "WHERE source_match_id=? AND outcome IS NULL",
                 (home,away,home+away,evidence,source_id))
    conn.commit()


def dashboard(live_path, checkpoint=0, state="all", limit=200):
    path = Path(store_path(live_path))
    empty = dict(success=True, initialized=False, running=False, last_tick=None,
                 enabled=os.environ.get("HT11_SHADOW_ENABLED", "1") != "0",
                 checkpoints=[], observations=[], watching=[], total=0)
    if not path.exists():
        return empty
    conn = readonly(path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='observations'").fetchone():
            return empty
        summary = report(conn)
        monitor_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor'").fetchone()
        tick = conn.execute("SELECT value FROM monitor WHERE key='last_tick'").fetchone() if monitor_exists else None
        last_tick = float(tick[0]) if tick else None
        conditions, params = [], []
        if checkpoint in (60,65,70,75):
            conditions.append("checkpoint=?"); params.append(checkpoint)
        if state in ("WON","LOST","VOID"):
            conditions.append("outcome=?"); params.append(state)
        elif state == "unverified":
            conditions.append("outcome IS NULL")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = conn.execute("SELECT COUNT(*) FROM observations"+where, params).fetchone()[0]
        # Raw snapshots are not sent to the browser.
        rows = [dict(r) for r in conn.execute(
            "SELECT id,match_id,source_match_id,checkpoint,minute,home_score,away_score,market,"
            "observed_at,observed_outcome,outcome,result_evidence FROM observations"+where+
            " ORDER BY observed_at DESC,id DESC LIMIT ?", params+[max(1,min(limit,200))])]
        halves = [dict(r) for r in conn.execute(
            "SELECT source_match_id,observed_at FROM halftimes WHERE home_score=1 AND away_score=1 "
            "AND observed_at >= datetime('now','-4 hours') ORDER BY observed_at DESC LIMIT 200")]
    finally:
        conn.close()
    sources = sorted({r['source_match_id'] for r in rows+halves})
    matches = {}
    if sources:
        live = readonly(live_path)
        try:
            for r in live.execute("SELECT source_match_id,home_team_id,away_team_id,league_name,"
                                  "minute,status,home_score,away_score FROM matches WHERE source_match_id IN ("+
                                  ",".join("?" for _ in sources)+")",sources):
                matches[r['source_match_id']] = dict(r)
        finally:
            live.close()
    for r in rows:
        m = matches.get(r['source_match_id'], {})
        r.update(home_team=m.get('home_team_id',r['source_match_id']),
                 away_team=m.get('away_team_id',''),league=m.get('league_name',''),
                 current_home=m.get('home_score'),current_away=m.get('away_score'),
                 match_status=m.get('status'))
    watching = [matches[h['source_match_id']] for h in halves
                if matches.get(h['source_match_id'],{}).get('status') in ('LIVE','HT')]
    return dict(success=True,initialized=True,enabled=empty['enabled'],
                running=last_tick is not None and 0 <= time.time()-last_tick < 120,
                last_tick=last_tick,checkpoints=summary['checkpoints'],observations=rows,
                watching=watching,total=total)


def report(conn):
    conn.row_factory = sqlite3.Row
    result = []
    for r in conn.execute("SELECT checkpoint, COUNT(*) AS observations, "
                          "SUM(outcome='WON') AS won,SUM(outcome='LOST') AS lost, "
                          "SUM(outcome='VOID') AS void, "
                          "SUM(observed_outcome='WON' AND outcome IS NULL) AS provisional_won, "
                          "SUM(observed_outcome='LOST' AND outcome IS NULL) AS provisional_lost, "
                          "SUM(outcome IS NULL) AS unverified "
                          "FROM observations GROUP BY checkpoint ORDER BY checkpoint"):
        item = dict(r)
        n = (item["won"] or 0) + (item["lost"] or 0)
        item["verified_hit_rate"] = item["won"] / n if n else None
        result.append(item)
    return dict(mode="shadow", production_enabled=False, checkpoints=result)


def backtest(conn, min_train=60):
    """Match-disjoint chronological holdout. Unknown/VOID outcomes never train.

Uses score-only cells; snapshot features are deliberately not trusted yet.
No model is activated by this diagnostic, even if metrics look promising.
"""
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE outcome IN ('WON','LOST') ORDER BY observed_at")]
    dates = sorted({r["observed_at"][:10] for r in rows})
    if len(dates) < 2:
        return dict(status="insufficient_data", reason="En az iki ayrı tarihte doğrulanmış sonuç gerekli.")
    cutoff = dates[len(dates)//2]
    # A match spanning the boundary is excluded, never shared between train and test.
    spans = {}
    for r in rows:
        spans.setdefault(r["source_match_id"], set()).add(r["observed_at"][:10] < cutoff)
    rows = [r for r in rows if len(spans[r["source_match_id"]]) == 1]
    train = [r for r in rows if r["observed_at"][:10] < cutoff]
    test = [r for r in rows if r["observed_at"][:10] >= cutoff]
    metrics = []
    for checkpoint in (60,65,70,75):
        training = [r for r in train if r["checkpoint"] == checkpoint]
        cells = {}
        for r in training:
            key = (r["initial_goals"], abs(r["home_score"]-r["away_score"]))
            cells.setdefault(key, []).append(int(r["outcome"] == "WON"))
        predictions, targets = [], []
        for r in test:
            if r["checkpoint"] != checkpoint:
                continue
            cell = cells.get((r["initial_goals"], abs(r["home_score"]-r["away_score"])), [])
            if len(cell) < min_train:
                continue
            predictions.append(statistics.mean(cell))
            targets.append(int(r["outcome"] == "WON"))
        baseline = statistics.mean(r["outcome"] == "WON" for r in training) if training else None
        n = len(targets)
        corr = (statistics.correlation(predictions,targets)
                if n >= 3 and len(set(predictions))>1 and len(set(targets))>1 else None)
        metrics.append(dict(checkpoint=checkpoint,train_matches=len(training),test_scored=n,
                            brier=statistics.mean((p-y)**2 for p,y in zip(predictions,targets)) if n else None,
                            baseline_brier=statistics.mean((baseline-y)**2 for y in targets) if n else None,
                            correlation=corr))
    return dict(status="diagnostic_only", cutoff=cutoff, min_cell_samples=min_train,
                production_enabled=False, checkpoints=metrics)


def main():
    parser = argparse.ArgumentParser(description="HT 1-1 deneme botu; canlı sinyal yayımlamaz")
    parser.add_argument("--db", required=True, help="ht11_shadow.sqlite3 yolu")
    parser.add_argument("--confirm-result", nargs=3, metavar=("SOURCE_ID","HOME","AWAY"))
    parser.add_argument("--evidence", default="")
    parser.add_argument("--backtest", action="store_true")
    args = parser.parse_args()
    conn = (sqlite3.connect(Path(args.db).resolve().as_uri()+"?mode=rw", uri=True)
            if args.confirm_result else readonly(args.db))
    try:
        if args.confirm_result:
            sid,h,a = args.confirm_result
            confirm_result(conn,sid,int(h),int(a),args.evidence)
        print(json.dumps(backtest(conn) if args.backtest else report(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
