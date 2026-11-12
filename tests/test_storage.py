"""Exercise CI persistence against local bare Git remotes, never GitHub."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import db_store
import scanner as sc

ROOT=Path(__file__).resolve().parents[1]


class StorageGitTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.environ.pop,'NICHE_CHECKPOINT_DIR',None)

    def git(self,path,*args):
        return subprocess.check_output(['git',*args],cwd=path,stderr=subprocess.STDOUT,text=True).strip()

    def add(self,path,tid):
        con=sc.open_db(path/'niches.db')
        row=sc.normalize(dict(trackId=tid,trackName=f'App {tid}',userRatingCount=1000,currentVersionReleaseDate='2020-01-01T00:00:00Z'),'us','c','q')
        sc.upsert_app(con,row);sc.insert_snapshot(con,row);con.commit();con.close()

    def repos(self,folder):
        base=Path(folder);origin=base/'origin';seed=base/'seed';seed.mkdir()
        self.git(base,'init','--bare',str(origin));self.git(seed,'init','-b','main')
        self.git(seed,'config','user.name','test');self.git(seed,'config','user.email','test@example.test')
        for name in ('ci_save.sh','db_store.py','db_merge.py','demand_history.py'):
            shutil.copy(ROOT/name,seed/name)
        (seed/'.gitignore').write_text('niches.db\n__pycache__/\n')
        # Persistence integration only; report rendering is tested separately.
        (seed/'rebuild_reports.py').write_text("import sqlite3,sys\nfrom pathlib import Path\nwith sqlite3.connect(sys.argv[2]) as c:\n assert c.execute('pragma quick_check').fetchone()[0]=='ok'\n Path('report.md').write_text(str(c.execute('select count(*) from apps').fetchone()[0]))\n")
        self.add(seed,1);os.environ['NICHE_CHECKPOINT_DIR']=str(base/'store')
        db_store.publish(db_store.pack(seed/'niches.db'),seed/'niches.db.checkpoint.json');(seed/'niches.db.xz').unlink()
        self.git(seed,'add','-A');self.git(seed,'commit','-m','initial');self.git(seed,'remote','add','origin',str(origin));self.git(seed,'push','origin','main')
        self.git(base,'--git-dir',str(origin),'symbolic-ref','HEAD','refs/heads/main')
        clones=[]
        for name in ('a','b'):
            clone=base/name;self.git(base,'clone',str(origin),str(clone));db_store.ensure(clone/'niches.db');clones.append(clone)
        return origin,clones

    def env(self,folder):
        return dict(os.environ,GITHUB_ACTIONS='true',GITHUB_REF_NAME='main',PYTHONPYCACHEPREFIX=str(Path(folder)/'cache'))

    def test_explicit_sync_preserves_local_and_pulled_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);local=base/'local';incoming=base/'incoming'
            local.mkdir();incoming.mkdir()
            self.add(local,1);self.add(local,2)
            self.add(incoming,1);self.add(incoming,3)
            db_store.pack(incoming/'niches.db',local/'niches.db.xz')
            db_store.sync(local/'niches.db')
            with sqlite3.connect(local/'niches.db') as con:
                self.assertEqual({r[0] for r in con.execute('SELECT track_id FROM apps')},{1,2,3})

    def test_simultaneous_data_pushes_merge_without_losing_either_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            origin,clones=self.repos(folder)
            self.add(clones[0],2);self.add(clones[1],3)
            jobs=[subprocess.Popen(['bash','ci_save.sh','concurrent test'],cwd=path,env=self.env(folder),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) for path in clones]
            for job in jobs:
                output,_=job.communicate(timeout=30)
                self.assertEqual(job.returncode,0,output)
            final=Path(folder)/'final';self.git(Path(folder),'clone',str(origin),str(final));db_store.ensure(final/'niches.db')
            with sqlite3.connect(final/'niches.db') as con:
                self.assertEqual({r[0] for r in con.execute('SELECT track_id FROM apps')},{1,2,3})
            self.assertEqual(self.git(final,'ls-files','niches.db','niches.db.gz'),'')
            pointer=json.loads((final/'niches.db.checkpoint.json').read_text())
            self.assertEqual(db_store.digest(Path(os.environ['NICHE_CHECKPOINT_DIR'])/pointer['asset']),pointer['sha256'])
            self.assertEqual((final/'report.md').read_text(),'3')

    def test_upstream_code_change_preserves_local_collected_database(self):
        with tempfile.TemporaryDirectory() as folder:
            origin,(upstream,local)=self.repos(folder)
            self.git(upstream,'config','user.name','test');self.git(upstream,'config','user.email','test@example.test')
            with (upstream/'db_merge.py').open('a') as output:output.write('\n# upstream code update\n')
            self.git(upstream,'add','db_merge.py');self.git(upstream,'commit','-m','new code');self.git(upstream,'push','origin','main')
            self.add(local,3)
            result=subprocess.run(['bash','ci_save.sh','code race'],cwd=local,env=self.env(folder),capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Code changed',result.stderr)
            with sqlite3.connect(local/'niches.db') as con:
                self.assertEqual({r[0] for r in con.execute('SELECT track_id FROM apps')},{1,3})
            self.assertEqual(self.git(upstream,'rev-parse','HEAD'),self.git(Path(folder),'--git-dir',str(origin),'rev-parse','main'))

    def test_upstream_docs_or_workflow_commit_does_not_discard_collected_data(self):
        with tempfile.TemporaryDirectory() as folder:
            origin,(upstream,local)=self.repos(folder)
            self.git(upstream,'config','user.name','test');self.git(upstream,'config','user.email','test@example.test')
            (upstream/'NOTES.md').write_text('pushed while a scan ran\n')
            self.git(upstream,'add','NOTES.md');self.git(upstream,'commit','-m','docs');self.git(upstream,'push','origin','main')
            self.add(local,3)
            subprocess.run(['bash','ci_save.sh','docs race'],cwd=local,env=self.env(folder),check=True,capture_output=True)
            final=Path(folder)/'final';self.git(Path(folder),'clone',str(origin),str(final))
            self.assertTrue((final/'NOTES.md').exists())
            self.assertTrue(db_store.ensure(final/'niches.db'))
            with sqlite3.connect(final/'niches.db') as con:
                self.assertIn(3,{r[0] for r in con.execute('SELECT track_id FROM apps')})

    def test_git_keeps_a_pointer_instead_of_a_new_binary_blob_per_save(self):
        with tempfile.TemporaryDirectory() as folder:
            origin,(clone,_)=self.repos(folder)
            self.add(clone,2)
            subprocess.run(['bash','ci_save.sh','first'],cwd=clone,env=self.env(folder),check=True,capture_output=True)
            tracked=self.git(clone,'ls-files').split('\n')
            self.assertIn('niches.db.checkpoint.json',tracked)
            self.assertFalse(any(name.endswith(('.gz','.xz','.db')) for name in tracked))

    def test_first_save_migrates_the_legacy_archive_out_of_git(self):
        with tempfile.TemporaryDirectory() as folder:
            origin,(clone,other)=self.repos(folder)
            self.git(clone,'config','user.name','test');self.git(clone,'config','user.email','test@example.test')
            self.git(clone,'rm','-q','niches.db.checkpoint.json')
            archive=clone/'niches.db.gz'
            with sqlite3.connect(clone/'niches.db') as con:pass
            import gzip
            archive.write_bytes(gzip.compress((clone/'niches.db').read_bytes()))
            self.git(clone,'add','-f','niches.db.gz');self.git(clone,'commit','-m','legacy state');self.git(clone,'push','origin','main')
            self.add(clone,2)
            subprocess.run(['bash','ci_save.sh','migrate'],cwd=clone,env=self.env(folder),check=True,capture_output=True)
            final=Path(folder)/'final';self.git(Path(folder),'clone',str(origin),str(final))
            self.assertEqual(self.git(final,'ls-files','niches.db.gz'),'')
            self.assertTrue(db_store.ensure(final/'niches.db'))
            with sqlite3.connect(final/'niches.db') as con:
                self.assertEqual({r[0] for r in con.execute('SELECT track_id FROM apps')},{1,2})
                names={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn('idx_snap_store_time',names)

    def test_release_outage_stores_the_checkpoint_in_git_and_the_next_save_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            origin,(clone,_)=self.repos(folder)
            healthy=dict(self.env(folder),NICHE_CHECKPOINT_DIR=os.environ['NICHE_CHECKPOINT_DIR'])
            store=Path(healthy['NICHE_CHECKPOINT_DIR']);self.add(clone,2)
            store.chmod(0o555)  # Reads still work, as with a missing token on a public repo.
            try:
                subprocess.run(['bash','ci_save.sh','outage'],cwd=clone,env=healthy,check=True,capture_output=True)
            finally:
                store.chmod(0o755)
            final=Path(folder)/'after-outage';self.git(Path(folder),'clone',str(origin),str(final))
            self.assertEqual(self.git(final,'ls-files','niches.db.xz'),'niches.db.xz')
            self.assertEqual(self.git(final,'ls-files','niches.db.checkpoint.json'),'')
            self.assertTrue(db_store.ensure(final/'niches.db'))
            with sqlite3.connect(final/'niches.db') as con:
                self.assertEqual({r[0] for r in con.execute('SELECT track_id FROM apps')},{1,2})
            self.add(clone,3)
            subprocess.run(['bash','ci_save.sh','recovered'],cwd=clone,env=healthy,check=True,capture_output=True)
            final=Path(folder)/'after-recovery';self.git(Path(folder),'clone',str(origin),str(final))
            self.assertEqual(self.git(final,'ls-files','niches.db.xz'),'')
            self.assertTrue(db_store.ensure(final/'niches.db'))
            with sqlite3.connect(final/'niches.db') as con:
                self.assertEqual({r[0] for r in con.execute('SELECT track_id FROM apps')},{1,2,3})

    def test_old_archive_without_later_columns_can_still_be_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old.db'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE snapshots(id INTEGER PRIMARY KEY,track_id INTEGER,country TEXT,scanned_at TEXT)')
            db_store.restore_indexes(path)
            with sqlite3.connect(path) as con:
                names={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertEqual(names,{'idx_snap_track_time','idx_snap_store_time'})

    def test_checkpoint_that_does_not_match_its_pointer_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);os.environ['NICHE_CHECKPOINT_DIR']=str(base/'store')
            source=base/'a';source.mkdir();self.add(source,1)
            pointer=base/'niches.db.checkpoint.json'
            entry=db_store.publish(db_store.pack(source/'niches.db'),pointer)
            (base/'store'/entry['asset']).write_bytes(b'tampered')
            with self.assertRaises(ValueError):db_store.ensure(base/'niches.db')
            self.assertFalse((base/'niches.db').exists())

    def test_release_is_created_once_and_a_parallel_creator_is_tolerated(self):
        failure=subprocess.CalledProcessError(1,'gh')
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,GITHUB_REPOSITORY='o/r'):
            os.environ.pop('NICHE_CHECKPOINT_DIR',None)
            path=Path(folder)/'niches-abc.db.xz';path.write_bytes(b'x')
            with patch.object(db_store,'gh',side_effect=[failure,failure,'','']) as gh:
                db_store.upload(path)
            commands=[call.args[:2] for call in gh.call_args_list]
            self.assertEqual(commands,[('release','view'),('release','create'),('release','view'),('release','upload')])
            self.assertIn('o/r',gh.call_args_list[-1].args)
            with patch.object(db_store,'gh',side_effect=['','']) as gh:
                db_store.upload(path)
            self.assertEqual([call.args[1] for call in gh.call_args_list],['view','upload'])

    def test_failed_upload_leaves_the_pointer_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);pointer=base/'pointer.json';pointer.write_text('previous')
            archive=base/'a.xz';archive.write_bytes(b'x')
            with patch.object(db_store,'upload',side_effect=OSError('offline')),self.assertRaises(OSError):
                db_store.publish(archive,pointer)
            self.assertEqual(pointer.read_text(),'previous')

    def test_prune_keeps_the_current_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);os.environ['NICHE_CHECKPOINT_DIR']=str(base/'store');(base/'store').mkdir()
            for i in range(5):
                path=base/'store'/f'c{i}.xz';path.write_bytes(b'x');os.utime(path,(i,i))
            removed=db_store.prune(keep=2,protect={'c0.xz'})
            self.assertEqual(sorted(removed),['c1.xz','c2.xz'])
            self.assertEqual(sorted(p.name for p in (base/'store').iterdir()),['c0.xz','c3.xz','c4.xz'])
