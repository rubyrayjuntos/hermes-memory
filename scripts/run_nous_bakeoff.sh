#!/usr/bin/env bash
set -euo pipefail

# Source Nous API key
source ~/.hermes/profiles/librarian/cache/scratch/nous-bakeoff.env

# Set up paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
EVAL_DIR="$ROOT_DIR/docs/evals"

# Throwaway DB setup
export EVAL_DSN="postgres://hermes:dev_password_12345@127.0.0.1:5455/hermes_spec3_nous_eval"

# Create throwaway DB if needed
echo "Setting up throwaway DB..."
docker rm -f hermes-spec3-nous-pg 2>/dev/null || true
docker run -d --name hermes-spec3-nous-pg \
  -e POSTGRES_USER=hermes \
  -e POSTGRES_PASSWORD=dev_password_12345 \
  -e POSTGRES_DB=hermes_spec3_nous_eval \
  -p 127.0.0.1:5455:5432 \
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
echo "Running migrations..."
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src"
python3 scripts/migrate.py --dsn "$EVAL_DSN"

# Golden fixtures
GOLDEN_FIXTURE="$ROOT_DIR/tests/fixtures/golden_conversation.json"
PARAPHRASE_FIXTURE="$ROOT_DIR/tests/fixtures/paraphrase_slice.json"

# Models to test
NOUS_URL="https://inference-api.nousresearch.com/v1"

# Longcat 2.5
echo "===== Running Longcat 2.5 on golden ====="
python3 "$SCRIPT_DIR/spec3_bakeoff.py" \
  --dsn "$EVAL_DSN" \
  --fixture "$GOLDEN_FIXTURE" \
  --extract-mode rules_then_model \
  --extract-url "$NOUS_URL" \
  --extract-model "meituan/longcat-2.5:free" \
  --schema json_object \
  --key-env NOUS_API_KEY \
  --json-out "$EVAL_DIR/2026-11-18-spec3-longcat-2.5-free-json_object-golden.json"

echo "===== Running Longcat 2.5 on paraphrase ====="
python3 "$SCRIPT_DIR/spec3_bakeoff.py" \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE_FIXTURE" \
  --extract-mode rules_then_model \
  --extract-url "$NOUS_URL" \
  --extract-model "meituan/longcat-2.5:free" \
  --schema json_object \
  --key-env NOUS_API_KEY \
  --json-out "$EVAL_DIR/2026-11-18-spec3-longcat-2.5-free-json_object-paraphrase.json"

# Step 3.7 Flash
echo "===== Running Step 3.7 Flash on golden ====="
python3 "$SCRIPT_DIR/spec3_bakeoff.py" \
  --dsn "$EVAL_DSN" \
  --fixture "$GOLDEN_FIXTURE" \
  --extract-mode rules_then_model \
  --extract-url "$NOUS_URL" \
  --extract-model "stepfun/step-3.7-flash:free" \
  --schema json_object \
  --key-env NOUS_API_KEY \
  --json-out "$EVAL_DIR/2026-11-18-spec3-step-3.7-flash-free-json_object-golden.json"

echo "===== Running Step 3.7 Flash on paraphrase ====="
python3 "$SCRIPT_DIR/spec3_bakeoff.py" \
  --dsn "$EVAL_DSN" \
  --fixture "$PARAPHRASE_FIXTURE" \
  --extract-mode rules_then_model \
  --extract-url "$NOUS_URL" \
  --extract-model "stepfun/step-3.7-flash:free" \
  --schema json_object \
  --key-env NOUS_API_KEY \
  --json-out "$EVAL_DIR/2026-11-18-spec3-step-3.7-flash-free-json_object-paraphrase.json"

# Cleanup
docker rm -f hermes-spec3-nous-pg

echo "===== Done ====="
echo "Results written to $EVAL_DIR/"
