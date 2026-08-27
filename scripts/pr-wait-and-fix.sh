#!/usr/bin/env bash
# pr-wait-and-fix.sh — local companion to `pr-review-autofix.yml`.
# After `gh pr create`, polls for CI + bot reviews, then invokes a local
# opencode fix or prints threads for manual patching.
#
# Usage:
#   ./scripts/pr-wait-and-fix.sh [PR_NUMBER] [--wait 180] [--autofix]
#   ./scripts/pr-wait-and-fix.sh --help
#
# Defaults: PR = current branch's PR, wait = 180s, no autofix (just print).
set -euo pipefail

PR=""
WAIT=180
AUTOFIX=0

usage() {
  cat <<'USAGE'
Usage: ./scripts/pr-wait-and-fix.sh [PR_NUMBER] [--wait SECONDS] [--autofix]

Polls `gh pr checks --watch` then fetches Codex/Copilot/opencode review threads.
With --autofix, attempts `gh pr view --json ...` + local patch loop (requires MODEL_API_KEY).

Examples:
  gh pr create ... && ./scripts/pr-wait-and-fix.sh
  ./scripts/pr-wait-and-fix.sh 27 --wait 300 --autofix
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    --wait)
      [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "--wait requires an integer SECONDS argument" >&2; usage; exit 2; }
      WAIT="$2"; shift 2;;
    --autofix) AUTOFIX=1; shift;;
    [0-9]*) PR="$1"; shift;;
    *) echo "unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$PR" ]]; then
  PR=$(gh pr view --json number --jq .number 2>/dev/null || true)
  if [[ -z "$PR" ]]; then
    echo "No PR number given and current branch has no PR. Pass PR_NUMBER." >&2
    exit 2
  fi
fi

echo "== PR #$PR — waiting up to ${WAIT}s for checks + reviews =="

# 1) Wait for required checks (property + integration). Non-blocking if not present.
if command -v timeout >/dev/null 2>&1; then
  timeout "$WAIT" gh pr checks "$PR" --watch 2>&1 | tail -20 || echo "(checks watch timed out or not required — continuing)"
else
  gh pr checks "$PR" --watch 2>&1 | tail -20 || true
fi

sleep 10  # reviews often land 10-30s after checks

echo ""
echo "== Fetching review threads =="
gh api "repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/pulls/$PR/reviews" --jq '.[] | "\(.user.login) \(.state) \(.body[0:120])"' 2>&1 | head -20 || true
echo "---"
gh api graphql -f query='
  query($owner:String!,$repo:String!,$pr:Int!){
    repository(owner:$owner,name:$repo){
      pullRequest(number:$pr){
        reviewThreads(first:20){ nodes{ isResolved path line comments(first:1){nodes{body author{login}}}} }
      }
    }
  }' -f owner="$(gh repo view --json owner --jq .owner.login)" -f repo="$(gh repo view --json name --jq .name)" -F pr="$PR" 2>&1 | python3 -m json.tool 2>&1 | head -80 || true

if [[ "$AUTOFIX" -eq 1 ]]; then
  if [[ -z "${MODEL_API_KEY:-}" ]]; then
    echo "MODEL_API_KEY not set — cannot run opencode autofix. Export it or run without --autofix." >&2
    exit 0
  fi
  echo ""
  echo "== Triggering local opencode fix (agent: bugfix) =="
  # Requires `opencode` CLI installed; falls back to printing guidance.
  if command -v opencode >/dev/null 2>&1; then
    opencode run --agent bugfix --prompt "Address P1/P2 review threads on PR #$PR — fetch threads, patch minimally, run pytest -q -m 'not integration', push fixup." || echo "opencode run failed — apply threads manually"
  else
    echo "opencode CLI not found — install it or use the GitHub 'pr-review-autofix' workflow which runs on review events." >&2
  fi
else
  cat <<'NEXT'

Next:
  - If threads show P1 (e.g. Codex "secrets not in jobs.if"), patch, `git commit --amend` or fixup, `git push --force-with-lease`, then resolve threads:
    gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"PRRT_..."}){thread{isResolved}}}'
  - Or re-run with --autofix if MODEL_API_KEY is set.
  - The GitHub workflow `pr-review-autofix` will also auto-push a fix when a review is submitted.

NEXT
fi
