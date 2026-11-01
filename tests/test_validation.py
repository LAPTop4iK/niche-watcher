import contextlib
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scanner as sc
import scan_state
import evidence
import notifications
import db_merge
import charts_scan
import play_scan
import review_clusters
import revenue_model as rm
from relevance import relevant
from hypotheses import pearson,ranks


class Fixtures(unittest.TestCase):
    def make(self,con,tid=1,country='us',category='construction',query='concrete calculator',count=1000,date='2026-10-31T00:00:00Z',version='2020-01-01T00:00:00Z',run_id=None,name='Concrete Calculator'):
        app=sc.normalize(dict(trackId=tid,trackName=name,userRatingCount=count,currentVersionReleaseDate=version,price=3.99,currency='USD'),country,category,query)
        app.update(scanned_at=date,run_id=run_id)
        sc.upsert_app(con,app);sc.insert_snapshot(con,app)
        return app


class FreshnessAndSampling(Fixtures):
    def test_old_velocity_is_not_current_when_rss_missing(self):
        con=sc.open_db(':memory:')
        self.make(con,count=1000,date='2026-01-01T00:00:00Z')
        self.make(con,count=1010,date='2026-01-10T00:00:00Z')
        row=evidence.latest(con)[0]
        a=evidence.assess(con,row,{},datetime(2026,10,31,tzinfo=timezone.utc))
        self.assertIsNone(a['velocity']);self.assertEqual(a['verdict'],'UNKNOWN')

    def test_category_uses_current_metadata(self):
        con=sc.open_db(':memory:')
        self.make(con,category='construction',date='2026-10-01T00:00:00Z')
        self.make(con,category='diy',date='2026-10-30T00:00:00Z',version='2026-10-29T00:00:00Z')
        rows=evidence.latest(con,by_category=True)
        self.assertEqual({r['category'] for r in rows},{'construction','diy'})
        self.assertFalse(any(evidence.is_candidate(r) for r in rows))

    def test_empty_completed_serp_removes_historic_membership(self):
        con=sc.open_db(':memory:');job=('us','construction','concrete calculator')
        self.make(con)
        run=scan_state.start_run(con,[job],{})
        scan_state.begin_query(con,run,*job);scan_state.end_query(con,run,*job,'OK',0,0)
        scan_state.finish_run(con,run)
        self.assertEqual(evidence.latest(con,by_category=True),[])
        self.assertEqual(len(evidence.latest(con)),1)  # retained history
        self.assertFalse(evidence.is_candidate(evidence.latest(con)[0]))

    def test_partial_scan_does_not_erase_previous_sample(self):
        con=sc.open_db(':memory:');self.make(con)
        job=('us','construction','concrete calculator');run=scan_state.start_run(con,[job],{})
        scan_state.begin_query(con,run,*job);scan_state.end_query(con,run,*job,'ERROR',error='offline')
        scan_state.finish_run(con,run)
        self.assertEqual(len(evidence.latest(con,by_category=True)),1)

    def test_successful_query_in_partial_run_updates_only_that_query(self):
        con=sc.open_db(':memory:')
        self.make(con,tid=1,query='one');self.make(con,tid=2,query='two')
        jobs=[('us','construction','one'),('us','construction','two')]
        rid=scan_state.start_run(con,jobs,{})
        for job,status in zip(jobs,['OK','ERROR']):
            scan_state.begin_query(con,rid,*job);scan_state.end_query(con,rid,*job,status,0,0)
        scan_state.finish_run(con,rid)
        self.assertEqual({r['track_id'] for r in evidence.latest(con,by_category=True)},{2})

    def test_rank_comparison_uses_query_runs_and_seven_day_baseline(self):
        import divergence
        con=sc.open_db(':memory:');job=('de','construction','q')
        for run,date,rank in [('older','2026-10-01T00:00:00Z',35),('retry','2026-10-30T01:00:00Z',40),('new','2026-10-30T02:00:00Z',10)]:
            scan_state.start_run(con,[job],{'limit':50},run);scan_state.begin_query(con,run,*job)
            self.make(con,country='de',query='q',date=date,run_id=run,name='Lokaler Name')
            con.execute('UPDATE snapshots SET rank=? WHERE run_id=?',(rank,run))
            scan_state.end_query(con,run,*job,'OK',1,1)
            con.execute('UPDATE scan_queries SET finished_at=? WHERE run_id=?',(date,run))
        movers=divergence.rank_movers(con)
        self.assertEqual(len(movers),1)
        self.assertEqual(movers[0][3:6],(35,10,'Lokaler Name'))
        self.assertIn('/de/',movers[0][6])

    def test_play_queries_do_not_erase_app_store_membership(self):
        con=sc.open_db(':memory:');self.make(con)
        job=('us','construction','concrete calculator')
        run=scan_state.start_run(con,[job],{'source':'google-play'})
        scan_state.begin_query(con,run,*job);scan_state.end_query(con,run,*job,'OK',0,0)
        scan_state.finish_run(con,run)
        self.assertEqual(len(evidence.latest(con,by_category=True)),1)

    def test_study_requires_complete_and_keeps_frozen_features(self):
        import study_sample
        con=sc.open_db(':memory:');job=('us','construction','q')
        run=scan_state.start_run(con,[job],{},'study')
        with self.assertRaises(ValueError):study_sample.freeze(con,run)
        scan_state.begin_query(con,run,*job)
        self.make(con,query='q',run_id=run,version='2020-01-01T00:00:00Z')
        scan_state.end_query(con,run,*job,'OK',1,1);scan_state.finish_run(con,run)
        study_sample.freeze(con,run)
        self.make(con,query='q',version='2026-10-30T00:00:00Z')
        study_sample.freeze(con,run)
        row=con.execute('SELECT role,version_date FROM study_members').fetchone()
        self.assertEqual(tuple(row),('CANDIDATE','2020-01-01T00:00:00Z'))

    def test_historical_study_is_not_rewritten_by_future_reviews(self):
        import demand_check,study_sample,hypotheses
        con=sc.open_db(':memory:');job=('us','construction','q')
        run=scan_state.start_run(con,[job],{},'frozen')
        scan_state.begin_query(con,run,*job);self.make(con,query='q',run_id=run)
        scan_state.end_query(con,run,*job,'OK',1,1);scan_state.finish_run(con,run)
        demand_check.prepare(con)
        demand_check.store_result(con,1,'us',demand_check.DemandFetch(('2026-10-30',None,2,0,'OK'),['2026-10-29','2026-10-30'],[5,5]))
        study_sample.freeze(con,run);study_sample.freeze_reviews(con,run)
        first=hypotheses.build(con)
        con.execute("UPDATE demand SET review_dates_json='[\"2020-01-01\"]',r90=0,last_review='2020-01-01'")
        self.make(con,query='q',date='2027-01-01T00:00:00Z',version='2026-12-31T00:00:00Z')
        second=hypotheses.build(con,datetime(2027,1,2,tzinfo=timezone.utc))
        self.assertEqual(first['activity_comparison'],second['activity_comparison'])
        self.assertEqual(second['activity_comparison']['old']['active'],1)
        self.assertEqual(first['analysis_asof'],second['analysis_asof'])

    def test_new_launch_window_is_exactly_seven_days_at_search_completion(self):
        import hypotheses
        con=sc.open_db(':memory:');job=('us','construction','q')
        rid=scan_state.start_run(con,[job],{'limit':50},'release-window')
        scan_state.begin_query(con,rid,*job)
        for tid,release in [(1,'2026-10-24T22:00:00Z'),(2,'2026-10-24T20:00:00Z')]:
            self.make(con,tid=tid,query='q',run_id=rid)
            con.execute('UPDATE snapshots SET release_date=? WHERE track_id=?',(release,tid))
        scan_state.end_query(con,rid,*job,'OK',2,2);scan_state.finish_run(con,rid)
        con.execute("UPDATE scan_runs SET started_at='2026-10-31T19:00:00Z',finished_at='2026-10-31T21:00:00Z'")
        con.execute("UPDATE apps SET first_seen='2026-10-31T19:30:00Z'")
        result=hypotheses.build(con)['discovery_vs_launch']
        self.assertEqual(result['new_unique_apps'],2)
        self.assertEqual(result['released_unique_apps_7d'],1)

    def test_all_current_queries_survive_within_one_category(self):
        con=sc.open_db(':memory:')
        self.make(con,query='concrete calculator')
        self.make(con,query='stair calculator')
        sources=evidence.latest(con)[0]['source_queries']['construction']
        self.assertEqual(set(sources),{'concrete calculator','stair calculator'})

    def test_review_window_rolls_from_stored_dates(self):
        con=sc.open_db(':memory:');self.make(con)
        now=datetime(2026,10,31,tzinfo=timezone.utc)
        cache=dict(checked_at='2026-10-30T00:00:00Z',last_review='2026-10-29',r90=2,fetch_status='OK',review_dates_json=json.dumps(['2026-10-29','2026-07-01']),review_scores_json='[1,5]')
        a=evidence.assess(con,evidence.latest(con)[0],cache,now)
        self.assertEqual(a['r90'],1);self.assertEqual(a['bad_recent_count'],1)
        self.assertEqual(a['verdict'],'WEAK')

    def test_same_timestamp_conflicting_counters_stays_unknown(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2026-10-01T00:00:00Z',count=1000)
        self.make(con,date='2026-10-10T00:00:00Z',count=1020)
        self.make(con,date='2026-10-10T00:00:00Z',count=1010)
        self.assertIsNone(evidence.rating_velocity(con,1,'us')[0])

    def test_reset_before_nearest_baseline_does_not_become_recovery_growth(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2026-09-25T00:00:00Z',count=1000)
        self.make(con,date='2026-10-01T00:00:00Z',count=900)
        self.make(con,date='2026-10-15T00:00:00Z',count=1000)
        self.assertIsNone(evidence.rating_velocity(con,1,'us')[0])

    def test_conflicting_baseline_does_not_get_hidden_by_early_return(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2026-10-01T00:00:00Z',count=1000)
        self.make(con,date='2026-10-01T00:00:00Z',count=995)
        self.make(con,date='2026-10-10T00:00:00Z',count=1020)
        self.assertIsNone(evidence.rating_velocity(con,1,'us')[0])

    def test_new_age_threshold_is_not_flagged_in_the_past(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2021-01-01T00:00:00Z',version='2020-01-01T00:00:00Z')
        self.assertFalse(sc.was_flagged_before(con,1,18,500,country='us'))

    def test_storefront_name_and_price_are_not_global(self):
        con=sc.open_db(':memory:')
        self.make(con,country='us',name='US name')
        self.make(con,country='de',name='DE Name')
        rows={r['country']:r for r in evidence.latest(con)}
        self.assertEqual(rows['us']['name'],'US name');self.assertEqual(rows['de']['name'],'DE Name')
        self.assertEqual(rows['us']['price'],3.99)


class DigestScope(Fixtures):
    def test_velocity_digest_uses_country_and_excludes_updated_apps(self):
        import tg_digest
        con=sc.open_db(':memory:')
        self.make(con,date='2026-10-01T00:00:00Z',count=1000,name='US name')
        self.make(con,date='2026-10-30T00:00:00Z',count=1010,name='US name')
        self.make(con,country='gb',date='2026-10-30T00:00:00Z',count=100,name='GB name')
        self.make(con,tid=2,date='2026-10-01T00:00:00Z',count=2000,name='Fresh app',version='2026-10-01T00:00:00Z')
        self.make(con,tid=2,date='2026-10-30T00:00:00Z',count=4000,name='Fresh app',version='2026-10-01T00:00:00Z')
        msg=tg_digest.build_message(con)
        self.assertIn('US name',msg)
        self.assertNotIn('GB name',msg)
        self.assertNotIn('Fresh app',msg)


class Durability(Fixtures):
    def test_resume_rejects_changed_configuration(self):
        con=sc.open_db(':memory:');jobs=[('us','c','q')]
        scan_state.start_run(con,jobs,{'limit':50},'run')
        with self.assertRaises(ValueError):
            scan_state.start_run(con,jobs,{'limit':100},'run')

    def test_validator_rejects_complete_with_wrong_query_keys(self):
        import validate_data
        con=sc.open_db(':memory:');job=('us','c','q')
        rid=scan_state.start_run(con,[job],{})
        scan_state.begin_query(con,rid,'de','c','q');scan_state.end_query(con,rid,'de','c','q','OK',0,0)
        scan_state.finish_run(con,rid)
        _,errors=validate_data.validate(con,True)
        self.assertTrue(any('query keys' in error for error in errors))

    def test_partial_is_not_success_and_retry_completes(self):
        con=sc.open_db(':memory:');job=('us','c','q')
        rid=scan_state.start_run(con,[job],{})
        scan_state.begin_query(con,rid,*job);scan_state.end_query(con,rid,*job,'ERROR',error='offline')
        self.assertEqual(scan_state.finish_run(con,rid),('PARTIAL',0,1))
        scan_state.begin_query(con,rid,*job);scan_state.end_query(con,rid,*job,'OK',0,0)
        self.assertEqual(scan_state.finish_run(con,rid),('COMPLETE',1,1))

    def test_notification_retry_and_duplicate_event(self):
        con=sqlite3.connect(':memory:')
        notifications.enqueue(con,'one','payload');notifications.enqueue(con,'one','payload')
        self.assertEqual(notifications.flush(con,lambda p:False),(0,1))
        self.assertEqual(notifications.flush(con,lambda p:True),(1,0))
        self.assertEqual(notifications.flush(con,lambda p:self.fail('already sent')),(0,0))
        self.assertEqual(con.execute('SELECT status,attempts FROM notification_outbox').fetchone(),('SENT',2))

    def test_merge_preserves_run_coverage_and_sent_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            paths=[str(Path(folder)/n) for n in ('a','b')]
            for i,path in enumerate(paths):
                con=sc.open_db(path);job=('us','c','q')
                rid=scan_state.start_run(con,[job],{},'same')
                scan_state.begin_query(con,rid,*job)
                scan_state.end_query(con,rid,*job,'OK' if i else 'ERROR',0,0)
                scan_state.finish_run(con,rid)
                notifications.enqueue(con,'e','payload')
                if i:
                    notifications.flush(con,lambda p:True)
                con.close()
            with contextlib.redirect_stdout(io.StringIO()):
                db_merge.merge(paths[0],paths[1]);db_merge.merge(paths[1],paths[0])
            for path in paths:
                con=sqlite3.connect(path)
                self.assertEqual(con.execute('SELECT status FROM scan_runs').fetchone()[0],'COMPLETE')
                self.assertEqual(con.execute('SELECT status FROM scan_queries').fetchone()[0],'OK')
                self.assertEqual(con.execute('SELECT status FROM notification_outbox').fetchone()[0],'SENT')
                con.close()

    def test_scanner_resume_does_not_repeat_successful_network_queries(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed=base/'seed';markets=base/'markets'
            seed.write_text(json.dumps({'categories':[{'id':'c','queries':{'en':['q']}}]}))
            markets.write_text(json.dumps({'markets':{'us':{'lang':'en'}}}))
            args=['scanner.py','--seed',str(seed),'--markets',str(markets),'--countries','us','--db',str(base/'db'),'--report',str(base/'report'),'--no-alerts','--skip-reviews','--run-id','test','--quiet']
            with patch.object(sys,'argv',args),patch.object(sc,'search_apps',return_value=[]),contextlib.redirect_stdout(io.StringIO()):
                sc.main()
            with patch.object(sys,'argv',args),patch.object(sc,'search_apps',side_effect=AssertionError('repeat')),contextlib.redirect_stdout(io.StringIO()):
                sc.main()


class CompressedStorage(Fixtures):
    def test_roundtrip_and_restore_never_overwrites_working_data(self):
        import db_store
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db'
            con=sc.open_db(path);self.make(con);con.commit();con.close()
            archive=db_store.pack(path);first=archive.read_bytes()
            self.assertEqual(db_store.pack(path).read_bytes(),first)
            path.unlink();self.assertTrue(db_store.ensure(path))
            con=sc.open_db(path);self.make(con,tid=2);con.commit();con.close()
            self.assertFalse(db_store.ensure(path))
            with sqlite3.connect(path) as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM apps').fetchone()[0],2)

    def test_corrupt_archive_does_not_create_a_working_database(self):
        import db_store
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';Path(str(path)+'.gz').write_bytes(b'broken')
            with self.assertRaises(Exception):db_store.ensure(path)
            self.assertFalse(path.exists())


class Presentation(Fixtures):
    def test_alert_footer_has_one_dashboard_link_and_no_filename_domain(self):
        import tg_digest
        con=sc.open_db(':memory:');app=self.make(con)
        alert=sc.build_alert_message([sc.analyze(con,app,18,500)],1)
        self.assertNotIn('report.md',alert)
        self.assertNotIn('с динамикой',alert)
        self.assertEqual(alert.count('Открыть дашборд'),1)
        self.assertEqual(tg_digest.build_message(con).count('Открыть дашборд'),1)

    def test_dashboard_keeps_source_coverage_separate(self):
        import dashboard
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';con=sc.open_db(path)
            self.make(con,country='us',category='construction')
            self.make(con,country='us',category='chart:music')
            self.make(con,tid=2,country='de',category='chart:music')
            con.commit();con.close();data=dashboard.build_data(path)
            self.assertEqual(data['source_coverage']['search'],['us'])
            self.assertEqual(data['source_coverage']['charts'],['de','us'])
            both=next(r for r in data['candidates'] if r['track_id']==1)
            self.assertEqual(both['sources'],['charts','search'])


class SourceAndMath(unittest.TestCase):
    def test_relevance_supports_scripts_and_rejects_generic_false_positive(self):
        self.assertFalse(sc.is_native_risk('Concrete Calculator','concrete calculator'))
        self.assertTrue(sc.is_native_risk('Calculator','calculator'))
        self.assertFalse(relevant('Calculator','stair calculator'))
        self.assertFalse(relevant('Runkeeper Run Tracker','mileage tracker'))
        self.assertTrue(relevant('Concrete Calculator Plus','concrete calculator'))
        self.assertTrue(relevant('מחשבון בטון','מחשבון בטון'))
        self.assertTrue(relevant('حاسبة الخرسانة','حاسبة الخرسانة'))
        self.assertTrue(relevant('硬幣識別器','硬幣識別'))

    def test_page_link_extraction_does_not_drop_first_or_mix_storefront_seeds(self):
        import similar_crawl
        text='https://apps.apple.com/us/app/id1 https://apps.apple.com/de/app/name/id2 https://apps.apple.com/us/app/other/id3'
        self.assertEqual(similar_crawl.extract_ids(text,1),['2','3'])
        self.assertEqual(similar_crawl.report_seeds(text,'us',5),['1','3'])
        self.assertEqual(similar_crawl.extract_ids('/app/id3 /app/title/id1 /app/id3',1),['3'])

    def test_review_window_uses_timestamp_instead_of_midnight(self):
        import demand_check
        now=datetime(2026,10,31,12,tzinfo=timezone.utc)
        feed={'feed':{'entry':[{'id':{'label':'1'},'im:rating':{'label':'2'},'updated':{'label':'2026-08-02T13:00:00Z'}}]}}
        with patch.object(sc,'fetch_json',return_value=feed),patch.object(sc,'now_utc',return_value=now):
            result=demand_check.fetch_demand(1,'us')
        self.assertEqual(result[2],1)
        self.assertEqual(evidence.classify(None,result[0],result[2],'OK',now),'WEAK')
        feed['feed']['entry'][0]['updated']['label']='2026-08-02T11:00:00Z'
        with patch.object(sc,'fetch_json',return_value=feed),patch.object(sc,'now_utc',return_value=now):
            self.assertEqual(demand_check.fetch_demand(1,'us')[2],0)

    def test_chart_rotation_requires_the_same_feed_limit(self):
        con=sc.open_db(':memory:');job=('us','chart:music','topfreeapplications')
        run=scan_state.start_run(con,[job],{'source':'apple-charts','top':200})
        scan_state.begin_query(con,run,*job)
        app=sc.normalize(dict(trackId=1,trackName='Test'),'us',job[1],job[2]);app['run_id']=run
        sc.upsert_app(con,app);sc.insert_snapshot(con,app)
        scan_state.end_query(con,run,*job,'OK',1,1)
        con.execute("UPDATE scan_queries SET finished_at='2026-10-01T00:00:00Z'")
        self.assertIsNone(charts_scan.previous_ids(con,*job,'2026-11-01T00:00:00Z',10))
        self.assertEqual(charts_scan.previous_ids(con,*job,'2026-11-01T00:00:00Z',200),{1})

    def test_chart_dedup_and_min_ratings(self):
        app=dict(trackId=1,currentVersionReleaseDate='2020-01-01T00:00:00Z',userRatingCount=10)
        row=charts_scan.summarize([app,app],min_ratings=500)
        self.assertEqual(row['resolved'],1);self.assertEqual(row['candidates'],0)

    def test_ad_days_and_subscription_closed_forms(self):
        markets,d=rm.load();mk=dict(markets['us'],store_commission=.15)
        d=dict(d,d1_retention=1,d7_retention=1,d30_retention=1,monthly_churn_utility=0,horizon_months=12,freemium_sub_convert_30d=.02,refund_rate=0)
        self.assertEqual(rm.active_days(d),31)
        self.assertAlmostEqual(rm.ltv_sub(mk,d,10),2.04)
        self.assertAlmostEqual(rm.ltv_sub(mk,dict(d,monthly_churn_utility=1),10),.17)

    def test_invalid_economics_rejected(self):
        markets,d=rm.load();mk=markets['us']
        for changes in [{'monthly_churn_utility':-1},{'horizon_months':0},{'horizon_months':1.2},{'d1_retention':3},{'refund_rate':float('nan')}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                rm.model_revenue('us',mk,dict(d,**changes),'sub',100,5)
        with self.assertRaises(ValueError):
            rm.model_revenue('us',mk,d,'hybrid',100,5,ads_share=2)

    def test_calibration_does_not_call_period_ratio_a_conversion(self):
        import calibrate
        for matched in [False,True]:
            with self.subTest(matched=matched),tempfile.TemporaryDirectory() as folder:
                path=Path(folder);(path/'markets.json').write_text('{}')
                args=['calibrate.py','--installs','100','--ratings-gained','1','--paying','10','--revenue','20','--apply']
                if matched:args.append('--matched-cohort')
                with patch.object(sys,'argv',args),patch.object(calibrate,'HERE',folder),contextlib.redirect_stdout(io.StringIO()):
                    calibrate.main()
                fact=json.loads((path/'markets.json').read_text())['_calibration']
                self.assertEqual(fact['install_to_paying'],.1 if matched else None)
                self.assertEqual(fact['period_payers_to_installs_ratio'],.1)

    def test_statistics_degenerate_and_ties(self):
        self.assertIsNone(pearson([1,1,1],[1,2,3]))
        self.assertAlmostEqual(pearson([1,2,3],[3,2,1]),-1)
        self.assertEqual(ranks([10,10,20]),[1.5,1.5,3])

    def test_telegram_http_200_without_ack_is_failure(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"ok":false}'
        with patch.dict('os.environ',{'TG_BOT_TOKEN':'test-token','TG_CHAT_ID':'1'}),patch.object(sc.urllib.request,'urlopen',return_value=Response()),patch.object(sc.time,'sleep'):
            self.assertFalse(sc.send_telegram('test'))

    def test_malformed_play_results_are_not_successful_empty_queries(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed=base/'seed';markets=base/'markets'
            seed.write_text(json.dumps({'categories':[{'id':'c','queries':{'en':['query']}}]}))
            markets.write_text(json.dumps({'markets':{'us':{'lang':'en'}}}))
            args=['play_scan.py','--seed',str(seed),'--markets',str(markets),'--countries','us','--db',str(base/'db')]
            with patch.object(sys,'argv',args),patch.object(play_scan,'search',return_value=[{'appId':None}]),patch.object(play_scan.time,'sleep'),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit):
                play_scan.main()
            with sqlite3.connect(base/'db') as con:
                self.assertEqual(con.execute('SELECT status FROM scan_runs').fetchone()[0],'PARTIAL')
                self.assertEqual(con.execute('SELECT status FROM scan_queries').fetchone()[0],'ERROR')

    def test_play_review_analysis_excludes_old_reviews_and_duplicates(self):
        import play_reviews
        from datetime import timedelta
        now=datetime.now(timezone.utc)
        rows=[dict(reviewId='old',at=now-timedelta(days=200),score=1,content='broken'),
              dict(reviewId='new',at=now-timedelta(days=1),score=5,content='good')]
        with patch.object(play_reviews,'gp_app',return_value={}),patch.object(play_reviews,'fetch_play_reviews',return_value=rows+[rows[1]]):
            result=play_reviews.analyze_app('app','us','en',300)
        self.assertEqual(result['n_reviews'],1);self.assertEqual(result['bad_share'],0)

    def test_play_partial_metadata_is_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed=base/'seed';markets=base/'markets'
            seed.write_text(json.dumps({'categories':[{'id':'c','queries':{'en':['query']}}]}))
            markets.write_text(json.dumps({'markets':{'us':{'lang':'en'}}}))
            args=['play_scan.py','--seed',str(seed),'--markets',str(markets),'--countries','us','--db',str(base/'db')]
            good=dict(title='App',developer='Dev',installs='1000+',price=0,genre='Tools',updated=1600000000,score=4,ratings=1000)
            with patch.object(sys,'argv',args),patch.object(play_scan,'search',return_value=[{'appId':'com.one'},{'appId':'com.two'}]),patch.object(play_scan,'gp_app',side_effect=[good,RuntimeError('source failed')]),patch.object(play_scan.time,'sleep'),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit):
                play_scan.main()
            con=sqlite3.connect(base/'db')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM play_snapshots').fetchone()[0],0)
            self.assertEqual(con.execute('SELECT status,stored_count FROM scan_queries').fetchone(),('ERROR',0))
            con.close()

    def test_play_queries_are_localized_phrases(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed=base/'seed';markets=base/'markets'
            seed.write_text(json.dumps({'categories':[{'id':'c','queries':{'en':['concrete calculator'],'de':['Beton Rechner']}}]}))
            markets.write_text(json.dumps({'markets':{'de':{'lang':'de'}}}))
            args=['play_scan.py','--seed',str(seed),'--markets',str(markets),'--countries','de','--db',str(base/'db')]
            with patch.object(sys,'argv',args),patch.object(play_scan,'search',return_value=[]) as search,patch.object(play_scan.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
                play_scan.main()
            self.assertEqual(search.call_args.args[0],'Beton Rechner')
