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
  # Only generated files may differ. A code/schema change needs a fresh run,
  # rather than silently reverting upstream code or using old loaded modules.
  python3 - <<'PY'
import subprocess
allowed={'niches.db','niches.db.gz','niches.db.checkpoint.json','report.md','report_t2.md','demand_report.md','geo_gap_report.md','charts_report.md','clusters_report_us.md','clusters_report_gb.md','docs/scoreboard.md','docs/divergence.md','docs/hypotheses.json','docs/hypotheses.md','docs/current_metrics.json','docs/data_quality.json','docs/telegram_preview.txt'}
def git(*args):return set(subprocess.check_output(['git',*args],text=True).splitlines())
changed=git('diff','--name-only','HEAD','FETCH_HEAD')|git('diff','--name-only','HEAD')|git('ls-files','--others','--exclude-standard')
if changed-allowed:
    raise SystemExit('Code changed; collected DB is retained for recovery. Rerun using the updated code.')
PY
  # The checkpoint is outside the checkout. Decode origin anew on every retry.
  rm -f "$scan_checkpoint"/origin.db*
  if git show 'FETCH_HEAD:niches.db.checkpoint.json' > "$scan_checkpoint/origin.db.checkpoint.json" 2>/dev/null; then
    python3 db_store.py restore --db "$scan_checkpoint/origin.db"
  else
    rm -f "$scan_checkpoint/origin.db.checkpoint.json"
    if git show 'FETCH_HEAD:niches.db.gz' > "$scan_checkpoint/origin.db.gz" 2>/dev/null; then
      python3 db_store.py restore --db "$scan_checkpoint/origin.db"
    else
      git show 'FETCH_HEAD:niches.db' > "$scan_checkpoint/origin.db"
    fi
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
  python3 db_store.py publish --archive "$scan_checkpoint/niches.db.xz" --pointer niches.db.checkpoint.json
  git rm -f --cached --ignore-unmatch niches.db niches.db.gz
  rm -f niches.db.gz
  git add -A
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
