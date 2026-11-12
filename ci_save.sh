#!/usr/bin/env bash
# CI-only. Never run against a developer's working checkout.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "ci_save.sh requires an isolated GitHub Actions checkout" >&2
  exit 2
fi
scan_branch="${GITHUB_REF_NAME:-main}"
git check-ref-format --branch "$scan_branch" >/dev/null
scan_checkpoint=$(mktemp -d "${TMPDIR:-/tmp}/niche-save.XXXXXX")
restore_checkpoint() {
  scan_status=$?
  trap - EXIT
  if [[ -s "$scan_checkpoint/local.db" ]]; then
    python3 db_store.py backup --db "$scan_checkpoint/local.db" --out niches.db || true
  fi
  rm -rf "$scan_checkpoint"
  exit "$scan_status"
}
trap restore_checkpoint EXIT
python3 db_store.py backup --out "$scan_checkpoint/local.db"
for scan_report in charts_report.md clusters_report_us.md clusters_report_gb.md report_t2.md; do
  if [[ -f "$scan_report" && -n "$(git status --porcelain -- "$scan_report")" ]]; then
    cp "$scan_report" "$scan_checkpoint/$scan_report"
  fi
done
git config user.name "scan-bot"
git config user.email "scan-bot@users.noreply.github.com"
for scan_attempt in 1 2 3; do
  git fetch origin "$scan_branch"
  # Locally only generated files may differ. Upstream, a code/schema change needs a
  # fresh run rather than old loaded modules; a docs or workflow commit pushed while
  # this job ran changes nothing it computed, and used to discard its data.
  python3 - <<'PY'
import subprocess
allowed={'niches.db','niches.db.gz','niches.db.xz','niches.db.checkpoint.json','report.md','report_t2.md','demand_report.md','geo_gap_report.md','charts_report.md','clusters_report_us.md','clusters_report_gb.md','docs/scoreboard.md','docs/divergence.md','docs/hypotheses.json','docs/hypotheses.md','docs/current_metrics.json','docs/data_quality.json','docs/telegram_preview.txt'}
code=lambda path:path.endswith(('.py','.sh')) or path in {'seed_queries.json','markets.json','research_hypotheses.json','requirements-play.txt'}
def git(*args):return set(subprocess.check_output(['git',*args],text=True).splitlines())
local=git('diff','--name-only','HEAD')|git('ls-files','--others','--exclude-standard')
upstream={path for path in git('diff','--name-only','HEAD','FETCH_HEAD') if code(path)}
if local-allowed or upstream:
    raise SystemExit('Code changed; collected DB is retained for recovery. Rerun using the updated code.')
PY
  # The checkpoint is outside the checkout. Decode origin anew on every retry.
  rm -f "$scan_checkpoint"/origin.db*
  scan_origin=""
  for scan_format in checkpoint.json xz gz; do
    if git show "FETCH_HEAD:niches.db.$scan_format" > "$scan_checkpoint/origin.db.$scan_format" 2>/dev/null; then
      scan_origin="$scan_format"
      break
    fi
    rm -f "$scan_checkpoint/origin.db.$scan_format"
  done
  if [[ -n "$scan_origin" ]]; then
    python3 db_store.py restore --db "$scan_checkpoint/origin.db"
  else
    git show 'FETCH_HEAD:niches.db' > "$scan_checkpoint/origin.db"
  fi
  python3 db_merge.py --main "$scan_checkpoint/local.db" --other "$scan_checkpoint/origin.db"
  # Source code is identical; generated files are captured/rebuilt below.
  git reset --hard FETCH_HEAD
  python3 rebuild_reports.py --db "$scan_checkpoint/local.db"
  for scan_report in charts_report.md clusters_report_us.md clusters_report_gb.md report_t2.md; do
    if [[ -f "$scan_checkpoint/$scan_report" ]]; then
      cp "$scan_checkpoint/$scan_report" "$scan_report"
    fi
  done
  # The asset goes up before the pointer: Git never names a missing checkpoint.
  python3 db_store.py pack --db "$scan_checkpoint/local.db" --out "$scan_checkpoint/niches.db.xz"
  if python3 db_store.py publish --archive "$scan_checkpoint/niches.db.xz" --pointer niches.db.checkpoint.json; then
    git rm -f --cached --ignore-unmatch niches.db niches.db.gz niches.db.xz
    rm -f niches.db.gz niches.db.xz
    git add -A
  else
    # Collected data outranks a clean history: one blob in Git until the Release recovers.
    # GitHub refuses files over 100 MB; past that the job artifact keeps the data.
    if (( $(wc -c < "$scan_checkpoint/niches.db.xz") > 95000000 )); then
      echo "::error::Release unavailable and the checkpoint exceeds Git's file limit" >&2
      exit 1
    fi
    echo "::warning::Release unavailable; this checkpoint goes through Git"
    git rm -f --cached --ignore-unmatch niches.db niches.db.gz niches.db.checkpoint.json
    rm -f niches.db.gz niches.db.checkpoint.json
    cp "$scan_checkpoint/niches.db.xz" niches.db.xz
    git add -A
    git add -f niches.db.xz
  fi
  if ! git diff --cached --quiet; then
    git commit -m "${1:-scan checkpoint}"
  fi
  if git push origin "HEAD:$scan_branch"; then
    # A rollback copy is a convenience; losing old ones must not fail the save.
    python3 db_store.py prune --pointer niches.db.checkpoint.json || echo "Checkpoint pruning skipped" >&2
    exit 0
  fi
  echo "Concurrent data push; retry $scan_attempt/3" >&2
done
exit 1
