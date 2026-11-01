"""Exercise CI persistence against local bare Git remotes, never GitHub."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import db_store
import scanner as sc

ROOT=Path(__file__).resolve().parents[1]


class StorageGitTests(unittest.TestCase):
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
        self.add(seed,1);db_store.pack(seed/'niches.db')
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
            self.add(local,1);db_store.pack(local/'niches.db');self.add(local,2)
            self.add(incoming,1);self.add(incoming,3)
            db_store.pack(incoming/'niches.db',local/'niches.db.gz')
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
            self.assertEqual(self.git(final,'ls-files','niches.db'),'')
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
