"""Load only the new endpoints: do not start the live API or touch its DB."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from fastapi import Request
from test_signal_retention import load_function
from app.core.ht11_shadow import SCHEMA, store_path


class HT11AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'live.db')
        ns={'Request':Request,'sqlite3':sqlite3,'DB_PATH':self.path,
            '_check_admin':lambda request: request.headers.get('x-admin-secret')=='test-only'}
        self.get=load_function('api.py','admin_panel_ht11',ns)
        self.post=load_function('api.py','admin_panel_ht11_confirm',ns)
        self.request=Request({'type':'http','headers':[(b'x-admin-secret',b'test-only')]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_endpoints_require_admin_before_db_access(self):
        req=Request({'type':'http','headers':[]})
        self.assertEqual(self.get(req).status_code,403)
        self.assertEqual(self.post(req,{}).status_code,403)
        self.assertFalse(Path(store_path(self.path)).exists())

    def test_missing_experiment_has_honest_empty_state(self):
        self.assertFalse(self.get(self.request)['initialized'])

    def test_invalid_confirmation_rejected(self):
        for h in (True,-1,1.2,None,100):
            result=self.post(self.request,dict(source_match_id='v4_1',home=h,away=1,evidence='verified'))
            self.assertEqual(result.status_code,422)

    def test_confirm_requires_evidence_and_never_overwrites(self):
        with sqlite3.connect(store_path(self.path)) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO observations(match_id,source_match_id,checkpoint,minute,home_score,away_score,initial_goals,goal_line,market,observed_at,bot_version,features_json) VALUES(1,'v4_1',60,60,1,1,2,2.5,'MS 2.5','2026-09-11 12:00:00','v1','{}')")
        payload=dict(source_match_id='v4_1',home=2,away=1,evidence='')
        self.assertEqual(self.post(self.request,payload).status_code,422)
        payload['evidence']='Independent final score checked'
        self.assertTrue(self.post(self.request,payload)['success'])
        self.assertEqual(self.post(self.request,dict(payload,home=1)).status_code,409)
        with sqlite3.connect(store_path(self.path)) as db:
            self.assertEqual(db.execute('SELECT outcome FROM observations').fetchone()[0],'WON')
