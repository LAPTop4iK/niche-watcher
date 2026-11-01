#!/usr/bin/env python3
"""Run a research collector with full local response capture (no extra services)."""
import argparse
import os
from pathlib import Path
import sys
import raw_archive

ROOT=Path(__file__).resolve().parent
COLLECTORS=('scanner','demand_check','charts_scan','review_clusters','study_sample','verify_pipeline')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--archive-dir',default=str(ROOT/'.data/raw'))
    ap.add_argument('--max-mib',type=int,default=5120)
    ap.add_argument('collector',choices=COLLECTORS)
    ap.add_argument('arguments',nargs=argparse.REMAINDER)
    args=ap.parse_args()
    if args.max_mib<=0:ap.error('max-mib must be positive')
    if os.environ.get('GITHUB_ACTIONS')=='true':
        ap.error('Raw capture is local: ephemeral CI storage is not a durable archive')
    os.environ[raw_archive.ENV_DIR]=str(Path(args.archive_dir).expanduser().resolve())
    os.environ[raw_archive.ENV_LIMIT]=str(args.max_mib)
    # One process owns the environment; library calls in workers share the archive.
    os.execv(sys.executable,[sys.executable,str(ROOT/(args.collector+'.py')),*args.arguments])


if __name__=='__main__':main()
