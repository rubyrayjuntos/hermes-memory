#!/usr/bin/env python3
"""Compile docs/design-system/tokens.json -> docs/design-system/tokens.css.

tokens.json is the single source of truth for The Hermes Librarian's colours,
type families, spacing, radii and shadows. Every surface that renders HTML
(the graph-explorer pane, generated docs/reports) loads the compiled
tokens.css and never re-declares a value.

    python scripts/build_design_tokens.py          # write tokens.css
    python scripts/build_design_tokens.py --check  # exit 1 if tokens.css is stale

Theme selection, in order of precedence:
  [data-theme="<id>"] on any element  -> that theme
  no data-theme, OS prefers light     -> the "parchment" theme
  otherwise                           -> the first theme ("ink")

Stdlib only; safe to run in CI without the dev extras.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOKENS_JSON = ROOT / "docs" / "design-system" / "tokens.json"
TOKENS_CSS = ROOT / "docs" / "design-system" / "tokens.css"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ALIAS_RE = re.compile(r"^\{([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\}$")
HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
LIGHT_THEME = "parchment"


class TokenError(ValueError):
    """tokens.json violates the grammar the design-system page reads."""


def _themed(value: Any, themes: list[str]) -> dict[str, str]:
    """Normalise a token value to {theme: raw}; a plain string is the first theme."""
    if isinstance(value, str):
        return {themes[0]: value}
    if isinstance(value, dict):
        unknown = set(value) - set(themes)
        if unknown:
            raise TokenError(f"unknown theme(s) {sorted(unknown)}")
        return {k: str(v) for k, v in value.items()}
    raise TokenError(f"bad value {value!r}")


def resolve_color(name: str, theme: str, table: dict[str, dict[str, str]], first: str) -> str:
    """Follow aliases to a literal colour for one theme (missing theme inherits the first)."""
    seen: list[str] = []
    cur = name
    while True:
        if cur in seen:
            raise TokenError(f"alias cycle: {' -> '.join(seen + [cur])}")
        seen.append(cur)
        if cur not in table:
            raise TokenError(f"alias to missing token {cur!r} (from {name!r})")
        vals = table[cur]
        raw = vals.get(theme, vals.get(first))
        if raw is None:
            raise TokenError(f"{cur!r} has no value for {theme!r} or {first!r}")
        m = ALIAS_RE.match(raw)
        if not m:
            return raw
        cur = m.group(1)
        if len(seen) > 16:
            raise TokenError(f"alias chain too deep from {name!r}")


def validate(tokens: dict[str, Any]) -> None:
    themes = [t["id"] for t in tokens["color"]["themes"]]
    if not themes or len(set(themes)) != len(themes):
        raise TokenError("themes must be non-empty and unique")
    names: set[str] = set()
    for fam in ("color", "spacing", "radius", "shadow"):
        for tok in tokens.get(fam, {}).get("tokens", []):
            n = tok["name"]
            if not NAME_RE.match(n):
                raise TokenError(f"bad token name {n!r}")
            if n in names:
                raise TokenError(f"duplicate token name {n!r}")
            if not tok.get("usage"):
                raise TokenError(f"token {n!r} has no usage note")
            names.add(n)
    table = {t["name"]: _themed(t["value"], themes) for t in tokens["color"]["tokens"]}
    for n, vals in table.items():
        for raw in vals.values():
            if not (ALIAS_RE.match(raw) or HEX_RE.match(raw)):
                raise TokenError(f"{n!r}: colour must be #hex or {{alias}}, got {raw!r}")
        for th in themes:
            resolve_color(n, th, table, themes[0])


def _css_value(raw: str) -> str:
    m = ALIAS_RE.match(raw)
    return f"var(--{m.group(1)})" if m else raw.lower() if HEX_RE.match(raw) else raw


def _theme_decls(tokens: dict[str, Any], themes: list[str], theme: str) -> list[str]:
    """Declarations for one theme. The first theme declares everything; later
    themes declare overrides plus every alias (re-declared so it re-resolves)."""
    first = theme == themes[0]
    out: list[str] = []
    for fam in ("color", "shadow"):
        for tok in tokens.get(fam, {}).get("tokens", []):
            vals = _themed(tok["value"], themes)
            if theme in vals:
                raw = vals[theme]
            elif not first and ALIAS_RE.match(vals.get(themes[0], "")):
                raw = vals[themes[0]]
            else:
                continue
            out.append(f"  --{tok['name']}: {_css_value(raw)};")
    return out


def compile_css(tokens: dict[str, Any]) -> str:
    validate(tokens)
    themes = [t["id"] for t in tokens["color"]["themes"]]
    title = tokens.get("name", "Design system")
    lines = [
        f"/* {title} — generated from tokens.json */",
        "/* Do not edit: run `python scripts/build_design_tokens.py`. */",
        "",
        f':root, [data-theme="{themes[0]}"] {{',
        *_theme_decls(tokens, themes, themes[0]),
        "}",
    ]
    for th in themes[1:]:
        decls = _theme_decls(tokens, themes, th)
        lines += ["", f'[data-theme="{th}"] {{', *decls, "}"]
        if th == LIGHT_THEME:
            lines += [
                "",
                "@media (prefers-color-scheme: light) {",
                "  :root:not([data-theme]) {",
                *("  " + d for d in decls),
                "  }",
                "}",
            ]
    lines += ["", ":root {"]
    for fam in ("spacing", "radius"):
        for tok in tokens.get(fam, {}).get("tokens", []):
            lines.append(f"  --{tok['name']}: {tok['value']};")
    for key, stack in tokens["type"]["families"].items():
        lines.append(f"  --font-{key}: {stack};")
    lines.append("}")
    for group in tokens["type"]["groups"]:
        for st in group["styles"]:
            fam = st.get("family", group["family"])
            decl = [f"font-family: var(--font-{fam})", f"font-size: {st['fontSize']}"]
            if "lineHeight" in st:
                decl.append(f"line-height: {st['lineHeight']}")
            if "fontWeight" in st:
                decl.append(f"font-weight: {st['fontWeight']}")
            if "letterSpacing" in st:
                decl.append(f"letter-spacing: {st['letterSpacing']}")
            lines += ["", f".{st['name']} {{ " + "; ".join(decl) + "; }"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if tokens.css is out of date")
    args = ap.parse_args(argv)
    css = compile_css(json.loads(TOKENS_JSON.read_text(encoding="utf-8")))
    if args.check:
        current = TOKENS_CSS.read_text(encoding="utf-8") if TOKENS_CSS.exists() else ""
        if current != css:
            print(f"{TOKENS_CSS.relative_to(ROOT)} is stale: run scripts/build_design_tokens.py")
            return 1
        print("tokens.css up to date")
        return 0
    TOKENS_CSS.write_text(css, encoding="utf-8")
    print(f"wrote {TOKENS_CSS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
