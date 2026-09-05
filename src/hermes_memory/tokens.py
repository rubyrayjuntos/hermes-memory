"""Token counting for the injection knapsack (shared by prefetch and bench)."""
from __future__ import annotations

# <PAST_CONTEXT> is consumed by Hermes, not a single vendor tokenizer.
# Librarian default is Nemotron; other profiles use Grok / Claude / GPT / DeepSeek.
# cl100k_base is exact for GPT-4-class and an approximation elsewhere. Pack to
# 90% of the nominal cap so a GOSP/code-dense turn near 1,100 tokens has slack
# if the consumer tokenizer splits richer than cl100k.
TOKENIZER_SLACK = 0.90
INJECTION_HARD_CAP = 1200

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text or ""))

except ImportError:  # pragma: no cover

    def count_tokens(text: str) -> int:
        return max(1, len(text or "") // 4)


def injection_token_cap(max_tokens: int, hard_cap: int = INJECTION_HARD_CAP) -> int:
    return max(1, int(min(int(max_tokens), int(hard_cap)) * TOKENIZER_SLACK))
