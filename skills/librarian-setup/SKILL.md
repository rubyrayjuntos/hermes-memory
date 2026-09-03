---
name: librarian-setup
description: "Walks hybrid-age setup; user seeds memory in chat."
version: 0.1.0
author: Ray Swan (rubyrayjuntos)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [memory, hybrid-age, setup, tutorial]
    category: ai-systems
---

# Librarian Setup

Guide the user through a **real** local stack (Docker Postgres + AGE + pgvector,
Ollama embeddings, hybrid-age provider). Name every moving part. Do not do it
for them and call it magic. Hermes names the architecture; they run it. Then
**they** seed memory in this chat — no Zephyr, no `MEMORY.md` replacement.

## When to Use

- "Set up the Librarian" / hybrid-age / hermes-memory
- First clone, empty graph, or "is this provider on?"
- After a failed install — start from the failed step, do not restart the sermon

Don't use for: codebase ingest only, AGE internals, stuffing `MEMORY.md`.

## Stance

Do not hide complexity, and do not complete the stack as a silent favor.
Name each piece and where it sits in Hermes: `MEMORY.md` / `USER.md` (always-on
snapshot), `session_search` (history when asked), this provider (retrieval +
graph on loopback Postgres). Then the next command. Never collect the
Postgres password in chat. If they stall, name the piece and the check.

## How to Run

Use `terminal` for checks and non-secret commands. Password and `.env` edits
happen in **their** terminal (`getpass` / editor).

```
terminal(command="hermes memory status", timeout=30)
terminal(command="docker compose version", timeout=15)
terminal(command="ollama list", timeout=15)
```

## Procedure

Skip any step whose check already passes. Say the skip.

① **Boundary (once).** Unofficial `hybrid-age` provider. Native `MEMORY.md` /
`USER.md` still load. `session_search` is history. Obsidian is notes.
`hermes backup` does not dump this database. Loopback only. Not a host.

② **Docker.** Needed so Postgres 17 + Apache AGE + pgvector listen on
`127.0.0.1:5450`. Check `docker compose version`. If missing, point at Docker
Desktop / engine docs and wait. Completion: compose command exists.

③ **Ollama + `nomic-embed-text`.** Local 768-dim embeddings; recall stays on
the machine. `ollama list` should show `nomic-embed-text`. If not:
`terminal(command="ollama pull nomic-embed-text", timeout=600)`. Completion:
the model is listed.

④ **Clone + `.env`.** If they are not already in the hermes-memory repo:
`git clone https://github.com/rubyrayjuntos/hermes-memory.git` then `cd`.
`cp .env.example .env`. They set `HERMES_PG_PASSWORD` and the same password in
`HYBRID_AGE_DSN` (replace `***`) **in their editor**. Never echo the password.
Completion: they say the file is saved.

⑤ **Install.** From the repo: `pip install -e '.[dev]'` then
`hermes-memory-install` in **their** terminal (it prompts for the DSN password).
That starts compose, wires `memory.provider=hybrid-age`, copies this skill,
starts viz on `127.0.0.1:7890`. Completion: install prints verify PASS or they
run `hermes-memory-verify` (CI synthetic — not their memory). If verify fails,
stay on this step; do not invent recall.

⑥ **New session if needed.** Provider + skill load at session start. If install
just ran, tell them to **start a new session** and load this skill again, then
jump to ⑦.

⑦ **User seed.** Ask for **three durable facts** (project, convention, thing
they always re-explain). They type them as normal messages **here**. Do not
rewrite into `MEMORY.md`. Do not invent examples. Those turns are the ingest.

⑧ **Canary.** New Hermes session, ask one fact in different words. Pass =
correct. Fail = status + Ollama; empty prefetch is a signal, not a guess.

⑨ **Why (only if they ask).** Vector = nearby chunks. Graph = the walk those
turns just created. Fountain `http://127.0.0.1:7890/api/librarian/pane` is
optional (Garden: drag to orbit). Recall must work with the pane closed.

## Pitfalls

- Do not skip naming Docker / Ollama / password / plugin. Hiding them is the
  bait-and-switch this crowd has already had.
- Verify's Zephyr/Atlas rows are CI. Not product memory. Hide-bench on Fountain.
- Prefetch `""` on failure (Hermes 8s cap).
- One profile, one DSN. No shared `HERMES_HOME`.
- No secrets in chat "so they get embedded."

## Verification

- Each used moving part had a named check (or an explicit skip)
- `hermes memory status` → hybrid-age
- Three user-authored facts in the transcript
- User has the new-session canary (cannot finish it in the setup session)
