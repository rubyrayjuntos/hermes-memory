# The Hermes Librarian — design system

**Source of truth for every surface's look:** the CLI, the Hermes skin, repo
documents, and the Graph Explorer pane. The PDF at the repo root
(`The Hermes Librarian — Brand Style Guide.pdf`) is the brief; this folder is
the decision. A browsable mirror is published as a Claude design-system
artifact, but this folder wins if the two ever differ.

| File | Role |
| --- | --- |
| `tokens.json` | **Edit this.** Colours (two themes: `ink` dark, `parchment` light), type, spacing, radii, shadow. |
| `tokens.css` | Generated: `python scripts/build_design_tokens.py`. Never hand-edit; CI fails if stale. |
| `components.css` | `hx-*` component classes. Uses only `var(--token)`, no raw colours. |
| `assets/` | Marks copied from the style guide PDF (raster; no vector sources yet). |

**Using it in HTML:** load `tokens.css` then `components.css`. Theme follows the OS
(`prefers-color-scheme`) unless an ancestor sets `data-theme="ink"` or `data-theme="parchment"`.

**Invariants** (enforced by `tests/property/test_design_tokens.py`): `tokens.css`
is compiled from `tokens.json`. The brand hexes match the style guide. Every text
pair is ≥4.5:1 and every mark/border ≥3:1 on all three surfaces in both themes.
`status-grounded` stays blue-dominant. `components.css` references only declared
tokens. The compiler resolves any acyclic alias graph and rejects cycles.

## Brand book

The Hermes Librarian is the brand of `hermes-memory`, the `hybrid-age` memory provider for Hermes Agent. One identity spans four surfaces: the CLI, the repo's documents, Hermes itself, and the database pane. The Caduceus is the sole primary mark. Gold and lapis are the colours. Classical serif lettering sits over plain modern UI. Surface-specific rules are in **Surfaces**.

### Names

| Thing | Write it as | Never |
| --- | --- | --- |
| Product / brand | **The Hermes Librarian** (first mention), *the Librarian* after | "Hermes Memory" as a product name, "HL" |
| Repository, package, commands | `hermes-memory`, `hermes-memory-install`, `hermes-memory-verify` … in `code` | the product name in a shell command |
| Provider id in Hermes config | `hybrid-age` | |
| CLI skin / handle | `librarian` (skin file), `librarian:` (tool prefix) | |

The package that ships is `hermes-memory` (`pyproject.toml`). The style guide's `pip install hermes-librarian` describes a planned rename. Do not print it until that package exists.

### Voice

- **Taglines.** Primary: *"The messenger never forgets."* Technical: *"Write → Index → Recall → Weed. The library that tends itself."* Native/kawaii: *"Your filing cabinet that fills its own shelves."* Use the primary in heroes and social posts, the technical one in the README header and `--help`, and the native one only next to the owl.
- **Library verbs for activity, plain words for state.** Activity uses *cataloging, shelving, cross-referencing, recalling, weeding, indexing memory*. State and fields are always literal and in `code`: `drain_status`, `beam_score`, `LEDGER`, `pending`, `leased`. The pane shows source fields under their source names; never rename a field into the metaphor.
- **Honest status.** Say *Alpha*, *unofficial community plugin*, *loopback only (127.0.0.1)* wherever install or trust is discussed. Never imply Nous Research endorsement.
- **Person and case.** Address the reader as *you*; the Librarian speaks in the third person ("the Librarian recalled 4 memories"). Use sentence case for headings and UI; UPPERCASE only in `label` and `inscription` styles.
- **Emoji.** 📜 is the Librarian's response label and README glyph. The owl frames (🦉) belong to the CLI spinner only. ⚕️ is banned: it is the single-snake medical rod, which is the wrong symbol. No decorative emoji in the pane or docs body.

### Colour

The brand palette is exact from the style guide: `brand-nous-gold` `brand-librarian-gold` `brand-lapis` `brand-parchment` `brand-ink` `brand-vector-dot` `brand-graph-edge` `brand-weed-red`. UI is built from the semantic tokens on top of them, never from raw brand tokens.

- **Two themes, one rule from the guide:** gold on dark, lapis on light. `accent` is Nous Gold on **Ink** and Lapis on **Parchment**. Pair every `accent` fill with `on-accent` text.
- Ground is `surface`. Panels are `surface-raised`. Code, terminals, inputs and tab tracks are `surface-sunken`. Text is `text` / `text-muted` on any of the three.
- `accent` is the only interactive colour: primary button, active tab, link, `focus-ring`. One primary button per panel.
- **Data colours carry the dual index.** `vector` marks pgvector/ANN things (dots). `graph-edge` marks AGE things (edges, traversal). They appear in charts, graph views and terminal highlights, never as UI chrome. `brand-vector-dot` is pure yellow, and the guide forbids pure yellow as a *brand* gold, so it never stands in for gold.
- `weed` covers discard, drift, retraction and failure. It is `brand-weed-red` on Parchment and lifted to `#e08a78` on Ink, where the original reads 2.5:1.
- **Status = mark + word.** Claim status and drain status always render through `hx-status`: a dot in `status-*` plus the literal status word. Grounded/done is **blue** (`graph-edge`), not green, so success and failure never differ only along red–green. Failure states use a square mark and pending/orphaned a hollow ring, so the three never depend on hue.
- `seal` (Librarian Gold, darkened on Parchment) is for meander rules, stamps and eyebrows only.
- `entity-person` … `entity-other` colour graph vertices, gems and type chips, always next to the type word. Each is ≥4.5:1 against `surface` (vertex initials are set in `surface` on the mark), and every pair is ≥20 ΔE apart in both themes.
- Contrast: every text token holds ≥4.5:1, and every `line-strong`, `focus-ring` and status mark holds ≥3:1, on all three surfaces in both themes. Darkened Parchment values of the data colours exist for this reason. `line` is decorative (below 3:1) and never the only edge of a control.

### Type

- `display` family (Cormorant Garamond → EB Garamond fallback) is for headlines, the seal and `inscription` eyebrows. It never appears below 13px or in controls.
- `sans` family (Inter) covers all UI and documentation body: `title`, `body`, `body-sm`, `label`, `metric`.
- `mono` family (JetBrains Mono, matching the Hermes TUI) covers commands, field names, `terminal` panes and anything copy-pasteable.
- All three are Google-hosted. Load them through `components.css` (or the same `@import`); no font files ship.
- Metric values use tabular figures. Labels and status words are `label`: 11px, 600, uppercase, 0.08em tracking.

### Space, shape, depth

- 4px grid: `space-1` … `space-12`. Panels pad `space-4` and sit `space-6` apart. Use a `space-4` side gutter at phone width.
- Corners stay restrained and classical: `radius-sm` for gems and code, `radius-md` for buttons, inputs, metrics and terminals, `radius-lg` for panels. `radius-pill` is reserved for chips, status pills and tab tracks.
- Separate with borders, not shadows. Panels take a 1px `line` border at rest. `shadow-raised` is only for things that float (sticky header, popovers).
- Ornament: one Greek-meander rule in `seal` may top a hero or a release note. Never tile it as a background.
- Motion: none in the pane beyond 120–180ms hover/selection transitions. Animation lives in the CLI spinner (see Surfaces).

### Iconography & marks

- **Caduceus only.** Always two snakes, wings, and a staff or scroll. Never one snake (Rod of Asclepius / ⚕️), a wingless staff, or a book alone.
- **Minimal icon** (`assets/minimal-icon.png`) is used at 16–32px: favicon, pane header, CLI-adjacent places. Its left snake reads as vector, its right as graph.
- **Primary mark** (`assets/primary-mark.jpg`) is for the README banner, docs and social, at 128px and up.
- **Seal** (`assets/seal.png`, `assets/seal-parchment.jpg`) goes on release stamps and avatars. It works as the library's checkout stamp.
- **Owl** (`assets/owl.png`) is secondary. It stays ≤32px, sits in a corner or the status line, and only ever appears beside the Caduceus, never alone on a seal, favicon or lockup. Give it a calm expression, a rounded silhouette, and 4–6 shapes.
- On dark grounds use Nous Gold marks; on light grounds use Lapis. Never neon or pure yellow.
- UI glyphs are the `hx-gem` (rotated square), status dots, and text. There is no icon font. Arrows are typographic (→).

### Components

Classes come from `components.css` and keep the pane's `hx-` prefix: `Panel` (`hx-panel`, `hx-header`, `hx-eyebrow`), `Button` (`hx-btn`, `hx-btn-ghost`, `hx-btn-danger`), `Chip` (`hx-chip`, `hx-gem`), `StatusBadge` (`hx-status[data-status]`), `Tabs` (`hx-tabs`, `hx-tab`), `Metric` (`hx-metric`), `Terminal` (`hx-term`), `Banner` (`hx-banner`). These are plain HTML/CSS with no JS bundle, so any surface (React pane, static docs page, `graph_api.py` HTML) can use them.

## Surfaces

Each surface gets the same tokens, adapted to what that medium can render.

### 1 · CLI (`hermes-memory-*` commands)

- Step lines follow the installer's existing format: `[n/N] Sentence-case action…` with a single `…` (U+2026). Sub-steps use `[5b/7]`. Keep it.
- Use the `terminal` mono style. Map ANSI colours to the Ink tokens: step counter `text-muted`, success `status-grounded`, warning `status-unconfirmed`, failure `weed`, product voice (`librarian:` prefix) `accent`. Respect `NO_COLOR` and non-TTY output by dropping colour, never words.
- Every status word is printed literally (`ok`, `warn`, `fail`, `pending`, `leased`) so meaning survives without colour.
- Machine output (`--json`) carries field names verbatim (`drain_status`, `beam_score`) and contains no brand voice.
- Redact secrets in any echoed DSN (`_redact_dsn`). Branding never outranks that.

### 2 · Hermes skin (`~/.hermes/skins/librarian.yaml`)

From the style guide (colours exact):

```yaml
name: librarian
description: The Hermes Librarian — gold + lapis, memory-aware
colors:
  banner_border: "#DD8E35"   # brand-nous-gold
  banner_title:  "#F7F3EB"   # brand-parchment
  banner_accent: "#1A3A5F"   # brand-lapis
  context_bar:   "#DD8E35"   # brand-nous-gold
spinner:
  thinking_verbs: ["cataloging", "shelving", "cross-referencing", "recalling", "weeding", "indexing memory"]
branding:
  agent_name: "Hermes Librarian"
  response_label: " 📜 Librarian "
  tool_activity_prefix: "librarian:"
```

`banner_accent` Lapis on the Ink terminal is 1.6:1. Use it for borders and fills only, never text.

**Spinner frames** (owl, ≤1 cell high, one verb per state):

| State | Verb | Frames |
| --- | --- | --- |
| Idle / ready | — | soft blink |
| Recall / prefetch | recalling | wing flutter + pulse |
| Index / embed | cataloging · indexing memory | page turn |
| Graph expansion | cross-referencing | two dots orbiting (vector · edge) |
| Write | shelving | brief placement, 2–3 frames |
| Maintenance | weeding | leaf fall, muted |
| Injected | — | blink + ✓, one-shot |

### 3 · Hermes context injection

The injected memory block is model-facing text, not UI. Keep it plain and stable. Brand voice never goes in the injected payload; only the `📜 Librarian` label and `librarian:` tool prefix appear in the human-visible transcript.

### 4 · Repository documents

- README header: primary mark banner, then `# 📜 The Hermes Librarian 📜`, then the technical tagline and the *Alpha / unofficial / loopback* line.
- Use GitHub-flavoured markdown only. Field names and commands go in `code`. Release-criteria rows are cited by ID (`D-MVP-*`, `RQ-PROD-*`, `OC-MVP-*`).
- Generated HTML reports and docs pages load `tokens.css` + `components.css`. They set `data-theme="parchment"` for print or light reading and `ink` by default.
- Release notes may carry the seal and one meander rule at the top.

### 5 · Database pane (graph explorer, `127.0.0.1:7890`)

- Shell: `hx-header` (minimal icon at 24px + `display` title + `hx-tabs`), then `hx-panel` grid on `surface`.
- **View reads source fields.** Every metric shows the source field it came from (`.hx-metric .src`, e.g. `drain_status`, `LEDGER`, `beam_score`). Never show a derived number without its source.
- Claim, drain/job and evidence states render only through `StatusBadge`. Evidence `live` maps to the grounded mark and `superseded_version` to the orphaned ring.
- Graph canvas: vertices are diamonds filled with `entity-*`. Edges take their claim's `status-*` colour, traversal/selection takes `accent`, and literals are dashed `line-strong`.
- Control actions (pane control is a Production criterion, not MVP) use `hx-btn-danger` for anything that weeds or discards, and always confirm.
