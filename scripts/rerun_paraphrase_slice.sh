#!/usr/bin/env bash
set -euo pipefail

# Re-run paraphrase slice only after T3.6 fixes (wider cues + role normalization)

# Source keys
source ~/.hermes/profiles/librarian/cache/scratch/nous-bakeoff.env
source ~/.hermes/.env 2>/dev/null || true
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-dummy}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
EVAL_DIR="$ROOT_DIR/docs/evals"

# Throwaway DB setup
export EVAL_DSN="postgres://hermes:dev_password_12345@127.0.0.1:5456/hermes_paraphrase_eval"

echo "Setting up throwaway DB..."
docker rm -f hermes-paraphrase-rerun-pg 2>/dev/null || true
docker run -d --name hermes-paraphrase-rerun-pg \
  -e POSTGRES_USER=hermes \
  -e POSTGRES_PASSWORD=dev_password_12345 \
  -e POSTGRES_DB=hermes_paraphrase_eval \
  -p 127.0.0.1:5456:5432 \
  hermes-memory-postgres:local

# Wait for DB
sleep 5
for i in {1..30}; do
  if psql "$EVAL_DSN" -c "SELECT 1" >/dev/null 2>&1; then
    echo "DB ready"
    break
  fi
  sleep 1
done

# Run migrations
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src"
python3 scripts/migrate.py --dsn "$EVAL_DSN"

PARAPHRASE="$ROOT_DIR/tests/fixtures/paraphrase_slice.json"
NOUS_URL="https://inference-api.nousresearch.com/v1"
OR_URL="https://openrouter.ai/api/v1"
OLLAMA_URL="http://127.0.0.1:11434/v1"

# Run all 4 models
echo "===== Running rules_only on paraphrase ====="
python3 scripts/spec3_bakeoff.py \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE" \
  --extract-mode rules_only \
  --json-out "$EVAL_DIR/2026-11-18-paraphrase-rerun-rules-only.json"

echo "===== Running llama3.2:3b on paraphrase ====="
python3 scripts/spec3_bakeoff.py \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE" \
  --extract-mode rules_then_model \
  --extract-url "$OLLAMA_URL" \
  --extract-model "llama3.2:3b" \
  --key-env HYBRID_AGE_EXTRACT_KEY \
  --json-out "$EVAL_DIR/2026-11-18-paraphrase-rerun-llama32-3b.json"

echo "===== Running Nemotron 3.5 Lightning on paraphrase ====="
export HYBRID_AGE_EXTRACT_KEY="$OPENROUTER_API_KEY"
python3 scripts/spec3_bakeoff.py \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE" \
  --extract-mode rules_then_model \
  --extract-url "$OR_URL" \
  --extract-model "nvidia/nemotron-3.5-lightning:free" \
  --key-env HYBRID_AGE_EXTRACT_KEY \
  --json-out "$EVAL_DIR/2026-11-18-paraphrase-rerun-nemotron-lightning-free.json"

echo "===== Running Longcat 2.5 on paraphrase ====="
python3 scripts/spec3_bakeoff.py \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE" \
  --extract-mode rules_then_model \
  --extract-url "$NOUS_URL" \
  --extract-model "meituan/longcat-2.5:free" \
  --key-env NOUS_API_KEY \
  --json-out "$EVAL_DIR/2026-11-18-paraphrase-rerun-longcat-2.5-free.json"

echo "===== Running Step 3.7 Flash on paraphrase ====="
python3 scripts/spec3_bakeoff.py \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE" \
  --extract-mode rules_then_model \
  --extract-url "$NOUS_URL" \
  --extract-model "stepfun/step-3.7-flash:free" \
  --key-env NOUS_API_KEY \
  --json-out "$EVAL_DIR/2026-11-18-paraphrase-rerun-step-3.7-flash-free.json"

docker rm -f hermes-paraphrase-rerun-pg

echo "===== Done ====="
echo "Results in $EVAL_DIR/2026-11-18-paraphrase-rerun-*.json"
