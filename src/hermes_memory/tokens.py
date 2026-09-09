"""Token counting for the injection knapsack (shared by prefetch and bench)."""
from __future__ import annotations

# <PAST_CONTEXT> is consumed by Hermes, not a single vendor tokenizer.
# Librarian default is Nemotron; other profiles use Grok / Claude / GPT / DeepSeek.
# cl100k_base is exact for GPT-4-class and an approximation elsewhere.
# TOKENIZER_SLACK is a hedge, not a measured error bar against Nemotron/Claude.
# 0.90 leaves ~11% room if the consumer splits richer than cl100k; it is not
# a solved per-profile count. Do not treat this as "tiktoken fixed the budget."
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


# Query-side window for ANN. Stored turns embed whole-text, so the query must
# see a comparable window: truncating the enriched query by tokens (not a
# magic char count) keeps both sides in the same geometry. 2048 sits well
# inside nomic-embed-text's 8192-token window and covers multi-turn enrichment.
QUERY_TOKEN_CAP = 2048


def truncate_tokens(text: str, max_tokens: int) -> str:
    """Short passthrough; long text cut to max_tokens (cl100k approx)."""
    text = text or ""
    if count_tokens(text) <= max_tokens:
        return text
    try:
        toks = _ENC.encode(text)[:max_tokens]
        return _ENC.decode(toks)
    except NameError:  # no tiktoken: char fallback mirrors count_tokens
        return text[: max_tokens * 4]
