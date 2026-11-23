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

    def test_api_snapshot_converts_cents(self):
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db')
        tw.store(con,[record('paid',mrr=18000)],'api:mobile-apps','2026-11-23T00:00:00Z',cents=True)
        self.assertEqual(con.execute('SELECT mrr FROM observations').fetchone()[0],180.0)

    def test_api_is_used_only_with_the_owners_key(self):
        con=tw.open_db(Path(tempfile.mkdtemp())/'t.db');calls=[]
        def fake(url,headers=None,attempts=3):
            calls.append((url,headers));return {}
        with patch.object(tw,'get_json',side_effect=fake),patch.dict('os.environ',{},clear=True):
            tw.collect(con,api_pages=3)
        self.assertEqual([u for u,_ in calls],['https://trustmrr.com/api/ai/discovery','https://trustmrr.com/api/ai'])
        calls.clear()
        with patch.object(tw,'get_json',side_effect=fake),patch.dict('os.environ',{'TRUSTMRR_API_KEY':'tmrr_test'}),patch.object(tw.time,'sleep'):
            tw.collect(con,api_pages=3)
        self.assertEqual(calls[-1][1],{'Authorization':'Bearer tmrr_test'})
        self.assertIn('category=mobile-apps',calls[-1][0])


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
