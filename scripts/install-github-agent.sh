#!/usr/bin/env bash
# install-github-agent.sh — reusable installer for Muse Spark + OpenCode GitHub agent
# Source cookbook: https://dev.meta.ai/docs/cookbook/github-agent
# Template repo: rubyrayjuntos/hermes-memory (branch main, path /)
# Usage:
#   ./install-github-agent.sh owner/repo          # clones via gh, installs Phase 1, pushes branch
#   ./install-github-agent.sh /path/to/local/repo # local path install (no push)
#   ./install-github-agent.sh owner/repo --phase all   # Phase 1+2+3
#   ./install-github-agent.sh --help

set -euo pipefail

PHASE="phase1" # phase1 = review+triage, phase2 = +command, all = +bugfix+stale
REPO=""
MODE="clone"
TARGET_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2;;
    --help|-h) echo "Usage: $0 [owner/repo | /path/to/repo] [--phase phase1|phase2|all]"; exit 0;;
    *) if [[ -z "$REPO" ]]; then REPO="$1"; else echo "unknown arg $1"; exit 1; fi; shift;;
  esac
done

if [[ -z "$REPO" ]]; then echo "error: provide owner/repo or /path/to/repo"; exit 1; fi

# Resolve target dir
if [[ -d "$REPO/.git" ]]; then
  MODE="local"
  TARGET_DIR="$(realpath "$REPO")"
  echo "[*] Local mode: $TARGET_DIR (phase=$PHASE)"
else
  MODE="clone"
  TMPDIR="$(mktemp -d)"
  echo "[*] Clone mode: $REPO -> $TMPDIR (phase=$PHASE)"
  gh repo clone "$REPO" "$TMPDIR/repo" -- --depth 1
  TARGET_DIR="$TMPDIR/repo"
fi

# Source files live in hermes-memory (canonical) or fallback to meta cookbook cache
SRC_ROOT="$HOME/hermes-memory"
if [[ ! -f "$SRC_ROOT/opencode.json" ]]; then
  SRC_ROOT="$HOME/.hermes/docs/meta-cookbook-github-agent/recipe"
fi
CACHE_ROOT="$HOME/.hermes/docs/meta-cookbook-github-agent/recipe"

copy_file() { mkdir -p "$(dirname "$2")"; cp -f "$1" "$2"; echo "  + $2"; }

echo "[*] Installing from $SRC_ROOT -> $TARGET_DIR"

# 1. opencode.json (Muse Spark)
if [[ -f "$SRC_ROOT/opencode.json" ]]; then
  copy_file "$SRC_ROOT/opencode.json" "$TARGET_DIR/opencode.json"
elif [[ -f "$CACHE_ROOT/opencode.json" ]]; then
  copy_file "$CACHE_ROOT/opencode.json" "$TARGET_DIR/opencode.json"
fi

# 2. AGENTS.md (project-specific, keep SECURITY)
if [[ -f "$SRC_ROOT/AGENTS.md" ]]; then
  copy_file "$SRC_ROOT/AGENTS.md" "$TARGET_DIR/AGENTS.md"
fi

# 3. .opencode (agents + commands)
if [[ -d "$SRC_ROOT/.opencode" ]]; then
  mkdir -p "$TARGET_DIR/.opencode"
  cp -r "$SRC_ROOT/.opencode" "$TARGET_DIR/" 2>/dev/null || true
  echo "  + .opencode/"
fi

# 4. Workflows — Phase-gated
mkdir -p "$TARGET_DIR/.github/workflows"
case "$PHASE" in
  phase1)
    for wf in opencode-pr-review.yml opencode-triage.yml; do
      [[ -f "$SRC_ROOT/.github/workflows/$wf" ]] && copy_file "$SRC_ROOT/.github/workflows/$wf" "$TARGET_DIR/.github/workflows/$wf"
      [[ -f "$CACHE_ROOT/workflow/$wf" ]] && [[ ! -f "$TARGET_DIR/.github/workflows/$wf" ]] && copy_file "$CACHE_ROOT/workflow/$wf" "$TARGET_DIR/.github/workflows/$wf"
    done
    ;;
  phase2)
    for wf in opencode-pr-review.yml opencode-triage.yml opencode-command.yml; do
      [[ -f "$SRC_ROOT/.github/workflows/$wf" ]] && copy_file "$SRC_ROOT/.github/workflows/$wf" "$TARGET_DIR/.github/workflows/$wf"
      [[ -f "$CACHE_ROOT/workflow/$wf" ]] && [[ ! -f "$TARGET_DIR/.github/workflows/$wf" ]] && copy_file "$CACHE_ROOT/workflow/$wf" "$TARGET_DIR/.github/workflows/$wf"
    done
    ;;
  all)
    for wf in opencode-pr-review.yml opencode-triage.yml opencode-command.yml opencode-bugfix.yml opencode-stale.yml; do
      [[ -f "$SRC_ROOT/.github/workflows/$wf" ]] && copy_file "$SRC_ROOT/.github/workflows/$wf" "$TARGET_DIR/.github/workflows/$wf"
      [[ -f "$CACHE_ROOT/workflow/$wf" ]] && [[ ! -f "$TARGET_DIR/.github/workflows/$wf" ]] && copy_file "$CACHE_ROOT/workflow/$wf" "$TARGET_DIR/.github/workflows/$wf"
    done
    ;;
esac

echo "[*] Done. Next:"
if [[ "$MODE" == "clone" ]]; then
  echo "  cd $TARGET_DIR && git checkout -b wip/github-agent && git add opencode.json AGENTS.md .opencode .github/workflows/opencode-*.yml && git commit -m 'feat: add Muse Spark GitHub agent (Phase $PHASE)' && git push -u origin wip/github-agent"
  echo "  Then: gh secret set MODEL_API_KEY --repo $REPO   # paste Meta API key"
  echo "       gh pr create --title 'feat: add Muse Spark GitHub agent' --body 'Cookbook: https://dev.meta.ai/docs/cookbook/github-agent'"
else
  echo "  cd $TARGET_DIR && git status"
  echo "  gh secret set MODEL_API_KEY --repo \$(gh repo view --json nameWithOwner -q .nameWithOwner)"
fi
