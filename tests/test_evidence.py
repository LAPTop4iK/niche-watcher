import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scanner as sc
import demand_check as dc
import db_merge
import evidence
import revenue_model as rm
import tg_digest


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.con=sc.open_db(':memory:')

    def tearDown(self):
        self.con.close()

    def snap(self, rc, date, geo='us', tid=1):
        app=sc.normalize(dict(trackId=tid,trackName='App',userRatingCount=rc,currentVersionReleaseDate='2020-01-01T00:00:00Z'),geo,'test','query')
        app['scanned_at']=date
        sc.upsert_app(self.con,app)
        sc.insert_snapshot(self.con,app)

    def test_drop_does_not_become_positive_growth(self):
        self.snap(1000,'2026-10-01T00:00:00Z')
        self.snap(900,'2026-10-10T00:00:00Z')
        self.assertIsNone(evidence.rating_velocity(self.con,1,'us')[0])

    def test_short_history_and_country_mixing(self):
        self.snap(1000,'2026-10-01T00:00:00Z')
        self.snap(1010,'2026-10-01T13:00:00Z')
        self.snap(50000,'2026-10-10T00:00:00Z','gb')
        self.assertIsNone(evidence.rating_velocity(self.con,1,'us')[0])
        self.assertIsNone(evidence.rating_velocity(self.con,1,'gb')[0])

    def test_growth_uses_exact_days_and_recent_endpoints(self):
        self.snap(1000,'2026-10-01T00:00:00Z')
        self.snap(1030,'2026-10-08T12:00:00Z')
        self.assertEqual(evidence.rating_velocity(self.con,1,'us'),(120,7.5))

    def test_counter_dip_and_recovery_stays_unknown(self):
        for rc,day in [(1000,'01'),(900,'04'),(1010,'10')]:
            self.snap(rc,f'2026-10-{day}T00:00:00Z')
        self.assertIsNone(evidence.rating_velocity(self.con,1,'us')[0])

    def test_replica_noise_within_a_run_is_not_a_counter_reset(self):
        for run,day,counts in [('a','01',(260100,260008)),('b','10',(260092,260300))]:
            for i,rc in enumerate(counts):
                app=sc.normalize(dict(trackId=1,trackName='App',userRatingCount=rc,currentVersionReleaseDate='2020-01-01T00:00:00Z'),'us','test',f'query{i}')
                app.update(scanned_at=f'2026-10-{day}T00:00:0{i}Z',run_id=run)
                sc.upsert_app(self.con,app);sc.insert_snapshot(self.con,app)
        self.assertEqual(evidence.rating_velocity(self.con,1,'us')[0],0.0)

    def test_small_drop_is_noise_and_only_a_reset_blocks(self):
        self.snap(100000,'2026-10-01T00:00:00Z')
        self.snap(99900,'2026-10-10T00:00:00Z')
        self.assertEqual(evidence.rating_velocity(self.con,1,'us')[0],0.0)
        self.snap(90000,'2026-10-12T00:00:00Z')
        self.assertIsNone(evidence.rating_velocity(self.con,1,'us')[0])

    def test_creeping_drop_is_caught_across_scans(self):
        for day,rc in [('01',1000),('04',996),('07',992),('10',988)]:
            self.snap(rc,f'2026-10-{day}T00:00:00Z')
        self.assertIsNone(evidence.rating_velocity(self.con,1,'us')[0])

    def test_alive_path_records_which_evidence_fired(self):
        now=datetime(2026,10,31,tzinfo=timezone.utc)
        self.assertEqual(evidence.activity(5,None,None,'UNKNOWN',now),('ALIVE','velocity'))
        self.assertEqual(evidence.activity(None,'2026-10-30',2,'OK',now),('ALIVE','rss'))
        self.assertEqual(evidence.activity(5,'2026-10-30',2,'OK',now),('ALIVE','rss+velocity'))
        self.assertEqual(evidence.activity(0.0,'2026-10-30',1,'OK',now),('WEAK',None))

    def test_one_new_rating_is_not_activity(self):
        self.snap(1000,'2026-10-01T00:00:00Z')
        self.snap(1001,'2026-10-10T00:00:00Z')
        vel,_=evidence.rating_velocity(self.con,1,'us')
        self.assertEqual(evidence.activity(vel,None,None,'UNKNOWN'),('UNKNOWN',None))

    def test_errors_empty_and_old_reviews_are_distinct(self):
        now=datetime(2026,10,31,tzinfo=timezone.utc)
        self.assertEqual(evidence.classify(None,None,None,'ERROR',now),'UNKNOWN')
        self.assertEqual(evidence.classify(None,None,None,'EMPTY',now),'UNKNOWN')
        self.assertEqual(evidence.classify(None,'2025-01-01',0,'OK',now),'NO_RECENT_EVIDENCE')
        self.assertEqual(evidence.classify(None,'2026-10-30',1,'OK',now),'WEAK')
        self.assertEqual(evidence.classify(None,'2026-10-30',2,'OK',now),'ALIVE')

    def test_fetch_failure_does_not_mean_dead(self):
        with patch.object(sc,'fetch_json',side_effect=RuntimeError('offline')):
            self.assertEqual(dc.fetch_demand(1,'us')[-1],'ERROR')

    def test_demand_write_and_cached_reclassification(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'db';con=sc.open_db(str(path))
            app=sc.normalize(dict(trackId=1,trackName='App',userRatingCount=1000,currentVersionReleaseDate='2020-01-01T00:00:00Z'),'us','cat','q')
            sc.upsert_app(con,app);sc.insert_snapshot(con,app);con.commit();con.close()
            with patch.object(sys,'argv',['demand_check.py','--db',str(path),'--out',str(Path(t)/'out.md'),'--delay','0']),patch.object(dc,'fetch_demand',return_value=(None,None,None,None,'ERROR')),contextlib.redirect_stdout(io.StringIO()):
                dc.main()
            con=sqlite3.connect(path)
            self.assertEqual(con.execute('SELECT verdict,fetch_status,method_version FROM demand').fetchone(),('UNKNOWN','ERROR',evidence.METHOD))
            con.close()

    def test_all_countries_categories_queries_are_kept(self):
        with tempfile.TemporaryDirectory() as t:
            t=Path(t)
            (t/'seed.json').write_text(json.dumps({'categories':[{'id':cat,'queries':{'en':['q1','q2']}} for cat in ['cat1','cat2']]}))
            (t/'markets.json').write_text(json.dumps({'markets':{g:{'lang':'en'} for g in ['us','gb']}}))
            raw={'trackId':1,'trackName':'App','userRatingCount':1000,'currentVersionReleaseDate':'2020-01-01T00:00:00Z'}
            argv=['scanner.py','--seed',str(t/'seed.json'),'--markets',str(t/'markets.json'),'--db',str(t/'db'),'--report',str(t/'report.md'),'--countries','us,gb','--no-alerts','--quiet']
            with patch.object(sys,'argv',argv),patch.object(sc,'search_apps',return_value=[raw]),patch.object(sc,'fetch_recent_reviews',return_value=None),patch.object(sc.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
                sc.main()
            with sqlite3.connect(t/'db') as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],8)
                self.assertEqual(len(evidence.latest(con)),2)
                self.assertEqual(len(evidence.latest(con,by_category=True)),4)


class MergeAndMoneyTests(unittest.TestCase):
    def test_merge_same_timestamp_countries_demand_and_idempotence(self):
        with tempfile.TemporaryDirectory() as t:
            paths=[str(Path(t)/name) for name in ['a.db','b.db']]
            for i,path in enumerate(paths):
                con=sc.open_db(path);con.executescript(dc.DEMAND_SCHEMA)
                app=sc.normalize(dict(trackId=1,trackName=['Old','New'][i],userRatingCount=1000,currentVersionReleaseDate='2020-01-01T00:00:00Z'),['us','gb'][i],'cat','q')
                app.update(scanned_at='2026-10-01T00:00:00Z',first_seen=['2026-01-01','2025-01-01'][i],last_seen=['2026-01-01','2026-10-01'][i])
                sc.upsert_app(con,app);sc.insert_snapshot(con,app)
                con.execute('INSERT INTO demand (track_id,country,checked_at,verdict) VALUES (1,?,?,?)',(app['country'],app['last_seen'],'ALIVE'))
                con.commit();con.close()
            with contextlib.redirect_stdout(io.StringIO()):
                db_merge.merge(*paths);db_merge.merge(*paths)
            with sqlite3.connect(paths[0]) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],2)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM demand').fetchone()[0],2)
                self.assertEqual(con.execute('SELECT name,first_seen,last_seen FROM apps').fetchone(),('New','2025-01-01','2026-10-01'))

    def test_money_has_finite_horizon_and_no_affinity_multiplication(self):
        markets,d=rm.load();d=dict(d,horizon_months=1,freemium_sub_convert_30d=.02,refund_rate=0)
        mk=dict(markets['us'],store_commission=.15)
        self.assertAlmostEqual(rm.ltv_sub(mk,d,10),.17)
        self.assertAlmostEqual(rm.ltv_sub(mk,dict(d,refund_rate=.1),10),.153)
        other=dict(mk,paid_affinity=5,sub_affinity=1)
        self.assertEqual(rm.ltv_sub(mk,d,10),rm.ltv_sub(other,d,10))
        self.assertEqual(rm.rpd_paid(mk,d,10),rm.rpd_paid(other,d,10))
        self.assertGreater(rm.active_days(d),d['d1_retention']+d['d7_retention']+d['d30_retention'])
        self.assertGreater(rm.model_revenue('us',mk,d,'sub',100,10,conv_top=True),rm.model_revenue('us',mk,d,'sub',100,10))


if __name__=='__main__':
    unittest.main()
