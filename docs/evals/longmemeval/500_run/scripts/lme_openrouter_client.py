"""Shared OpenRouter client + spend-cap enforcement for the LongMemEval run.

Budget (per explicit user instruction): $20 total.
- 3 main conditions (no_memory, tiered_off, tiered_on) on all 500: hard cap $15.
- full_context on the fixed 50-question subset only: hard cap $12.
Each condition tracks its own running spend in its own ledger file so the
two caps are enforced independently and a crash/resume doesn't lose the
running total.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LEDGER_DIR = Path("/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_ledgers")
LEDGER_DIR.mkdir(exist_ok=True)

# anthropic/claude-sonnet-5 via OpenRouter, confirmed 2026-11-28 (web_search):
# $2.00 / 1M input tokens, $10.00 / 1M output tokens.
ANSWER_MODEL = "anthropic/claude-sonnet-5"
ANSWER_INPUT_PRICE_PER_TOK = 2.00 / 1_000_000
ANSWER_OUTPUT_PRICE_PER_TOK = 10.00 / 1_000_000

# official LongMemEval judge model_zoo['gpt-4o'] entry, routed via OpenRouter
# (same model id the dry run used: gpt-4o-2024-08-06).
JUDGE_MODEL = "openai/gpt-4o-2024-08-06"
# Rough estimate for cap purposes (actual tokens come back in the response
# and are what's actually accounted against the ledger).
JUDGE_INPUT_PRICE_PER_TOK = 2.50 / 1_000_000
JUDGE_OUTPUT_PRICE_PER_TOK = 10.00 / 1_000_000


class SpendCapHit(Exception):
    pass


class Ledger:
    """One ledger per condition (or 'judge'), so caps are independent and
    a resumed run picks up the real running total, not zero."""

    def __init__(self, name: str, cap_usd: float):
        self.path = LEDGER_DIR / f"{name}.json"
        self.cap_usd = cap_usd
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"total_usd": 0.0, "calls": 0, "events": []}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))

    @property
    def total_usd(self) -> float:
        return self.data["total_usd"]

    def check_room(self, estimated_usd: float) -> None:
        if self.total_usd + estimated_usd > self.cap_usd:
            raise SpendCapHit(
                f"ledger {self.path.name}: would exceed cap (${self.cap_usd:.2f}); "
                f"current ${self.total_usd:.4f} + est ${estimated_usd:.4f}"
            )

    def record(self, usd: float, meta: dict) -> None:
        self.data["total_usd"] += usd
        self.data["calls"] += 1
        self.data["events"].append({"usd": usd, "ts": time.time(), **meta})
        self._save()


def call_openrouter(model: str, prompt: str, max_tokens: int, temperature: float = 0.0,
                     retries: int = 5) -> dict:
    """One chat completion call. Returns dict with content, prompt_tokens,
    completion_tokens, elapsed_s. Raises on persistent failure."""
    api_key = os.environ["OPENROUTER_API_KEY"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    last_exc = None
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=120)
            if resp.status_code == 429:
                time.sleep(min(60, 2 ** attempt))
                continue
            resp.raise_for_status()
            data = resp.json()
            elapsed = time.monotonic() - t0
            choice = data["choices"][0]
            usage = data.get("usage", {})
            return {
                "content": choice["message"]["content"].strip(),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "elapsed_s": elapsed,
                "model": data.get("model", model),
            }
        except Exception as e:
            last_exc = e
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"OpenRouter call failed after {retries} retries: {last_exc}")
