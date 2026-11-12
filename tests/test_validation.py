import contextlib
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
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
        queue,coverage=evidence.research_queue(con,now)
        self.assertFalse(any(r['track_id']==2 for r in queue))
        before=evidence.assess(con,next(r for r in evidence.latest(con) if r['track_id']==2),cache[(2,'us')],now)
        self.assertEqual(before['bad_after_version_count'],0)
        self.assertTrue(any('предшествуют' in reason for reason in before['reasons']))
        q=quality_metrics.build(con,now,queue=queue,coverage=coverage)
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
            dc.store_result(con,1,'us',dc.DemandFetch(('2026-10-30',50,50,100,'OK'),['2026-10-30']*50,[1]*50,capped=True))
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
        self.make(con,category='construction',date='2026-10-20T00:00:00Z')
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

    def test_same_timestamp_conflicting_counters_use_the_freshest(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2026-10-01T00:00:00Z',count=1000)
        self.make(con,date='2026-10-10T00:00:00Z',count=1020)
        self.make(con,date='2026-10-10T00:00:00Z',count=1010)
        self.assertAlmostEqual(evidence.rating_velocity(con,1,'us')[0],20/9*30)

    def test_unjournaled_membership_expires_but_journaled_chart_stays(self):
        con=sc.open_db(':memory:')
        self.make(con,tid=1,date='2026-10-01T00:00:00Z')
        self.make(con,tid=2,category='chart:utilities',query='topfreeapplications',date='2026-10-01T00:00:00Z')
        self.make(con,tid=3,date='2026-10-30T00:00:00Z')
        memberships=evidence.query_memberships(con)
        self.assertEqual({key[0] for key in memberships},{2,3})

    def test_narrower_run_does_not_replace_a_current_wider_serp(self):
        con=sc.open_db(':memory:')
        wide=scan_state.start_run(con,[('us','construction','concrete calculator')],{'limit':50})
        scan_state.begin_query(con,wide,'us','construction','concrete calculator')
        self.make(con,tid=1,run_id=wide,date='2026-10-30T00:00:00Z');self.make(con,tid=2,run_id=wide,date='2026-10-30T00:00:00Z')
        scan_state.end_query(con,wide,'us','construction','concrete calculator','OK',2,2)
        narrow=scan_state.start_run(con,[('us','construction','concrete calculator')],{'limit':25})
        scan_state.begin_query(con,narrow,'us','construction','concrete calculator')
        self.make(con,tid=1,run_id=narrow,date='2026-10-31T00:00:00Z')
        scan_state.end_query(con,narrow,'us','construction','concrete calculator','OK',1,1)
        self.assertEqual(evidence.coverage_map(con)[('us','construction','concrete calculator')],wide)
        self.assertEqual({key[0] for key in evidence.query_memberships(con)},{1,2})

    def test_other_entity_frame_never_replaces_the_software_serp(self):
        con=sc.open_db(':memory:')
        base=scan_state.start_run(con,[('us','construction','concrete calculator')],{'limit':50})
        scan_state.begin_query(con,base,'us','construction','concrete calculator')
        self.make(con,tid=1,run_id=base,date='2026-10-30T00:00:00Z')
        scan_state.end_query(con,base,'us','construction','concrete calculator','OK',1,1)
        other=scan_state.start_run(con,[('us','construction','concrete calculator')],{'limit':50,'entity':'software,iPadSoftware'})
        scan_state.begin_query(con,other,'us','construction','concrete calculator')
        self.make(con,tid=2,run_id=other,date='2026-10-31T00:00:00Z')
        scan_state.end_query(con,other,'us','construction','concrete calculator','OK',1,1)
        self.assertEqual({key[0] for key in evidence.query_memberships(con)},{1})

    def test_reset_before_nearest_baseline_does_not_become_recovery_growth(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2026-09-25T00:00:00Z',count=1000)
        self.make(con,date='2026-10-01T00:00:00Z',count=900)
        self.make(con,date='2026-10-15T00:00:00Z',count=1000)
        self.assertEqual(evidence.rating_velocity(con,1,'us')[0],0.0)

    def test_conflicting_baseline_takes_its_highest_value(self):
        con=sc.open_db(':memory:')
        self.make(con,date='2026-10-01T00:00:00Z',count=1000)
        self.make(con,date='2026-10-01T00:00:00Z',count=900)
        self.make(con,date='2026-10-10T00:00:00Z',count=1020)
        self.assertAlmostEqual(evidence.rating_velocity(con,1,'us')[0],20/9*30)

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
        self.make(con,tid=2,name='Recent pain concrete calculator',version='2026-10-01T00:00:00Z')
        self.make(con,tid=3,name='Not checked',version='2026-10-01T00:00:00Z')
        self.make(con,tid=4,name='Stale RSS',version='2026-10-01T00:00:00Z')
        self.make(con,tid=5,name='No complaint',version='2026-10-01T00:00:00Z')
        dc.prepare(con)
        with patch.object(sc,'now_utc',return_value=now):
            stamps=['2026-10-29T00:00:00Z','2026-10-30T00:00:00Z','2026-10-31T00:00:00Z']
            for tid,score in [(2,1),(4,2),(5,5)]:
                dc.store_result(con,tid,'us',dc.DemandFetch(('2026-10-31T00:00:00Z',3,3,100,'OK'),stamps,[score]*3))
        con.execute("UPDATE demand SET checked_at='2026-09-01T00:00:00Z' WHERE track_id=4")
        queue,coverage=evidence.research_queue(con,now)
        self.assertEqual({r['track_id']:r['lane'] for r in queue},{1:'stale',2:'recent_pain'})
        self.assertEqual(coverage['search'],dict(eligible=4,rss_usable=2,any_complaint=1,complaint_records=1,lost_to_stale_rss=1))
        # Telegram uses the same qualifying rule; missing/outdated data is not pain.
        with patch.object(evidence,'research_queue',return_value=(queue,coverage)):
            message=tg_digest.build_message(con)
        self.assertIn('Recent pain',message);self.assertNotIn('Stale RSS',message)
        self.assertNotIn('Not checked',message);self.assertNotIn('No complaint',message)

    def test_daily_summary_shows_apps_named_after_their_query_first_once_each(self):
        con=sc.open_db(':memory:')
        unrelated=[sc.analyze(con,self.make(con,tid=i,name='Instagram',count=900000),18,500) for i in range(1,8)]
        titled=sc.analyze(con,self.make(con,tid=20,name='Concrete Calculator Pro'),18,500)
        again=sc.analyze(con,self.make(con,tid=20,country='gb',name='Concrete Calculator Pro'),18,500)
        for rank,a in enumerate(unrelated,1):a['rank']=rank
        titled['rank']=again['rank']=30
        msg=sc.build_alert_message([*unrelated,titled,again],9)
        self.assertIn('Со словами запроса в названии: 2',msg)
        self.assertLess(msg.index('Concrete Calculator Pro'),msg.index('Instagram'))
        self.assertEqual(msg.count('Concrete Calculator Pro'),1)

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


class QueueSignals(Fixtures):
    def reviewed(self,con,tid,scores,versions=None,themes=None,version='2.0',name='Concrete Calculator',category='construction'):
        import demand_check as dc
        self.make(con,tid=tid,version='2026-10-01T00:00:00Z',name=name,category=category);con.execute('UPDATE snapshots SET version=? WHERE track_id=?',(version,tid))
        dates=[f'2026-10-{10+i:02d}T00:00:00Z' for i in range(len(scores))]
        now=datetime(2026,11,1,tzinfo=timezone.utc)
        with patch.object(sc,'now_utc',return_value=now):
            dc.prepare(con)
            dc.store_result(con,tid,'us',dc.DemandFetch((dates[-1],len(dates),len(dates),0,'OK'),dates,scores,
                ids=[f'{tid}-{i}' for i in range(len(dates))],versions=versions or ['2.0']*len(dates),themes=themes))
        return now

    def test_pain_needs_three_reviews_on_the_release_above_the_category_base(self):
        con=sc.open_db(':memory:')
        self.reviewed(con,1,[1,1,1,5,5,5],themes={'crash':[0,1,2]})
        self.reviewed(con,2,[1,1,5,5,5,5,5,5,5,5])
        self.reviewed(con,3,[1]*4+[5]*16)
        now=self.reviewed(con,4,[1,1,1,5],versions=['1.0','1.0','1.0','2.0'])
        queue,coverage=evidence.research_queue(con,now)
        self.assertEqual([r['track_id'] for r in queue],[1])
        self.assertGreater(queue[0]['pain_excess'],0)
        self.assertEqual(queue[0]['pain_theme'],'crash')
        self.assertEqual(coverage['search']['any_complaint'],4)

    def test_pain_from_search_needs_the_query_in_the_title(self):
        con=sc.open_db(':memory:')
        self.reviewed(con,9,[5]*20)
        self.reviewed(con,1,[1,1,1,5],name='Instagram')
        self.reviewed(con,2,[1,1,1,5],name='Concrete Calculator Pro')
        now=self.reviewed(con,3,[1,1,1,5],name='Chipotle',category='chart:food & drink')
        # A chart membership keeps the row in its own source; the unrelated search hit is not counted.
        self.make(con,tid=3,version='2026-10-01T00:00:00Z',name='Chipotle');con.execute("UPDATE snapshots SET version='2.0'")
        queue,coverage=evidence.research_queue(con,now)
        self.assertEqual({r['track_id']:r['sources'] for r in queue},{2:['search'],3:['charts']})
        self.assertEqual((coverage['search']['complaint_records'],coverage['charts']['complaint_records']),(1,1))

    def test_apple_own_apps_never_enter_the_queue(self):
        con=sc.open_db(':memory:')
        self.reviewed(con,9,[5]*20)
        self.reviewed(con,1,[1,1,1,5])
        self.make(con,tid=2,name='Concrete Calculator Remote')
        self.make(con,tid=3,name='Applebee’s Concrete Calculator')
        now=self.reviewed(con,4,[1,1,1,5],name='Concrete Calculator Watch')
        con.execute("UPDATE snapshots SET seller_name='Apple Distribution International' WHERE track_id IN (2,4)")
        con.execute("UPDATE snapshots SET seller_name='Applebee''s Holdings, LLC' WHERE track_id=3")
        self.assertEqual(sorted(r['track_id'] for r in evidence.research_queue(con,now)[0]),[1,3])

    def test_pain_with_captured_text_needs_a_repeated_theme(self):
        con=sc.open_db(':memory:')
        now=self.reviewed(con,1,[1,1,1,5],themes={})
        self.assertEqual(evidence.research_queue(con,now)[0],[])

    def test_review_themes_keep_repeated_words_of_low_ratings_only(self):
        import demand_check as dc
        themes=dc.review_themes(['sync keeps failing','sync lost data','sync is great','failing again'],[1,2,5,1])
        self.assertEqual(themes,{'failing':[0,3],'sync':[0,1]})

    def test_queue_shows_best_position_and_sorts_by_it(self):
        con=sc.open_db(':memory:')
        jobs=[('us','construction','concrete calculator'),('us','construction','slab')]
        run=scan_state.start_run(con,jobs,{'limit':50})
        for query,rank in (('concrete calculator',30),('slab',4)):
            scan_state.begin_query(con,run,'us','construction',query)
            app=self.make(con,tid=1,query=query,run_id=run);con.execute('UPDATE snapshots SET rank=? WHERE query=?',(rank,query))
            scan_state.end_query(con,run,'us','construction',query,'OK',1,1)
        self.assertEqual(evidence.best_positions(con)[(1,'us')],(4,'slab'))
        queue,_=evidence.research_queue(con,datetime(2026,11,1,tzinfo=timezone.utc))
        self.assertEqual((queue[0]['best_rank'],queue[0]['best_query']),(4,'slab'))

    def test_likely_truncated_counts_short_answers_below_the_limit(self):
        import quality_metrics
        con=sc.open_db(':memory:');markets=json.loads((Path(__file__).resolve().parents[1]/'markets.json').read_text())['markets']
        seed=sc.load_seed(Path(__file__).resolve().parents[1]/'seed_queries.json');jobs=sc.planned_jobs(seed,markets,['us'])[:2]
        run=scan_state.start_run(con,jobs,{'limit':50})
        for job,count in zip(jobs,(38,12)):
            scan_state.begin_query(con,run,*job);scan_state.end_query(con,run,*job,'OK',count,0)
        q=quality_metrics.build(con,datetime.now(timezone.utc))['queries']
        self.assertEqual((q['at_limit'],q['likely_truncated']),(0,1))

    def test_next_slot_takes_the_group_scanned_longest_ago(self):
        con=sc.open_db(':memory:')
        markets=dict(us=dict(tier=1),fr=dict(tier=2,week_slot=1),no=dict(tier=2,week_slot=2),sa=dict(tier=2,week_slot=3),kr=dict(tier=2,week_slot=4))
        self.assertEqual(sc.next_slot(con,markets),1)
        def scan(geo,stamp,source='itunes-search'):
            run=scan_state.start_run(con,[(geo,'c','q')],dict(source=source))
            scan_state.begin_query(con,run,geo,'c','q');scan_state.end_query(con,run,geo,'c','q','OK',1,1)
            con.execute('UPDATE scan_queries SET finished_at=? WHERE run_id=?',(stamp,run))
        scan('us','2026-11-02T18:00:00Z');scan('fr','2026-11-02T18:00:00Z')
        self.assertEqual(sc.next_slot(con,markets),2)
        for geo in ('no','sa','kr'):scan(geo,'2026-11-01T01:00:00Z')
        # A run that starts a day late takes the group it missed instead of the weekday's.
        scan('no','2026-11-03T18:00:00Z')
        # A chart pass over a group is not its search scan.
        scan('sa','2026-11-04T10:00:00Z','apple-charts')
        self.assertEqual(sc.next_slot(con,markets),3)

    def test_title_hint_finds_cjk_words_without_spaces(self):
        self.assertTrue(relevant('燃費記録アプリ','燃費 記録'))

    def test_query_language_is_recorded_for_fallback_jobs(self):
        seed=dict(categories=[dict(id='c',queries=dict(en=['tax'],de=['steuer']))])
        markets=dict(de=dict(lang='de'),fr=dict(lang='fr'))
        frames=list(sc.job_frames(seed,markets,sc.planned_jobs(seed,markets,['de','fr'])))
        self.assertEqual(frames,[('de','c','steuer','de','de'),('fr','c','tax','en','fr')])


class GeoGap(unittest.TestCase):
    lang={'us':'en','gb':'en','nz':'en','de':'de','at':'de'}

    def row(self,tid,geo,rank,count,query='concrete calculator',name='Concrete Calculator'):
        return (tid,geo,'c',query,rank,count,name)

    def test_same_app_in_a_smaller_market_is_not_a_gap(self):
        import geo_gap
        rows=[self.row(1,'us',1,5000),self.row(1,'nz',1,100)]
        self.assertEqual(geo_gap.find_gaps(rows,self.lang),[])

    def test_real_absence_is_reported_after_scaling(self):
        import geo_gap
        rows=[self.row(1,'us',1,5000),self.row(2,'gb',1,50),
              self.row(3,'us',1,6000,query='slab tool',name='Slab Tool'),self.row(4,'gb',1,6000,query='slab tool',name='Slab Tool')]
        gaps=geo_gap.find_gaps(rows,self.lang)
        self.assertEqual([(g['leader'][0],g['poor']) for g in gaps],[(1,{'gb':50})])

    def test_strong_top_ten_without_title_match_closes_the_gap(self):
        import geo_gap
        rows=[self.row(1,'us',1,5000),self.row(2,'gb',3,900,name='Builder Toolkit')]
        self.assertEqual(geo_gap.find_gaps(rows,self.lang),[])

    def test_market_size_explains_a_small_counter(self):
        import geo_gap
        rows=[self.row(1,'us',1,5000),self.row(2,'nz',1,100),
              self.row(3,'us',1,10000,query='slab tool',name='Slab Tool'),self.row(4,'nz',1,250,query='slab tool',name='Slab Tool')]
        self.assertEqual(geo_gap.find_gaps(rows,self.lang,max_poor=200),[])

    def test_fallback_query_is_not_compared_between_countries(self):
        import geo_gap
        con=sc.open_db(':memory:')
        scan_state.record_frames(con,[('de','c','tax','en','de'),('at','c','steuer','de','de')])
        rows=[(1,'de','c','tax',1,10,'x'),(2,'at','c','steuer',1,10,'y')]
        self.assertEqual(list(geo_gap.localized(con,rows,dict(categories=[]),self.lang)),[rows[1]])


class ReviewCollection(Fixtures):
    def page(self,count,start=0,stamp='2026-10-30T00:00:00Z'):
        return {'feed':{'entry':[{'id':{'label':f'r{i}'},'im:rating':{'label':'1'},'im:version':{'label':'2.0'},
            'updated':{'label':stamp},'content':{'label':'crash on start'}} for i in range(start,start+count)]}}

    def fetch(self,responses,**kwargs):
        now=datetime(2026,10,31,tzinfo=timezone.utc)
        with patch.object(sc,'fetch_json',side_effect=responses) as mocked,patch.object(sc,'now_utc',return_value=now):
            import demand_check
            return demand_check.fetch_demand(1,'us',pause=0,**kwargs),mocked.call_count

    def test_next_page_only_while_a_whole_page_stays_inside_the_window(self):
        result,calls=self.fetch([self.page(50),self.page(20,50)])
        self.assertEqual((len(result.dates),calls,result.capped),(70,2,False))
        result,calls=self.fetch([self.page(50,stamp='2026-01-01T00:00:00Z')])
        self.assertEqual((len(result.dates),calls,result.capped),(50,1,False))

    def test_empty_page_is_skipped_and_flagged_not_treated_as_the_end(self):
        result,calls=self.fetch([self.page(50),self.page(0),self.page(10,100)])
        self.assertEqual((len(result.dates),calls,result.capped),(60,3,True))

    def test_page_cap_is_flagged_as_a_lower_bound(self):
        result,calls=self.fetch([self.page(50,i*50) for i in range(3)],pages=3)
        self.assertEqual((len(result.dates),calls,result.capped),(150,3,True))

    def test_failed_later_page_keeps_the_sample_and_flags_it(self):
        result,_=self.fetch([self.page(50),RuntimeError('offline')])
        self.assertEqual((result[-1],len(result.dates),result.capped),('OK',50,True))

    def test_empty_is_confirmed_only_by_a_second_attempt_ten_minutes_later(self):
        import demand_check
        con=sc.open_db(':memory:');demand_check.prepare(con)
        empty=demand_check.DemandFetch((None,None,None,None,'EMPTY'))
        first=datetime(2026,10,31,tzinfo=timezone.utc);soon=first.replace(minute=5);later=first.replace(minute=16)
        status=lambda:con.execute('SELECT fetch_status FROM demand').fetchone()[0]
        with patch.object(sc,'now_utc',return_value=first):demand_check.store_result(con,1,'us',empty)
        self.assertEqual(status(),'EMPTY_UNCONFIRMED')
        with patch.object(sc,'now_utc',return_value=soon):demand_check.store_result(con,1,'us',empty)
        self.assertEqual(status(),'EMPTY_UNCONFIRMED')
        with patch.object(sc,'now_utc',return_value=later):demand_check.store_result(con,1,'us',empty)
        self.assertEqual(status(),'EMPTY')

    def test_failed_refresh_keeps_a_valid_sample_and_goes_to_history(self):
        import demand_check as dc
        con=sc.open_db(':memory:');dc.prepare(con)
        good=dc.DemandFetch(('2026-10-30T00:00:00Z',1,1,100,'OK'),['2026-10-30T00:00:00Z'],[1],ids=['a'],versions=['1'])
        first=datetime(2026,10,31,tzinfo=timezone.utc);later=datetime(2026,11,5,tzinfo=timezone.utc);expired=datetime(2026,11,20,tzinfo=timezone.utc)
        row=lambda:con.execute('SELECT fetch_status,r90,checked_at FROM demand').fetchone()
        with patch.object(sc,'now_utc',return_value=first):dc.store_result(con,1,'us',good)
        kept=row()
        for failure in (dc.DemandFetch((None,None,None,None,'ERROR'),error='offline'),dc.DemandFetch((None,None,None,None,'EMPTY'))):
            with patch.object(sc,'now_utc',return_value=later):dc.store_result(con,1,'us',failure)
            self.assertEqual(row(),kept)
        statuses=[json.loads(r[0]).get('fetch_status') for r in con.execute('SELECT payload_json FROM demand_samples')]
        # An app that had reviews never answers empty honestly: the answer is suspect.
        self.assertEqual(sorted(statuses),['EMPTY_SUSPECT','ERROR','OK'])
        import validate_data
        self.assertEqual(validate_data.validate(con)[1],[])
        with patch.object(sc,'now_utc',return_value=expired):dc.store_result(con,1,'us',dc.DemandFetch((None,None,None,None,'ERROR'),error='offline'))
        self.assertEqual(row()[0],'ERROR')

    def test_capped_sample_with_a_duplicate_across_pages_is_still_a_lower_bound(self):
        import demand_check as dc
        now=datetime(2026,11,1,tzinfo=timezone.utc);con=sc.open_db(':memory:');dc.prepare(con)
        row=self.make(con,version='2026-01-01T00:00:00Z')
        dates=[f'2026-10-{1+i%28:02d}T00:00:00Z' for i in range(249)]
        with patch.object(sc,'now_utc',return_value=now):
            dc.store_result(con,1,'us',dc.DemandFetch((dates[-1],249,249,0,'OK'),dates,[5]*249,capped=True))
        cached=evidence.demand_map(con)[(1,'us')]
        self.assertTrue(evidence.assess(con,evidence.latest(con)[0],cached,now)['recent_sample_capped'])

    def test_velocity_works_on_a_table_without_run_id(self):
        con=sqlite3.connect(':memory:')
        con.execute('CREATE TABLE snapshots(track_id INTEGER,country TEXT,scanned_at TEXT,rating_count INTEGER)')
        con.executemany('INSERT INTO snapshots VALUES (1,?,?,?)',[('us','2026-10-01T00:00:00Z',1000),('us','2026-10-10T00:00:00Z',1100)])
        self.assertAlmostEqual(evidence.rating_velocity(con,1,'us')[0],100/9*30)

    def test_page_links_report_defaults_to_one_file_per_storefront(self):
        import similar_crawl
        with tempfile.TemporaryDirectory() as folder:
            db=str(Path(folder)/'db');cwd=Path.cwd()
            try:
                import os
                os.chdir(folder)
                with patch.object(sys,'argv',['similar_crawl.py','--seeds','1','--country','gb','--db',db]),patch.object(similar_crawl,'page_ids',return_value=[]),patch.object(sc,'REQUEST_DELAY',0),patch.object(similar_crawl.time,'sleep'):
                    similar_crawl.main()
                self.assertTrue((Path(folder)/'clusters_report_gb.md').exists())
            finally:
                os.chdir(cwd)

    def test_empty_feed_of_an_app_that_had_reviews_is_never_a_measurement(self):
        import demand_check as dc
        con=sc.open_db(':memory:');dc.prepare(con)
        good=dc.DemandFetch(('2026-10-30T00:00:00Z',1,1,100,'OK'),['2026-10-30T00:00:00Z'],[1],ids=['a'],versions=['1'])
        with patch.object(sc,'now_utc',return_value=datetime(2026,10,31,tzinfo=timezone.utc)):dc.store_result(con,1,'us',good)
        # Past the 14-day window an empty answer used to replace the sample.
        with patch.object(sc,'now_utc',return_value=datetime(2026,11,20,tzinfo=timezone.utc)):
            dc.store_result(con,1,'us',dc.DemandFetch((None,None,None,None,'EMPTY')))
            dc.store_result(con,2,'us',dc.DemandFetch((None,None,None,None,'EMPTY')))
        self.assertEqual(con.execute('SELECT fetch_status FROM demand WHERE track_id=1').fetchone(),('OK',))
        self.assertEqual(con.execute('SELECT fetch_status FROM demand WHERE track_id=2').fetchone(),('EMPTY_UNCONFIRMED',))
        statuses=[json.loads(r[0])['fetch_status'] for r in con.execute('SELECT payload_json FROM demand_samples WHERE track_id=1 ORDER BY checked_at')]
        self.assertEqual(statuses,['OK','EMPTY_SUSPECT'])

    def test_measurement_stops_when_apps_that_had_reviews_answer_empty(self):
        import demand_check as dc
        con=sc.open_db(':memory:');dc.prepare(con)
        good=dc.DemandFetch(('2026-10-30T00:00:00Z',1,1,100,'OK'),['2026-10-30T00:00:00Z'],[5],ids=['a'],versions=['1'])
        now=datetime(2026,11,12,tzinfo=timezone.utc)
        with patch.object(sc,'now_utc',return_value=now):
            for tid in range(1,13):dc.store_result(con,tid,'us',good)
            empty=lambda *a,**k:dc.DemandFetch((None,None,None,None,'EMPTY'))
            # New apps without reviews answer empty honestly and never trip the guard.
            with patch.object(dc,'fetch_demand',side_effect=empty) as fetched,patch.object(dc.time,'sleep') as slept:
                dc.measure(con,[(tid,'us') for tid in range(100,120)],0)
                self.assertEqual(fetched.call_count,20)
                with self.assertRaises(dc.Throttled):
                    dc.measure(con,[(1,'us'),(200,'us'),*[(tid,'us') for tid in range(2,13)]],0)
        # Five empty canaries pause once; five more after the pause stop the lane.
        self.assertEqual(fetched.call_count,31)
        self.assertIn(unittest.mock.call(dc.THROTTLE_PAUSE),slept.call_args_list)
        self.assertEqual({r[0] for r in con.execute('SELECT fetch_status FROM demand WHERE track_id<13')},{'OK'})
        # The stopped run's other empty answers are not kept as measurements.
        self.assertIsNone(con.execute('SELECT 1 FROM demand WHERE track_id=200').fetchone())
        self.assertIsNotNone(con.execute('SELECT 1 FROM demand WHERE track_id=100').fetchone())

    def test_queue_refresh_takes_complaints_first_and_oldest_within_budget(self):
        import demand_check
        now=datetime(2026,10,31,tzinfo=timezone.utc)
        row=lambda tid,version='2026-06-01T00:00:00Z':dict(track_id=tid,country='us',rating_count=1000,categories=['c'],version_date=version)
        cached={(1,'us'):dict(checked_at='2026-10-20T00:00:00Z',review_scores_json='[5]'),
                (2,'us'):dict(checked_at='2026-10-22T00:00:00Z',review_scores_json='[1]'),
                (3,'us'):dict(checked_at='2026-10-20T00:00:00Z',review_scores_json='[2]'),
                (4,'us'):dict(checked_at='2026-10-29T00:00:00Z',review_scores_json='[1]'),
                (5,'us'):dict(checked_at='2026-10-01T00:00:00Z',review_scores_json='[5]')}
        rows=[row(1),row(2),row(3),row(4),row(5,'2020-01-01T00:00:00Z')]
        self.assertEqual(demand_check.queue_refresh_sample(rows,cached,3,None,now),[(3,'us'),(2,'us'),(5,'us')])
        self.assertEqual(demand_check.queue_refresh_sample(rows,cached,1,None,now,queued=[(2,'us')]),[(2,'us')])

    def test_queue_refresh_measures_unchecked_old_versions_before_rechecks(self):
        import demand_check
        now=datetime(2026,10,31,tzinfo=timezone.utc)
        row=lambda tid,version='2020-01-01T00:00:00Z',**extra:dict(track_id=tid,country='us',rating_count=1000,categories=['c'],version_date=version,**extra)
        cached={(1,'us'):dict(checked_at='2026-10-01T00:00:00Z',review_scores_json='[1]'),
                (2,'us'):dict(checked_at='2026-10-20T00:00:00Z',review_scores_json='[1]')}
        rows=[row(1),row(2,'2026-06-01T00:00:00Z'),row(3),row(4,'2026-06-01T00:00:00Z'),row(5,seller='Apple Inc.'),
              dict(row(6),categories=['chart:music'])]
        # Recent-version lane first, then old versions never measured, then other rechecks.
        self.assertEqual(demand_check.queue_refresh_sample(rows,cached,3,None,now,queued=[(2,'us')]),[(2,'us'),(3,'us'),(1,'us')])

    def test_chart_keeps_resolved_apps_when_one_id_is_delisted(self):
        with tempfile.TemporaryDirectory() as folder:
            db=str(Path(folder)/'db');out=str(Path(folder)/'r.md')
            with patch.object(charts_scan,'GENRES',{6002:'Utilities'}),patch.object(charts_scan,'FEEDS',['toppaidapplications']),\
                 patch.object(charts_scan,'fetch_rss_chart',return_value=[1,2]),patch.object(charts_scan,'lookup_ids',return_value=[dict(trackId=1,trackName='A',userRatingCount=10,currentVersionReleaseDate='2024-01-01T00:00:00Z')]),\
                 patch.object(sys,'argv',['charts_scan.py','--countries','us','--db',db,'--out',out]),patch.object(sc,'REQUEST_DELAY',0),patch.object(charts_scan.time,'sleep'):
                charts_scan.main()
            with sqlite3.connect(db) as con:
                row=con.execute('SELECT status,result_count,stored_count,error FROM scan_queries').fetchone()
            self.assertEqual(row,('OK',2,1,'1 unresolved IDs'))


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

    def test_vat_comes_out_before_the_commission(self):
        self.assertAlmostEqual(rm.net_price(4.99,.19,.15),3.564,places=3)
        markets,d=rm.load()
        self.assertAlmostEqual(markets['de']['vat_rate'],.19)
        self.assertLess(rm.ltv_payer(markets['de'],d),rm.ltv_payer(markets['us'],d)/1.15)

    def test_monthly_curve_steps_at_the_first_renewal(self):
        markets,d=rm.load();plan=d['plans']['monthly']
        self.assertAlmostEqual(rm.survival(plan,1),.57)
        self.assertAlmostEqual(rm.survival(plan,12),.10,places=2)
        payments=sum(rm.survival(plan,cycle) for cycle in range(12))
        self.assertAlmostEqual(payments,4.2,places=1)

    def test_payer_ltv_on_priors_stays_inside_the_published_range(self):
        markets,d=rm.load()
        self.assertTrue(9<=rm.ltv_payer(markets['us'],d)<=37)
        self.assertLess(rm.ltv_payer(markets['us'],d,'standard'),rm.ltv_payer(markets['us'],d))

    def test_commission_program_regions_and_renewals(self):
        markets,_=rm.load()
        self.assertEqual(rm.fee(markets['us'],'small-business'),.15)
        self.assertEqual(rm.fee(markets['us'],'standard'),.30)
        self.assertEqual(rm.fee(markets['de'],'standard'),.26)
        self.assertEqual(rm.fee(markets['us'],'standard',12),.15)
        with self.assertRaises(ValueError):rm.fee(markets['us'],'other')

    def test_acquisition_formulas_reproduce_the_plan(self):
        self.assertAlmostEqual(rm.cac(1,.40,.10),25)
        self.assertAlmostEqual(rm.cac(1.23,.632,.024),81.1,places=1)
        self.assertAlmostEqual(rm.break_even_cpt(19,.632,.024),.288,places=3)
        self.assertAlmostEqual(rm.searches_required(1000,.284,.6,.024,19),12870,delta=5)
        self.assertAlmostEqual(rm.payers_max(100000,.1,.106,.632,.024),16.1,places=1)

    def test_monte_carlo_is_seeded_and_medians_match_the_closed_form(self):
        first=rm.monte_carlo(rm.paid_contribution);second=rm.monte_carlo(rm.paid_contribution)
        self.assertEqual(first,second)
        self.assertLess(first['p10'],first['p50']);self.assertLess(first['p50'],first['p90'])
        self.assertLess(first['positive_share'],.5)
        self.assertAlmostEqual(rm.monte_carlo(lambda **values:values['cpt'])['p50'],1.23,delta=.05)

    def test_sensitivity_ranks_what_to_measure_first(self):
        ranked=rm.sensitivity(rm.paid_contribution)
        self.assertEqual(len(ranked),len(rm.PRIORS));self.assertEqual(ranked[0][0],'cpt')
        self.assertGreaterEqual(ranked[0][1],ranked[-1][1])

    def test_retention_interpolation_is_convex_and_lower_than_linear(self):
        _,d=rm.load()
        linear=1+d['d1_retention']+sum(d['d1_retention']+(d['d7_retention']-d['d1_retention'])*(day-1)/6 for day in range(2,8))+\
            sum(d['d7_retention']+(d['d30_retention']-d['d7_retention'])*(day-7)/23 for day in range(8,31))
        self.assertLess(rm.active_days(d),linear)
        self.assertAlmostEqual(rm.retention(d,7),d['d7_retention'])
        self.assertAlmostEqual(rm.retention(d,30),d['d30_retention'])
        self.assertLess(rm.retention(d,90),d['d30_retention'])

    def test_hybrid_uses_one_horizon_for_both_parts(self):
        markets,d=rm.load();mk=markets['us']
        hybrid=rm.model_revenue('us',mk,d,'hybrid',1,None)
        self.assertAlmostEqual(hybrid,.5*rm.rpd_ads(mk,d)+.5*rm.ltv_sub(mk,d))
        self.assertGreater(rm.rpd_ads(mk,d),rm.rpd_ads(mk,d,30))

    def test_requirements_report_prints_cac_and_required_searches(self):
        output=io.StringIO()
        with patch.object(sys,'argv',['revenue_model.py','--model','sub','--requirements','--rank','3']),contextlib.redirect_stdout(output):
            rm.main()
        for text in ('CAC $81','безубыточный CPT','Что измерить первым'):
            self.assertIn(text,output.getvalue())

    def test_requirements_scale_the_payer_value_to_the_storefront(self):
        markets,d=rm.load()
        priors=rm.market_priors(markets['de'],d,'small-business')
        self.assertLess(priors['ltv_payer'][1],rm.PRIORS['ltv_payer'][1])
        self.assertEqual(priors['cpt'],rm.PRIORS['cpt'])

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
