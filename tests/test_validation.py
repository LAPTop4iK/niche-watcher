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
    def test_scanner_reuses_its_review_request_and_persists_provenance(self):
        import demand_check as dc
        con=sc.open_db(':memory:');self.make(con)
        result=dc.DemandFetch(('2026-10-30',1,1,100,'OK'),['2026-10-30'],[1],ids=['known-id'],versions=['1.0'])
        with patch.object(dc,'fetch_demand',return_value=result) as fetch:
            display=sc.fetch_recent_reviews(1,'us',con)
        self.assertEqual(fetch.call_count,1)
        self.assertIsNone(display[2])
        self.assertEqual(json.loads(con.execute('SELECT review_ids_json FROM demand').fetchone()[0]),['known-id'])

    def test_review_version_and_release_timing_are_distinct_and_legacy_is_unknown(self):
        import demand_check as dc
        import quality_metrics
        now=datetime(2026,11,1,tzinfo=timezone.utc);con=sc.open_db(':memory:')
        app=self.make(con,version='2026-10-20T00:00:00Z');con.execute("UPDATE snapshots SET version='2.0'")
        self.make(con,tid=2,version='2026-10-31T00:00:00Z');con.execute("UPDATE snapshots SET version='3.0' WHERE track_id=2")
        dates=['2026-10-18','2026-10-21','2026-10-22','2026-10-30','2026-07-31']
        with patch.object(sc,'now_utc',return_value=now):
            dc.prepare(con)
            dc.store_result(con,1,'us',dc.DemandFetch(('2026-10-30',4,4,0,'OK'),dates,[1,1,2,5,1],ids=['a','b','c','d','e'],versions=['1.0','1.0','2.0','2.0','1.0']))
            dc.store_result(con,2,'us',dc.DemandFetch(('2026-10-30',1,1,100,'OK'),['2026-10-30'],[1],ids=['f'],versions=['2.0']))
        con.execute("UPDATE demand SET checked_at='2026-10-30T01:00:00Z' WHERE track_id=2")
        cache=evidence.demand_map(con);row=evidence.latest(con)[0]
        a=evidence.assess(con,row,cache[(1,'us')],now)
        self.assertEqual((a['r7'],a['r30'],a['r90']),(1,4,4))
        self.assertEqual((a['bad_recent_count'],a['bad_after_version_count'],a['bad_current_version_count']),(3,2,1))
        self.assertEqual(a['review_id_known_count'],5)
        queue,_=evidence.research_queue(con,now)
        before=next(r for r in queue if r['track_id']==2)
        self.assertEqual(before['bad_after_version_count'],0)
        self.assertTrue(any('предшествуют' in reason for reason in before['reasons']))
        q=quality_metrics.build(con,now,queue=queue)
        self.assertEqual(q['recent_pain_timing']['only_before_release'],1)
        self.assertEqual(q['recent_pain_timing']['rss_before_release'],1)
        legacy=dict(cache[(1,'us')]);legacy.pop('review_versions_json')
        self.assertIsNone(evidence.assess(con,row,legacy,now)['bad_current_version_count'])

    def test_query_caps_and_failed_latest_attempt_are_not_full_market_coverage(self):
        import quality_metrics
        import demand_check as dc
        con=sc.open_db(':memory:');now=datetime(2026,11,1,tzinfo=timezone.utc)
        job=('us','construction','concrete calculator')
        with patch.object(scan_state,'now',return_value='2026-10-31T00:00:00Z'):
            run=scan_state.start_run(con,[job],{'limit':1})
            self.make(con,run_id=run)
            scan_state.begin_query(con,run,*job);scan_state.end_query(con,run,*job,'OK',1,1);scan_state.finish_run(con,run)
        dc.prepare(con)
        with patch.object(sc,'now_utc',return_value=now):
            dc.store_result(con,1,'us',dc.DemandFetch(('2026-10-30',50,50,100,'OK'),['2026-10-30']*50,[1]*50))
        q=quality_metrics.build(con,now)
        self.assertEqual((q['queries']['fresh_ok'],q['queries']['at_limit']),(1,1))
        self.assertEqual(q['review_coverage']['rss_recent_capped'],1)
        self.assertIsNone(q['cross_geo_review_ids']['shared_ids'])
        with patch.object(scan_state,'now',return_value='2026-10-31T01:00:00Z'):
            failed=scan_state.start_run(con,[job],{'limit':1})
            scan_state.begin_query(con,failed,*job);scan_state.end_query(con,failed,*job,'ERROR');scan_state.finish_run(con,failed)
        q=quality_metrics.build(con,now)
        self.assertEqual((q['queries']['fresh_ok'],q['queries']['error']),(0,1))

    def test_age_threshold_matches_scanner_without_day_rounding(self):
        from datetime import timedelta
        now=datetime(2026,11,1,tzinfo=timezone.utc)
        con=sc.open_db(':memory:');app=self.make(con,version=(now-timedelta(days=547,hours=23)).isoformat())
        with patch.object(sc,'now_utc',return_value=now):a=sc.analyze(con,app,18,500)
        self.assertTrue(a['is_zombie'])
        self.assertTrue(evidence.is_candidate(evidence.latest(con)[0],now))

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
    def test_requested_report_sends_only_its_event_once_and_records_message_id(self):
        import send_report
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';con=sc.open_db(path);self.make(con)
            notifications.enqueue(con,'unrelated','must stay pending');con.close()
            receipt=Path(folder)/'receipt.json'
            response=dict(ok=True,result=dict(message_id=71,date=1793538000))
            with patch.dict('os.environ',{'TG_BOT_TOKEN':'fake','TG_CHAT_ID':'fake'}),patch.object(send_report.tg_setup,'call',return_value=response) as call:
                first=send_report.deliver(path,'requested:one',receipt,True)
                second=send_report.deliver(path,'requested:one',receipt,True)
            self.assertEqual(call.call_count,1)
            self.assertEqual((first['status'],first['messages_sent'],first['telegram_message_id']),('SENT',1,71))
            self.assertEqual(second['messages_sent'],0)
            self.assertEqual(first['payload_sha256'],second['payload_sha256'])
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("SELECT status FROM notification_outbox WHERE event_key='unrelated'").fetchone()[0],'PENDING')

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

    def test_candidate_receipts_cover_legacy_groups_pending_items_and_new_country(self):
        con=sqlite3.connect(':memory:')
        rows=[dict(track_id=i,country='us') for i in range(1,19)]
        for batch in (rows[:12],rows[12:]):
            key='candidates:'+','.join(f"{r['track_id']}:us" for r in batch)
            notifications.enqueue(con,key,'legacy')
        self.assertEqual(notifications.flush(con,lambda p:True),(2,0))
        never=lambda items:self.fail('legacy members already delivered')
        self.assertEqual(notifications.enqueue_candidates(con,list(reversed(rows)),never),0)
        incoming=[*rows,dict(track_id=19,country='us'),dict(track_id=19,country='gb')]
        built=[]
        def build(items):built.extend(items);return 'one summary'
        self.assertEqual(notifications.enqueue_candidates(con,incoming,build),2)
        self.assertEqual({(r['track_id'],r['country']) for r in built},{(19,'us'),(19,'gb')})
        self.assertEqual(notifications.enqueue_candidates(con,list(reversed(incoming)),never),0)
        self.assertEqual(notifications.flush(con,lambda p:True),(1,0))
        self.assertEqual(con.execute('SELECT COUNT(*),SUM(attempts) FROM notification_outbox').fetchone(),(3,3))

    def test_scanner_sends_one_summary_for_18_and_does_not_repeat_on_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed=base/'seed';markets=base/'markets'
            seed.write_text(json.dumps({'categories':[{'id':'c','queries':{'en':['q']}}]}))
            markets.write_text(json.dumps({'markets':{'us':{'lang':'en'}}}))
            args=['scanner.py','--seed',str(seed),'--markets',str(markets),'--countries','us','--db',str(base/'db'),'--report',str(base/'report'),'--skip-reviews','--run-id','same-run','--quiet']
            hits=[dict(trackId=i,trackName=f'App {i}',userRatingCount=600,currentVersionReleaseDate='2020-01-01T00:00:00Z') for i in range(1,19)]
            with patch.object(sys,'argv',args),patch.object(sc,'search_apps',return_value=hits),patch.object(sc,'send_telegram',return_value=True) as send,patch.dict('os.environ',{'TG_BOT_TOKEN':'test','TG_CHAT_ID':'test'}),contextlib.redirect_stdout(io.StringIO()):
                sc.main();sc.main()
            self.assertEqual(send.call_count,1)
            self.assertIn('<b>18</b> app×страна',send.call_args.args[0])
            with sqlite3.connect(base/'db') as con:
                self.assertEqual(con.execute('SELECT COUNT(*),SUM(attempts) FROM notification_outbox').fetchone(),(1,1))
                self.assertEqual(con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],18)

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
    def test_daily_summary_has_one_total_six_examples_and_a_bounded_unicode_size(self):
        con=sc.open_db(':memory:')
        apps=[sc.analyze(con,self.make(con,tid=i),18,500) for i in range(1,19)]
        msg=sc.build_alert_message(apps,562,as_of='2026-11-01T14:08:41Z')
        self.assertIn('<b>18</b> app×страна',msg)
        self.assertIn('562 app×страна',msg);self.assertIn('Показано 6 из 18',msg)
        self.assertEqual(msg.count('\n• '),6);self.assertNotIn('\n1.',msg)
        self.assertIn('01.11.2026 15:08 (Варшава)',msg)
        for a in apps:a['name']='&👩‍🔬'*60;a['store_url']='https://example.com/'+'x'*10000
        msg=sc.build_alert_message(apps,562)
        self.assertLessEqual(len(msg),3800)
        self.assertLessEqual(len(msg.encode('utf-16-le'))//2,3800)
        self.assertEqual(msg.count('Открыть дашборд'),1)

    def test_both_routes_share_evidence_without_excluding_high_rated_recent_apps(self):
        import demand_check as dc
        import tg_digest
        con=sc.open_db(':memory:');now=datetime(2026,11,1,tzinfo=timezone.utc)
        self.make(con,tid=1,name='Old app')
        self.make(con,tid=2,name='Recent pain',version='2026-10-01T00:00:00Z')
        self.make(con,tid=3,name='Not checked',version='2026-10-01T00:00:00Z')
        self.make(con,tid=4,name='Stale RSS',version='2026-10-01T00:00:00Z')
        self.make(con,tid=5,name='No complaint',version='2026-10-01T00:00:00Z')
        dc.prepare(con)
        with patch.object(sc,'now_utc',return_value=now):
            for tid,score in [(2,1),(4,2),(5,5)]:
                dc.store_result(con,tid,'us',dc.DemandFetch(('2026-10-31T00:00:00Z',1,1,100,'OK'),['2026-10-31T00:00:00Z'],[score]))
        con.execute("UPDATE demand SET checked_at='2026-09-01T00:00:00Z' WHERE track_id=4")
        queue,coverage=evidence.research_queue(con,now)
        self.assertEqual({r['track_id']:r['lane'] for r in queue},{1:'stale',2:'recent_pain'})
        self.assertEqual(coverage['search'],dict(eligible=4,rss_usable=2,complaint_records=1))
        # Telegram uses the same qualifying rule; missing/outdated data is not pain.
        with patch.object(evidence,'research_queue',return_value=(queue,coverage)):
            message=tg_digest.build_message(con)
        self.assertIn('Recent pain',message);self.assertNotIn('Stale RSS',message)
        self.assertNotIn('Not checked',message);self.assertNotIn('No complaint',message)

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


class DiscoveryAndReplay(Fixtures):
    def test_review_export_keeps_sources_and_excludes_old_future_and_duplicate_texts(self):
        now=datetime(2026,11,1,tzinfo=timezone.utc)
        base=dict(platform='ios',app_id='1',country='us',source_url='https://itunes.apple.com/us/rss/',
                  score=1,thumbs=0,text='Export loses saved progress')
        rows=[dict(base,review_id='current',date='2026-10-31T01:00:00Z'),
              dict(base,review_id='current',date='2026-10-31T01:00:00Z'),
              dict(base,review_id='too-old',date='2026-08-02T23:00:00Z'),
              dict(base,review_id='future',date='2026-11-02T00:00:00Z')]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reviews.json'
            with patch.object(sys,'argv',['review_clusters.py','--ios','1','--out',str(path)]),patch.object(review_clusters,'fetch_ios',return_value=rows),patch.object(sc,'now_utc',return_value=now),contextlib.redirect_stdout(io.StringIO()):
                review_clusters.main()
            result=json.loads(path.read_text())
            self.assertEqual([r['review_id'] for r in result['reviews']],['current'])
            self.assertEqual(result['reviews'][0]['source_url'],base['source_url'])
            self.assertEqual(result['method'],'lexical; not LLM')

    def test_updated_discovery_respects_country_budget_cooldown_and_refreshes(self):
        import demand_check as dc
        now=datetime(2026,11,1,tzinfo=timezone.utc)
        con=sc.open_db(':memory:')
        for tid,geo,category in [(1,'us','a'),(2,'us','b'),(3,'us','a'),(4,'us','b'),(5,'de','a'),(6,'us','c')]:
            self.make(con,tid=tid,country=geo,category=category,version='2026-09-01T00:00:00Z')
        self.make(con,tid=7,category='a',name='WhatsApp',version='2026-09-01T00:00:00Z')
        cached={(1,'us'):dict(checked_at='2026-10-20T00:00:00Z',fetch_status='OK'),
                (6,'us'):dict(checked_at='2026-10-31T00:00:00Z',fetch_status='OK')}
        selected=dc.updated_review_sample(evidence.latest(con),cached,3,{'us'},now)
        self.assertEqual(len(selected),3);self.assertEqual(len(set(selected)),3)
        self.assertTrue(all(geo=='us' for _,geo in selected))
        self.assertIn((1,'us'),selected);self.assertNotIn((6,'us'),selected);self.assertNotIn((7,'us'),selected)
        self.assertTrue({1,2}.issubset({tid for tid,_ in selected}))
        con.execute("UPDATE snapshots SET version_date='2026-10-31T12:00:00Z' WHERE track_id=6")
        after_update=dc.updated_review_sample(evidence.latest(con),cached,3,{'us'},now)
        self.assertIn((6,'us'),after_update)  # a new release overrides the seven-day cooldown

    def test_clean_replay_uses_original_jobs_after_current_config_expands(self):
        import verify_pipeline
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'reference.db';con=sc.open_db(db)
            jobs=[('us','historical-family','old phrase')]
            run=scan_state.start_run(con,jobs,{'limit':50,'storage_min_ratings':0},'historical-run')
            app=self.make(con,category='historical-family',query='old phrase',run_id=run)
            con.execute('UPDATE snapshots SET rank=1')
            scan_state.begin_query(con,run,*jobs[0]);scan_state.end_query(con,run,*jobs[0],'OK',1,1)
            scan_state.finish_run(con,run);con.close()
            result=verify_pipeline.replay(db,folder)
            self.assertEqual(result['queries'],1);self.assertEqual(result['observations'],1)
            self.assertEqual(result['network_requests'],0)


class SourceAndMath(unittest.TestCase):
    def test_economics_does_not_silently_treat_country_cpi_as_observed_cost(self):
        markets,defaults=rm.load();markets['us']['cpi_usd']=999999
        output=io.StringIO()
        with patch.object(rm,'load',return_value=(markets,defaults)),patch.object(sys,'argv',['revenue_model.py','--model','sub']),contextlib.redirect_stdout(output):
            rm.main()
        self.assertIn('вклад не рассчитан',output.getvalue())
        self.assertNotIn('999999',output.getvalue())

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
