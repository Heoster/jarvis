#!/usr/bin/env bash
# Publish a job's pytest output into ci-diagnostics/ on the branch being built.
#
# Why this exists: Actions job logs live on a blob host that is not reachable
# from every environment, so a red run was undiagnosable without opening the web
# UI. Committing the report next to the code makes it readable with a plain
# `gh api .../contents/ci-diagnostics`. Set JARVIS_CI_PUBLISH_REPORTS=0 in the
# workflow to turn it off.
#
# Usage: publish-report.sh <slug> <log-file> <kind>
set -uo pipefail

slug="${1:?slug}"
logfile="${2:-}"
kind="${3:-run}"
branch="${GITHUB_REF_NAME:?GITHUB_REF_NAME}"
sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"
out="ci-diagnostics"

mkdir -p "$out"
report="$out/${slug}-${kind}-${sha:0:7}.md"

{
  echo "# pytest · ${slug} · ${kind} · ${sha}"
  echo
  echo '- runner: '"${RUNNER_OS:-?}"
  echo '- python: '"$(python --version 2>&1)"
  echo '- job: '"${GITHUB_JOB:-?}"
  echo
  echo '## installed'
  echo
  echo '```'
  python -m pip freeze 2>/dev/null | head -60
  echo '```'
  echo
  echo '## output (tail)'
  echo
  echo '```'
  if [ -n "$logfile" ] && [ -f "$logfile" ]; then
    tail -n 200 "$logfile"
  else
    echo "(no captured output — the suite passed and this run did not tee a log)"
  fi
  echo '```'
} > "$report"

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git config core.autocrlf false

for attempt in 1 2 3 4 5; do
  # Rebase on the branch tip without touching the work tree: the matrix jobs run
  # concurrently, so a hard reset here would delete the reports the other jobs
  # just pushed. Only this job's own report is ever staged.
  git fetch --quiet origin "$branch"
  git reset --mixed --quiet "origin/$branch"
  git add -- "$report"
  if git diff --cached --quiet; then
    echo "report already published ($report)"
    exit 0
  fi
  if git commit --quiet -m "ci: ${slug} ${kind} report (${sha:0:7})" \
     && git push --quiet origin "HEAD:$branch"; then
    echo "published $report"
    exit 0
  fi
  echo "push race, retrying ($attempt)"
  sleep 3
done

echo "::error::could not publish the pytest report for $slug"
exit 1
