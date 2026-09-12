import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from fastapi import Request
from iddaa_analysis import features, compare, dashboard, save_snapshot, load_archive
from iddaa_odds_client import fetch_prematch_events, run_cycle
from test_signal_retention import load_function


class OddsAnalysisTests(unittest.TestCase):
    def test_bad_odds_never_generate_features(self):
        for bad in (None,0,1,-1,'nan','inf','bad'):
            self.assertIsNone(features([bad,3,4,2,2]))
        self.assertAlmostEqual(features([2,3,4,2,2])[3],.5)

    def test_neighborhood_and_outcomes(self):
        x=features([2,3,4,2,2]); X=np.tile(x,(100,1)); Y=np.tile([1,0],(100,1))
        rows=[dict(date='2025-01-01',match='A vs B',score='1–1',odds=[2,3,4,2,2])]*100
        result=compare(x,rows,X,Y)
        self.assertTrue(result['sufficient'])
        self.assertEqual(result['kg']['rate'],100)
        self.assertEqual(result['over25']['rate'],0)
        self.assertGreater(result['over25']['interval'][1],0)
        self.assertFalse(compare(x,rows[:99],X[:99],Y[:99])['sufficient'])
        self.assertEqual(compare(x+.1,rows,X,Y)['samples'],0)

    def test_archive_uses_actual_scores_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.csv'
            path.write_text('Date,Mac,FTHG,FTAG,Open_H,Open_D,Open_A,Open_O25,Open_U25,pKG\n01-01-2025,A vs B,1,1,2,3,4,2,2,0\n01-01-2025,A vs B,1,1,2,3,4,2,2,0\n02-01-2025,C vs D,3,0,2,3,4,2,2,1\n03-01-2025,E vs F,-1,2,2,3,4,2,2,1\n')
            rows,X,Y,rejected=load_archive(str(path),0)
            self.assertEqual(Y.tolist(),[[1,0],[0,1]])
            self.assertEqual(rejected,2)

    def test_snapshot_expiry_started_removed_and_empty_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'live.db');now=time.time()
            self.assertFalse(dashboard(path,now)['fresh'])
            event=dict(event_id=1,home='A',away='B',kickoff=now+3600,odd_1=2,odd_x=3,odd_2=4,over25_odd=2,under25_odd=2)
            save_snapshot(path,[event,dict(event,event_id=2,kickoff=now-1),dict(event,event_id=3,over25_odd=None)],now)
            d=dashboard(path,now)
            self.assertEqual(len(d['matches']),1)
            self.assertEqual(d['missing_odds'],1)
            self.assertEqual(dashboard(path,now+901)['matches'],[])
            save_snapshot(path,[],now)
            self.assertEqual(dashboard(path,now)['matches'],[])
            with self.assertRaises(ValueError):save_snapshot(path,[event],now-1000)

    def test_exact_25_never_falls_back_to_15(self):
        session=Mock(); session.get.return_value.json.return_value={'data':{'events':[{
            'sid':1,'s':0,'hn':'A','an':'B','i':1,'d':time.time()+3600,'m':[
                {'t':1,'st':1,'o':[{'n':'1','odd':2},{'n':'0','odd':3},{'n':'2','odd':4}]},
                {'t':2,'st':101,'sov':'1.5','o':[{'n':'Üst','odd':1.2},{'n':'Alt','odd':4}]}
            ]}]}}
        event=fetch_prematch_events(session)[0]
        self.assertEqual(event['ms_over_line'],1.5)
        self.assertIsNone(event['over25_odd'])
        session.get.return_value.json.return_value['data']['events'][0]['m'].append({'t':2,'st':101,'sov':'2.5','o':[{'n':'Üst','odd':1.8},{'n':'Alt','odd':1.9}]})
        self.assertEqual(fetch_prematch_events(session)[0]['over25_odd'],1.8)

    def test_empty_feed_clears_board(self):
        session=Mock();session.post.return_value.json.return_value={'yazilan':0}
        with patch('iddaa_odds_client.fetch_prematch_events',return_value=[]):
            run_cycle(session,'http://test','test-only')
        self.assertEqual(session.post.call_args.kwargs['json']['events'],[])

    def test_admin_required(self):
        endpoint=load_function('api.py','admin_panel_iddaa_analysis',{'Request':Request,'_check_admin':lambda r:False,'DB_PATH':'unused'})
        self.assertEqual(endpoint(Request({'type':'http','headers':[]})).status_code,403)


if __name__=='__main__':
    unittest.main()
