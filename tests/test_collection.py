"""Guard retained evidence, historical time alignment and exploratory selection."""
from datetime import datetime, timezone
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import db_merge
import demand_check as dc
import demand_history as history
import evidence
import raw_archive
import scanner as sc
import validate_data

NOW=datetime(2026,11,1,18,tzinfo=timezone.utc)


def snapshot(con,tid=1,stamp='2026-10-31T00:00:00Z'):
    app=sc.normalize(dict(trackId=tid,trackName='Test',userRatingCount=500,
        currentVersionReleaseDate='2020-01-01T00:00:00Z'),'us','test','test')
    app['scanned_at']=stamp
    sc.upsert_app(con,app);sc.insert_snapshot(con,app);con.commit()


def measurement(score=5):
    return dc.DemandFetch(('2026-10-31T00:00:00Z',1,1,int(score<=2)*100,'OK'),
        ['2026-10-31T00:00:00Z'],[score],ids=['r1'],versions=['1.0'])


class HistoryTests(unittest.TestCase):
    def test_refresh_preserves_old_measurement_and_original_time(self):
        con=sc.open_db(':memory:');snapshot(con);dc.prepare(con)
        with patch.object(sc,'now_utc',return_value=NOW):dc.store_result(con,1,'us',measurement(1),'wide-v1:small')
        original=con.execute('SELECT payload_json,snapshot_json FROM demand_samples').fetchone()
        with patch.object(sc,'now_utc',return_value=datetime(2026,11,2,tzinfo=timezone.utc)):
            dc.store_result(con,1,'us',dc.DemandFetch((None,None,None,None,'ERROR'),error='offline'))
        rows=con.execute('SELECT payload_json FROM demand_samples ORDER BY checked_at').fetchall()
        self.assertEqual(len(rows),2);self.assertEqual(rows[0][0],original[0])
        self.assertEqual(json.loads(rows[0][0])['collection_policy'],'wide-v1:small')
        self.assertEqual(json.loads(rows[1][0])['fetch_status'],'ERROR')
        self.assertEqual(con.execute('SELECT fetch_status FROM demand').fetchone()[0],'OK')
        self.assertEqual(json.loads(original[1])['scanned_at'],'2026-10-31T00:00:00Z')
        con.close()

    def test_history_failure_rolls_back_cache_overwrite(self):
        con=sc.open_db(':memory:');snapshot(con);dc.prepare(con)
        with patch.object(sc,'now_utc',return_value=NOW):dc.store_result(con,1,'us',measurement())
        old=con.execute('SELECT * FROM demand').fetchone()
        with patch.object(history,'append',side_effect=[0,RuntimeError('disk full')]):
            with self.assertRaisesRegex(RuntimeError,'disk full'):dc.store_result(con,1,'us',measurement(1))
        self.assertEqual(con.execute('SELECT * FROM demand').fetchone(),old)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM demand_samples').fetchone()[0],1)
        con.close()

    def test_validation_rejects_future_context_and_changed_measurement(self):
        con=sc.open_db(':memory:');snapshot(con);dc.prepare(con)
        with patch.object(sc,'now_utc',return_value=NOW):dc.store_result(con,1,'us',measurement())
        self.assertFalse(validate_data.validate(con)[1])
        con.execute("UPDATE demand_samples SET snapshot_json=?",(json.dumps(dict(track_id=1,country='us',scanned_at='2026-12-01')),))
        self.assertTrue(any('future' in e for e in validate_data.validate(con)[1]))
        con.execute("UPDATE demand_samples SET snapshot_json=NULL,payload_json=?",(json.dumps(dict(track_id=1,country='us',checked_at='2026-11-01',r90=999)),))
        self.assertTrue(any('identity' in e for e in validate_data.validate(con)[1]));con.close()

    def test_legacy_import_never_attaches_future_snapshot_or_invents_text(self):
        con=sc.open_db(':memory:');snapshot(con);dc.prepare(con)
        con.execute("INSERT INTO demand (track_id,country,checked_at,fetch_status) VALUES (1,'us','2026-10-30','EMPTY')")
        self.assertEqual(history.seed_existing(con),1);self.assertEqual(history.seed_existing(con),0)
        payload,context=con.execute('SELECT payload_json,snapshot_json FROM demand_samples').fetchone()
        self.assertEqual(json.loads(payload)['checked_at'],'2026-10-30');self.assertIsNone(context)
        self.assertNotIn('review_texts',json.loads(payload));con.close()

    def test_merge_preserves_both_legacy_caches_and_latest_value(self):
        with tempfile.TemporaryDirectory() as folder:
            paths=[Path(folder)/name for name in ('a.db','b.db')]
            for path,stamp in zip(paths,['2026-10-30','2026-11-01']):
                con=sc.open_db(path);snapshot(con,stamp='2026-10-29');dc.prepare(con)
                con.execute("INSERT INTO demand (track_id,country,checked_at,fetch_status) VALUES (1,'us',?,'EMPTY')",(stamp,))
                con.commit();con.close()
            db_merge.merge(*paths);db_merge.merge(*paths)
            with sqlite3.connect(paths[0]) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM demand_samples').fetchone()[0],2)
                self.assertEqual(con.execute('SELECT checked_at FROM demand').fetchone()[0],'2026-11-01')

    def test_merge_keeps_source_raw_reference_before_importing_its_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'target.db';source=Path(folder)/'source.db'
            con=sc.open_db(target);con.close()
            con=sc.open_db(source);snapshot(con);dc.prepare(con)
            result=measurement();result.raw_ref={'event_id':'real-event','sha256':'body-hash'}
            with patch.object(sc,'now_utc',return_value=NOW):dc.store_result(con,1,'us',result)
            con.close();db_merge.merge(target,source)
            with sqlite3.connect(target) as con:
                ref=json.loads(con.execute('SELECT raw_ref_json FROM demand_samples').fetchone()[0])
                self.assertEqual(ref,result.raw_ref)


class ArchiveTests(unittest.TestCase):
    def test_exact_body_deduplicated_but_fetch_events_and_safe_headers_retained(self):
        body=b'{"description":"full text", "unknownFutureField": [1, 2]}'
        with tempfile.TemporaryDirectory() as folder:
            first=raw_archive.capture('https://itunes.apple.com/search?term=cards',body,headers={'Date':'today','Set-Cookie':'secret'},directory=folder)
            second=raw_archive.capture('https://itunes.apple.com/search?term=cards',body,directory=folder)
            self.assertNotEqual(first['event_id'],second['event_id']);self.assertEqual(first['sha256'],second['sha256'])
            self.assertEqual(gzip.decompress((Path(folder)/first['object']).read_bytes()),body)
            self.assertEqual(raw_archive.stats(folder)['objects'],1);self.assertEqual(raw_archive.stats(folder)['fetches'],2)
            with sqlite3.connect(Path(folder)/'index.sqlite3') as con:
                self.assertEqual(json.loads(con.execute('SELECT headers_json FROM fetches WHERE event_id=?',(first['event_id'],)).fetchone()[0]),{'date':'today'})

    def test_storage_failure_stops_http_without_retry(self):
        response=io.BytesIO(b'{}');response.status=200;response.headers={}
        with patch.dict(os.environ,{raw_archive.ENV_DIR:'/unused'}),patch.object(sc.urllib.request,'urlopen',return_value=response) as request,patch.object(raw_archive,'capture',side_effect=raw_archive.ArchiveError('full')),patch.object(sc.time,'sleep') as sleep:
            with self.assertRaises(raw_archive.ArchiveError):sc.fetch_json('https://itunes.apple.com/search')
            self.assertEqual(request.call_count,1);sleep.assert_not_called()

    def test_capacity_limit_preserves_existing_objects(self):
        with tempfile.TemporaryDirectory() as folder:
            ref=raw_archive.capture('https://itunes.apple.com/search',b'first',directory=folder)
            with self.assertRaises(raw_archive.ArchiveError):raw_archive.capture('https://itunes.apple.com/search',b'second',directory=folder,max_bytes=1)
            self.assertEqual(raw_archive.stats(folder)['fetches'],1)
            self.assertEqual(gzip.decompress((Path(folder)/ref['object']).read_bytes()),b'first')

    def test_public_endpoint_allowlist_excludes_telegram_and_secrets(self):
        for url in ('https://api.telegram.org/botSECRET/sendMessage','https://itunes.apple.com/search?api_key=secret'):
            with self.assertRaises(raw_archive.ArchiveError):raw_archive.public_url(url)

    def test_http_capture_reference_survives_rss_parse(self):
        body=json.dumps({'feed':{'entry':[{'id':{'label':'r1'},'im:rating':{'label':'1'},'im:version':{'label':'2.0'},'updated':{'label':'2026-10-31'},'content':{'label':'Full complaint text'}}]}}).encode()
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{raw_archive.ENV_DIR:folder}):
            response=io.BytesIO(body);response.status=200;response.headers={}
            with patch.object(sc.urllib.request,'urlopen',return_value=response),patch.object(sc,'now_utc',return_value=NOW):result=dc.fetch_demand(1,'us')
            self.assertEqual(result[-1],'OK');self.assertEqual(result.ids,['r1']);self.assertTrue(result.raw_ref)
            self.assertEqual(gzip.decompress((Path(folder)/result.raw_ref['object']).read_bytes()),body)


class BroadSamplingTests(unittest.TestCase):
    def rows(self):
        rows=[];tid=0
        for ratings in (10,200,1000):
            for version in ('2020-01-01','2026-09-01'):
                for price in (0,5):
                    tid+=1;rows.append(dict(track_id=tid,country='us',rating_count=ratings,price=price,version_date=version,scanned_at='2026-11-01',categories=['cards']))
        return rows

    def test_small_paid_old_and_recent_results_all_eligible_without_outcome_selection(self):
        rows=self.rows();selected=dc.wide_review_sample(rows,{},12,{'us'},NOW)
        self.assertEqual(set(selected),{(r['track_id'],'us') for r in rows})
        self.assertEqual(len(dc.wide_review_sample(rows,{},5,{'us'},NOW)),5)
        self.assertEqual(dc.wide_review_sample(rows,{},5,{'de'},NOW),[])
        # Changing complaint outcomes must not affect an otherwise identical frame.
        a={(1,'us'):dict(checked_at='2026-10-01',fetch_status='OK',r90=1,bad_share=100)}
        b={(1,'us'):dict(checked_at='2026-10-01',fetch_status='OK',r90=50,bad_share=0)}
        self.assertEqual(dc.wide_review_sample(rows,a,5,{'us'},NOW),dc.wide_review_sample(rows,b,5,{'us'},NOW))

    def test_budget_exclusion_freshness_and_new_release_override(self):
        rows=self.rows();rows[1]['scanned_at']='2026-01-01'
        rows[2]['categories']=['chart:top'];rows[3]['version_date']='2026-11-01T12:00:00Z'
        cache={(r['track_id'],'us'):dict(checked_at='2026-10-31',fetch_status='OK') for r in rows}
        self.assertEqual(dc.wide_review_sample(rows,cache,10,{'us'},NOW,exclude={(1,'us')}),[(4,'us')])

    def test_small_strata_and_query_families_are_not_swamped_by_large_groups(self):
        rows=self.rows()
        crowded=[dict(rows[0],track_id=100+i) for i in range(100)]
        selected=dc.wide_review_sample(crowded+rows,{},12,{'us'},NOW)
        by_key={(r['track_id'],'us'):r for r in crowded+rows}
        self.assertEqual(len({dc.exploration_stratum(by_key[k],NOW) for k in selected}),12)
        more=dc.wide_review_sample(crowded+rows,{},12,{'us'},NOW,exclude=selected)
        self.assertFalse(set(selected)&set(more))
        # Two query families in the same stratum should both get a slot.
        rows=[dict(crowded[0],track_id=i,categories=['a' if i<100 else 'b']) for i in range(105)]
        by_key={(r['track_id'],'us'):r for r in rows}
        selected=dc.wide_review_sample(rows,{},2,{'us'},NOW)
        self.assertEqual({by_key[k]['categories'][0] for k in selected},{'a','b'})


if __name__=='__main__':unittest.main()
