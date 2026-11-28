#!/bin/bash
# Periodically catches tiered_off/tiered_on generation + judging up to
# whatever the ingest process has landed so far, until ingest's checkpoint
# shows all 500 done. Each inner call is itself resumable (skips
# already-done question_ids), so a crash/restart of THIS loop loses
# nothing either.
set -u
cd /home/rswan/.hermes/profiles/linkibinki/cache/scratch
export OPENROUTER_API_KEY="$(grep '^OPENROUTER_API_KEY=' ~/.hermes/.env | cut -d= -f2-)"
export HYBRID_AGE_DSN="$(cat /tmp/.eval_dsn_longmemeval_500)"
export HYBRID_AGE_DSN_LINKIBINKI="$HYBRID_AGE_DSN"

while true; do
    DONE_COUNT=$(python3 -c "import json; print(len(json.load(open('lme_full_ingest_checkpoint.json'))['done']))" 2>/dev/null || echo 0)
    echo "=== $(date -Iseconds) catch-up pass, ingest has $DONE_COUNT/500 done ==="

    python3 lme_generate_tiered.py --condition tiered_off
    python3 lme_generate_tiered.py --condition tiered_on
    python3 lme_judge.py --condition tiered_off
    python3 lme_judge.py --condition tiered_on

    if [ "$DONE_COUNT" -ge 500 ]; then
        echo "=== ingest complete, running one final catch-up and exiting ==="
        python3 lme_generate_tiered.py --condition tiered_off
        python3 lme_generate_tiered.py --condition tiered_on
        python3 lme_judge.py --condition tiered_off
        python3 lme_judge.py --condition tiered_on
        break
    fi
    sleep 900
done
echo "CATCHUP_LOOP_DONE"
