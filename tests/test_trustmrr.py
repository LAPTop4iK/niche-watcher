import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trustmrr_watch as tw


def record(slug,mrr=100.5,url='https://apps.apple.com/us/app/id123',**extra):
    return dict(slug=slug,name=slug.title(),category='Mobile Apps',isMobileApp=bool(url),paymentProvider='revenuecat',
                revenue=dict(last30Days=mrr*1.2,mrr=mrr),growth30d=12.5,onSale=False,
                mobileApp=dict(appStore=dict(url=url,country='us',ratingCount=40)) if url else None,**extra)


class PrivateStorage(unittest.TestCase):
    def test_data_is_never_kept_inside_the_public_repository(self):
        with self.assertRaises(SystemExit):
            tw.private_path(tw.HERE/'trustmrr.db')
        with self.assertRaises(SystemExit):
            tw.private_path(tw.HERE/'docs'/'report.html')
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(tw.private_path(Path(folder)/'t.db'),(Path(folder)/'t.db').resolve())


class Collection(unittest.TestCase):
    def test_public_snapshot_keeps_dollars_and_links_the_app_store_id(self):
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db')
        stored=tw.store(con,[record('docphoto'),record('docphoto'),record('web',url=None),record('hidden',stealthMode=True)],'public:x','2026-11-23T00:00:00Z')
        self.assertEqual(stored,2)
        row=con.execute('SELECT * FROM startups WHERE slug="docphoto"').fetchone()
        self.assertEqual((row['app_store_id'],row['is_mobile']),(123,1))
        obs=con.execute('SELECT mrr,revenue_30d,store_rating_count FROM observations WHERE slug="docphoto"').fetchone()
        self.assertEqual(tuple(obs),(100.5,120.6,40))
        self.assertIsNone(con.execute('SELECT app_store_id FROM startups WHERE slug="web"').fetchone()[0])

    def test_api_and_public_snapshots_share_dollar_units(self):
        # Nov 23 2026: one startup came back with MRR 1840.63 from both, despite the API docs saying cents.
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db')
        tw.store(con,[record('both',mrr=1840.63)],'public:x','2026-11-23T00:00:00Z')
        tw.store(con,[record('both',mrr=1840.63)],'api:mobile-apps','2026-11-23T00:00:00Z')
        self.assertEqual({r[0] for r in con.execute('SELECT mrr FROM observations')},{1840.63})

    def test_api_is_used_only_with_the_owners_key(self):
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db');calls=[]
        def fake(url,headers=None,attempts=3):
            calls.append((url,headers));return {}
        with patch.object(tw,'get_json',side_effect=fake),patch.object(tw,'api_key',return_value=None):
            tw.collect(con,api_pages=3)
        self.assertEqual([u for u,_ in calls],['https://trustmrr.com/api/ai/discovery','https://trustmrr.com/api/ai'])
        calls.clear()
        with patch.object(tw,'get_json',side_effect=fake),patch.object(tw,'api_key',return_value='tmrr_test'),patch.object(tw.time,'sleep'):
            tw.collect(con,api_pages=3)
        self.assertEqual(calls[-1][1],{'Authorization':'Bearer tmrr_test'})
        self.assertIn('category=mobile-apps',calls[-1][0])

    def test_rejected_key_keeps_the_public_snapshot(self):
        import urllib.error
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db')
        def fake(url,headers=None,attempts=3):
            if headers:raise urllib.error.HTTPError(url,401,'Unauthorized',{},None)
            return {'recentlyAddedStartups':[record('a')]}
        with patch.object(tw,'get_json',side_effect=fake),patch.object(tw,'api_key',return_value='tmrr_bad'):
            tw.collect(con,api_pages=3)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM startups').fetchone()[0],1)

    def test_key_comes_from_the_environment_first_then_the_keychain(self):
        class Found:
            def __init__(self,code,out):self.returncode,self.stdout=code,out
        with patch.dict('os.environ',{'TRUSTMRR_API_KEY':' tmrr_env\n'}):
            self.assertEqual(tw.api_key(),'tmrr_env')
        with patch.dict('os.environ',{},clear=True),patch.object(tw.sys,'platform','darwin'),patch('subprocess.run',return_value=Found(0,'tmrr_kc\n')) as run:
            self.assertEqual(tw.api_key(),'tmrr_kc')
        self.assertEqual(run.call_args.args[0][:4],['security','find-generic-password','-s','trustmrr-api'])
        with patch.dict('os.environ',{},clear=True),patch.object(tw.sys,'platform','darwin'),patch('subprocess.run',return_value=Found(44,'')):
            self.assertIsNone(tw.api_key())
        with patch.dict('os.environ',{},clear=True),patch.object(tw.sys,'platform','linux'):
            self.assertIsNone(tw.api_key())

    def test_niche_checks_go_by_revenue_and_skip_fresh_ones(self):
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db')
        tw.store(con,[record('small',mrr=10,url='https://apps.apple.com/us/app/id1'),record('big',mrr=900,url='https://apps.apple.com/us/app/id2'),
                      record('fresh',mrr=5000,url='https://apps.apple.com/us/app/id3')],'public:x','2026-11-23T00:00:00Z')
        con.execute("INSERT INTO niches VALUES ('fresh','2026-11-22T00:00:00Z','t','us','{}')")
        today=datetime(2026,11,23,tzinfo=timezone.utc)
        self.assertEqual([r['slug'] for r in tw.due(con,{},today)],['big','small'])
        self.assertEqual([r['slug'] for r in tw.due(con,{},today,limit=1)],['big'])
        self.assertEqual([r['slug'] for r in tw.due(con,{'fresh':'new phrase'},today)],['fresh','big','small'])


class Niche(unittest.TestCase):
    def test_search_terms_come_from_both_sides_of_the_title(self):
        self.assertEqual(tw.term_candidates('DocPhoto: passport photo maker'),['passport photo maker'])
        self.assertEqual(tw.term_candidates('Volume Booster: Louder Sound'),['volume booster','louder sound'])
        self.assertEqual(tw.term_candidates('Ace Pitcher - Velocity Program'),['ace pitcher','velocity program'])
        self.assertEqual(tw.term_candidates('Chalkie'),['chalkie'])

    def test_competition_profile_of_the_top_ten(self):
        today=datetime(2026,11,23,tzinfo=timezone.utc)
        results=[dict(trackId=i,trackName=f'A{i}',userRatingCount=c,price=p,currentVersionReleaseDate=d)
                 for i,(c,p,d) in enumerate([(5000,0,'2023-01-01T00:00:00Z'),(1000,2.99,'2026-10-01T00:00:00Z'),(0,0,'2026-11-01T00:00:00Z')])]
        c=tw.competition(results,today)
        self.assertEqual((c['top10_ratings'],c['leader_ratings'],c['apps_over_1k']),(6000,5000,2))
        self.assertEqual((c['leader_share'],c['stale_share'],c['paid_share']),(0.83,0.33,0.33))
        self.assertEqual(c['leaders'][0]['name'],'A0')

    def test_complaints_skip_leaders_without_reviews_and_keep_low_scores(self):
        leaders=[dict(id=1,ratings=500),dict(id=2,ratings=5)]
        bad=[dict(text='crashes every time I open the scanner',score=1),dict(text='crashes after update, scanner broken',score=2),
             dict(text='crashes again',score=1),dict(text='fine but crashes sometimes',score=3)]
        fetched=[]
        def fetch(track_id,country,n):
            fetched.append(track_id);return bad
        out=tw.complaints(leaders,'us',fetch)
        self.assertEqual(fetched,[1]);self.assertEqual(out['bad_reviews'],3)
        self.assertEqual(out['themes'][0]['word'],'crashes')


class Reviews(unittest.TestCase):
    def test_missing_review_feed_is_not_retried(self):
        import urllib.error
        calls=[]
        def fake(url,headers=None,attempts=3):
            calls.append(url);raise urllib.error.HTTPError(url,404,'Not Found',{},None)
        with patch.object(tw,'get_json',side_effect=fake):
            self.assertEqual(tw.ios_reviews(1,'us'),[])
        self.assertEqual(len(calls),1)

    def test_review_feed_keeps_low_scores_and_stops_on_an_empty_page(self):
        page={'feed':{'entry':[{'im:rating':{'label':'1'},'content':{'label':'crashes'}},
                               {'im:rating':{'label':'5'},'content':{'label':'great'}},{'title':'app entry'}]}}
        with patch.object(tw,'get_json',side_effect=[page,{'feed':{}}]),patch.object(tw.time,'sleep'):
            self.assertEqual(tw.ios_reviews(1,'us'),[dict(text='crashes',score=1)])


class Report(unittest.TestCase):
    def test_report_escapes_text_and_shows_revenue_per_rating(self):
        folder=Path(tempfile.mkdtemp());con=tw.open_db(folder/'t.db')
        tw.store(con,[record('a',mrr=1000),record('web',url=None,description='<script>x</script>')],'public:x','2026-11-23T00:00:00Z')
        payload=dict(app=dict(name='A',ratings=60,url='https://apps.apple.com/us/app/id123',genre='Utilities'),
                     competition=dict(leader_ratings=5000,median_ratings=100,apps_over_1k=2,stale_share=0.4),
                     complaints=dict(bad_reviews=3,themes=[dict(word='crash',count=3,example='"crash" <b>')]))
        con.execute('INSERT INTO niches VALUES (?,?,?,?,?)',('a','2026-11-23T00:00:00Z','photo maker','us',json.dumps(payload)))
        text=tw.report(con,folder/'r.html').read_text()
        self.assertIn('$20.0',text)  # 1200 / 60
        self.assertIn('crash ×3',text)
        self.assertNotIn('<script>x',text);self.assertIn('&lt;script&gt;',text)

    def test_spearman_needs_four_points(self):
        self.assertIsNone(tw.spearman([1,2,3],[1,2,3]))
        self.assertAlmostEqual(tw.spearman([1,2,3,4],[10,20,30,40]),1.0)


if __name__=='__main__':
    unittest.main()
