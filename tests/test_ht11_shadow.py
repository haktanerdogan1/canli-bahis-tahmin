import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.bots.bot_ht11_next_goal import HT11NextGoalBot, timestamp
from app.core.ht11_shadow import ShadowRunner, confirm_result, report, backtest, dashboard


class HT11ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = str(Path(self.tmp.name)/"live.db")
        self.shadow = str(Path(self.tmp.name)/"ht11_shadow.sqlite3")
        self.conn = sqlite3.connect(self.live)
        self.conn.executescript("""
        CREATE TABLE matches(id INTEGER PRIMARY KEY,source_match_id TEXT,status TEXT,
        minute INTEGER,home_score INTEGER,away_score INTEGER,last_seen_at TEXT);
        CREATE TABLE live_snapshots(id INTEGER PRIMARY KEY,match_id INTEGER,minute INTEGER,
        home_score INTEGER,away_score INTEGER,captured_at TEXT);
        CREATE TABLE consensus_predictions(id INTEGER PRIMARY KEY,outcome TEXT);
        INSERT INTO consensus_predictions VALUES(1,'WON');
        ALTER TABLE matches ADD COLUMN home_team_id TEXT DEFAULT 'Home';
        ALTER TABLE matches ADD COLUMN away_team_id TEXT DEFAULT 'Away';
        ALTER TABLE matches ADD COLUMN league_name TEXT DEFAULT 'Test League';
        """)
        self.runner = ShadowRunner(self.live,self.shadow)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def feed(self, status, minute, h, a, at):
        self.conn.execute("INSERT OR REPLACE INTO matches(id,source_match_id,status,minute,home_score,away_score,last_seen_at) VALUES(1,'v4_123',?,?,?,?,?)",
                          (status,minute,h,a,at))
        self.conn.commit()
        return self.runner.run_once(timestamp(at))

    def halftime(self,h=1,a=1):
        self.feed('HT',45,h,a,'2026-09-11 12:45:00')

    def db(self):
        return sqlite3.connect(self.shadow)

    def test_unknown_halftime_not_inferred_from_current_score(self):
        self.assertEqual(self.feed('LIVE',60,1,1,'2026-09-11 13:15:00'),0)

    def test_different_halftime_rejected(self):
        self.halftime(0,0)
        self.assertEqual(self.feed('LIVE',60,1,1,'2026-09-11 13:15:00'),0)

    def test_halftime_correction_before_entry_is_respected(self):
        self.halftime()
        self.feed('HT',45,2,1,'2026-09-11 12:46:00')
        self.assertEqual(self.feed('LIVE',60,2,1,'2026-09-11 13:15:00'),0)

    def test_line_is_current_goals_plus_half_and_no_probability(self):
        self.halftime()
        self.assertEqual(self.feed('LIVE',75,5,1,'2026-09-11 13:30:00'),1)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT goal_line,probability,outcome FROM observations').fetchone(),
                             (6.5,None,None))

    def test_repeat_and_restart_do_not_duplicate_or_change_initial_score(self):
        self.halftime()
        self.feed('LIVE',60,1,1,'2026-09-11 13:15:00')
        self.runner = ShadowRunner(self.live,self.shadow)
        self.assertEqual(self.feed('LIVE',61,2,1,'2026-09-11 13:16:00'),0)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*),initial_goals FROM observations').fetchone(),(1,2))

    def test_no_late_backfill_of_missed_checkpoints(self):
        self.halftime()
        self.assertEqual(self.feed('LIVE',64,1,1,'2026-09-11 13:19:00'),0)
        self.assertEqual(self.feed('LIVE',65,1,1,'2026-09-11 13:20:00'),1)

    def test_stale_or_future_or_invalid_scores_rejected(self):
        bot = HT11NextGoalBot()
        ht=dict(source_match_id='v4_123',home_score=1,away_score=1,observed_at='2026-09-11 12:45:00')
        m=dict(id=1,source_match_id='v4_123',status='LIVE',minute=60,home_score=1,away_score=1,
               last_seen_at='2026-09-11 13:15:00')
        now=timestamp(m['last_seen_at'])
        self.assertIsNone(bot.evaluate(m,ht,now+121))
        self.assertIsNone(bot.evaluate(m,ht,now-1))
        for bad in (None,True,-1,1.5):
            self.assertIsNone(bot.evaluate(dict(m,home_score=bad),ht,now))
        self.assertIsNone(bot.evaluate(dict(m,source_match_id='v4_other'),ht,now))

    def test_finished_labels_provisional_until_evidence(self):
        self.halftime()
        self.feed('LIVE',60,1,1,'2026-09-11 13:15:00')
        self.feed('FINISHED',90,2,1,'2026-09-11 13:50:00')
        with self.db() as db:
            self.assertEqual(db.execute('SELECT observed_outcome,outcome FROM observations').fetchone(),('WON',None))
            self.assertIsNone(report(db)['checkpoints'][0]['verified_hit_rate'])
            confirm_result(db,'v4_123',2,1,'Independent final-score verification')
            self.assertEqual(report(db)['checkpoints'][0]['verified_hit_rate'],1)
            confirm_result(db,'v4_123',1,1,'Later conflicting input')
            self.assertEqual(db.execute('SELECT outcome FROM observations').fetchone()[0],'WON')

    def test_terminal_score_regression_is_not_counted_as_loss(self):
        self.halftime()
        self.feed('LIVE',60,2,1,'2026-09-11 13:15:00')
        with self.db() as db:
            confirm_result(db,'v4_123',1,1,'Confirmed score correction')
            self.assertEqual(db.execute('SELECT outcome FROM observations').fetchone()[0],'VOID')
            self.assertIsNone(report(db)['checkpoints'][0]['verified_hit_rate'])

    def test_live_database_and_consensus_untouched(self):
        self.halftime()
        self.feed('LIVE',60,1,1,'2026-09-11 13:15:00')
        self.assertEqual(self.conn.execute('SELECT * FROM consensus_predictions').fetchall(),[(1,'WON')])
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='observations'").fetchone())

    def test_lock_failure_does_not_escape_tick(self):
        self.halftime()
        with self.db() as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            self.runner.tick(timestamp('2026-09-11 13:15:00'))

    def test_backtest_excludes_unverified_and_split_match(self):
        self.halftime()
        self.feed('LIVE',60,1,1,'2026-09-11 13:15:00')
        with self.db() as db:
            self.assertEqual(backtest(db)['status'],'insufficient_data')
            template=db.execute('SELECT match_id,source_match_id,checkpoint,minute,home_score,away_score,initial_goals,goal_line,market,observed_at,bot_version,probability,features_json FROM observations').fetchone()
            for i,(date,outcome) in enumerate([('2026-09-10','WON'),('2026-09-12','LOST')],2):
                row=list(template); row[0]=i; row[1]=f'v4_{i}'; row[9]=date+' 13:15:00'
                db.execute('INSERT INTO observations(match_id,source_match_id,checkpoint,minute,home_score,away_score,initial_goals,goal_line,market,observed_at,bot_version,probability,features_json,outcome) VALUES('+','.join('?' for _ in range(14))+')',row+[outcome])
            result=backtest(db,min_train=1)
            self.assertEqual(result['checkpoints'][0]['test_scored'],1)
            self.assertEqual(result['checkpoints'][0]['brier'],1)

    def test_same_match_cannot_cross_train_test_boundary(self):
        self.halftime()
        self.feed('LIVE',60,1,1,'2026-09-11 13:15:00')
        with self.db() as db:
            db.execute("UPDATE observations SET outcome='WON',observed_at='2026-09-10 13:15:00'")
            db.execute("INSERT INTO observations(match_id,source_match_id,checkpoint,minute,home_score,away_score,initial_goals,goal_line,market,observed_at,bot_version,features_json,outcome) "
                       "SELECT match_id,source_match_id,65,65,home_score,away_score,initial_goals,goal_line,market,'2026-09-12 13:20:00',bot_version,features_json,'WON' FROM observations")
            result=backtest(db,min_train=1)
            self.assertTrue(all(r['train_matches']==0 and r['test_scored']==0 for r in result['checkpoints']))

    def test_orchestrator_calls_shadow_outside_production_votes(self):
        path=Path(__file__).resolve().parents[1]/'app/core/orchestrator.py'
        tree=ast.parse(path.read_text())
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
               and isinstance(n.func.value,ast.Name) and n.func.value.id=='ht11_shadow' and n.func.attr=='tick']
        self.assertEqual(len(calls),1)

    def test_dashboard_missing_store_does_not_create_file(self):
        self.assertFalse(dashboard(self.live)['initialized'])
        self.assertFalse(Path(self.shadow).exists())

    def test_dashboard_filters_enriches_names_and_preserves_unverified(self):
        self.halftime()
        self.feed('LIVE',60,1,1,'2026-09-11 13:15:00')
        self.feed('LIVE',65,2,1,'2026-09-11 13:20:00')
        d=dashboard(self.live,checkpoint=60,state='unverified')
        self.assertEqual(d['total'],1)
        self.assertEqual(d['observations'][0]['home_team'],'Home')
        self.assertNotIn('features_json',d['observations'][0])
        self.assertEqual(dashboard(self.live,state='WON')['total'],0)
        with self.db() as db:
            confirm_result(db,'v4_123',2,1,'Verified fixture result')
        self.assertEqual(dashboard(self.live,state='WON')['total'],1)
        self.assertEqual(dashboard(self.live,state='LOST')['total'],1)


if __name__ == '__main__':
    unittest.main()
