"""Turn-content filters shared by provider, extract_nouns, and backfill."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    kind: str


def _coalesce(kind: list[str]) -> list[Segment]:
    if not kind:
        return []
    segs: list[Segment] = []
    start = 0
    for i in range(1, len(kind) + 1):
        if i == len(kind) or kind[i] != kind[start]:
            segs.append(Segment(start, i, kind[start]))
            start = i
    return segs


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 0
    for line in text.splitlines(keepends=True):
        spans.append((start, start + len(line), line))
        start += len(line)
    return spans


def _mark_range(kind: list[str], start: int, end: int, label: str) -> None:
    for j in range(start, end):
        if kind[j] == "authored":
            kind[j] = label


def _mark_line_runs(
    text: str,
    kind: list[str],
    predicate,
    label: str,
    *,
    min_lines: int = 1,
) -> None:
    run_start: int | None = None
    run_end = 0
    run_count = 0
    for start, end, line in _line_spans(text):
        body = line.rstrip("\r\n")
        if body and predicate(body):
            if run_start is None:
                run_start = start
            run_end = end
            run_count += 1
            continue
        if run_start is not None and run_count >= min_lines:
            _mark_range(kind, run_start, run_end, label)
        run_start = None
        run_count = 0
    if run_start is not None and run_count >= min_lines:
        _mark_range(kind, run_start, run_end, label)


_EMAIL_HEADER_RE = re.compile(r"^(From|To|Subject|Date):")


def _mark_email(text: str, kind: list[str]) -> None:
    for start, _end, line in _line_spans(text):
        if _EMAIL_HEADER_RE.match(line):
            _mark_range(kind, start, len(text), "pasted")
            return


_LOG_LINE_RE = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"|\[?(?:INFO|DEBUG|WARNING|WARN|ERROR|TRACE|CRITICAL|FATAL)\]?(?:[:\s]|$)"
    r")"
)


def _mark_logs(text: str, kind: list[str]) -> None:
    _mark_line_runs(text, kind, lambda line: bool(_LOG_LINE_RE.match(line)), "pasted", min_lines=3)


def _mark_quotes(text: str, kind: list[str]) -> None:
    _mark_line_runs(text, kind, lambda line: line.lstrip().startswith(">"), "quote")


_SIG_LINE_RE = re.compile(r"^(?:-- |Sent from\b)")


def _mark_signature(text: str, kind: list[str]) -> None:
    for start, _end, line in _line_spans(text):
        if _SIG_LINE_RE.match(line):
            _mark_range(kind, start, len(text), "pasted")
            return


def _mark_tables(text: str, kind: list[str]) -> None:
    _mark_line_runs(
        text, kind, lambda line: line.lstrip().startswith("|"), "pasted", min_lines=2
    )


def _mark_code(text: str, kind: list[str]) -> None:
    i = 0
    n = len(text)
    while i < n:
        start = text.find("```", i)
        if start < 0:
            return
        end = text.find("```", start + 3)
        if end < 0:
            _mark_range(kind, start, n, "code")
            return
        close = end + 3
        _mark_range(kind, start, close, "code")
        i = close


_SHELL_PROMPT_RE = re.compile(r"^[\w.-]+@[\w.-]+\S*\s+\$")


def _mark_shell_session(text: str, kind: list[str]) -> None:
    for start, _end, line in _line_spans(text):
        if _SHELL_PROMPT_RE.match(line):
            _mark_range(kind, start, len(text), "code")
            return


def segment_turn(text: str) -> list[Segment]:
    if not text:
        return []
    kind = ["authored"] * len(text)
    _mark_code(text, kind)
    _mark_shell_session(text, kind)
    _mark_email(text, kind)
    _mark_logs(text, kind)
    _mark_tables(text, kind)
    _mark_signature(text, kind)
    _mark_quotes(text, kind)
    return _coalesce(kind)

TURN_MIN_CHARS = 40

_NOISE_RE = re.compile(
    r"^("
    r"ok(ay)?|thanks?( you)?|thx|ty|np|"
    r"yes|no|sure|got it|done|cool|nice|great|"
    r"continue|please|exit|cancel|stop|quit|"
    r"yeah|yep|nope|alright"
    r")[\s\.\!\?]*$",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(r"(?<!\w)(?:i(?:['’]m)?|my|we)(?!\w)", re.IGNORECASE)


def _is_noise(
    content: str, *, min_chars: int = TURN_MIN_CHARS, role: str | None = None
) -> bool:
    return _noise_reason(content, min_chars=min_chars, role=role) is not None


def _noise_reason(
    content: str, *, min_chars: int = TURN_MIN_CHARS, role: str | None = None
) -> str | None:
    stripped = (content or "").strip()
    if not stripped:
        return "empty"
    if _NOISE_RE.match(stripped):
        return "noise_pattern"
    if len(stripped) >= min_chars:
        return None
    if role != "user":
        return "short_non_user"
    if _FIRST_PERSON_RE.search(stripped):
        return None
    from .reception import extract_assertions

    return "short_user_without_assertion" if not extract_assertions(stripped) else None
