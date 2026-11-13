# Release-Gate Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make clone-first installation and uninstall safe, deterministic, testable, and ready for a disposable release-gate run.

**Architecture:** A non-secret atomic installation record is the authority for owned paths and installed topology. Installer and uninstaller share focused state/config helpers; destructive removal fails closed and the gate uses temporary homes plus isolated fakes before any optional live phase.

**Tech Stack:** Python 3.11+, argparse, pathlib, JSON, subprocess, pytest, setuptools.

**Spec:** `docs/superpowers/specs/2026-11-13-release-gate-readiness-design.md`

## Global Constraints

- Preserve the pinned-Git-tree, clone-first installation contract.
- Never put a password or full DSN in the install record or output.
- Never default destructive removal to `:5450/hermes_memory` or another guessed database.
- Preserve unrelated YAML and environment-file content.
- Do not touch the real Hermes home, live Compose projects, or live databases in automated tests.
- Preserve the existing uncommitted agent-identity isolation changes.
- Do not commit, push, publish, or run a live destructive gate without explicit approval.

## Review Focus

- Missing/corrupt install record must refuse database deletion; Task 2 tests both.
- Later user edits to `.env` or YAML must survive uninstall; Task 3 tests value-sensitive cleanup.
- Compose/config/drop failures must produce nonzero status and retain retry state; Task 3 tests aggregation.
- Wheel invocation outside a Git checkout must fail before mutation with clone-first guidance; Task 1 tests preflight order.
- Provider discovery and isolated install/restart/recall/uninstall/reinstall orchestration must be represented in the release gate; Tasks 4 and 5 test these boundaries.

---

### Task 1: Installation state and clone-first preflight

**Files:**
- Create: `src/hermes_memory/install_state.py`
- Modify: `src/hermes_memory/install_cli.py`
- Create: `tests/test_install_state.py`
- Modify: `tests/test_install_pin.py`

**Interfaces:**
- Produces: `InstallState`, `load_install_state(path)`, `write_install_state(path, state)`, `install_state_path(home)`, and `require_git_checkout(repo)`.
- Consumes: existing install constants and pinned ref/SHA.

- [ ] Write failing tests proving atomic non-secret round-trip, corrupt-record rejection, and non-checkout preflight before mutation.
- [ ] Run `pytest -q tests/test_install_state.py tests/test_install_pin.py` and confirm the new tests fail for missing interfaces.
- [ ] Implement the state model, atomic JSON write, strict validation, and checkout preflight.
- [ ] Integrate state writing into installation without storing the DSN/password.
- [ ] Run `pytest -q tests/test_install_state.py tests/test_install_pin.py` and confirm success.

### Task 2: Fail-closed uninstall target and ownership resolution

**Files:**
- Modify: `src/hermes_memory/uninstall_cli.py`
- Create: `tests/test_uninstall_cli.py`

**Interfaces:**
- Consumes: `InstallState` and `load_install_state` from Task 1.
- Produces: `resolve_uninstall_target(args, state)` and `build_compose_down_command(state)`.

- [ ] Write failing tests for explicit DSN, recorded target, missing/corrupt record, `--keep-db`, redacted display, installed `:5452` targeting, and recorded Compose shutdown.
- [ ] Run the focused tests and verify failures describe the unsafe current fallback/missing helpers.
- [ ] Refactor CLI parsing into `main(argv=None, *, home=None, runner=subprocess.run) -> int` and implement fail-closed resolution.
- [ ] Stop the recorded Compose project before requested database removal.
- [ ] Run focused tests and confirm success.

### Task 3: Owned cleanup, preservation, and truthful failure status

**Files:**
- Create: `src/hermes_memory/config_cleanup.py`
- Modify: `src/hermes_memory/uninstall_cli.py`
- Modify: `src/hermes_memory/install_cli.py`
- Modify: `tests/test_uninstall_cli.py`
- Create: `tests/test_config_cleanup.py`

**Interfaces:**
- Produces: `remove_owned_env_values(path, expected)`, `remove_hybrid_age_config(path)`, and aggregated uninstall result handling.
- Consumes: owned paths and expected non-secret values from `InstallState`.

- [ ] Write failing tests that preserve unrelated/comment/later-modified values, remove only owned values, default to plugin/skill cleanup, support keep flags, retain the record on failure, and return nonzero for any required failure.
- [ ] Run focused tests and verify the intended failures.
- [ ] Implement value-sensitive environment/YAML cleanup and exact-path owned artifact removal.
- [ ] Aggregate step failures; emit complete/incomplete truthfully and remove the record only on full success.
- [ ] Run focused tests and confirm success.

### Task 4: Provider discovery and packaging contract

**Files:**
- Create: `tests/test_provider_registration.py`
- Modify: `tests/test_install_pin.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `hermes_memory.register`, project entry-point metadata, and clone-first preflight.
- Produces: tested discovery contract and documented distribution boundary.

- [ ] Write failing/characterization tests loading the entry point, registering exactly one provider, checking wheel metadata, and confirming non-checkout guidance.
- [ ] Run focused tests and validate the behavior.
- [ ] Make only the production changes needed by those tests.
- [ ] Document clone-first installation, owned state, safe uninstall flags, failure semantics, and release-gate command; add required changelogs.
- [ ] Run focused tests and confirm success.

### Task 5: Disposable release-gate harness

**Files:**
- Create: `scripts/release_gate.py`
- Create: `tests/test_release_gate.py`
- Modify: `README.md`
- Modify: `RELEASE_CRITERIA.md`

**Interfaces:**
- Produces: `python scripts/release_gate.py` for static/disposable checks and `--live` for an explicitly isolated Docker/Ollama lifecycle.
- Consumes: installer/uninstaller commands and existing `hermes-memory-verify` behavior.

- [ ] Write failing tests proving the default gate uses a temporary home, validates entry points/wheel/tests, never invokes live deletion, and rejects non-isolated live targets.
- [ ] Run focused tests and verify failures.
- [ ] Implement the default gate and guarded live orchestration with command transcripts and redacted output.
- [ ] Update release criteria as readiness evidence only; do not mark a live/user-facing row Met without a level-6 artifact.
- [ ] Run `pytest -q`, `ruff check src tests scripts/release_gate.py`, `mypy`, wheel build/inspection, and the non-live release gate.

### Task 6: Final safety review and handoff

**Files:**
- Review all changed files; modify only for findings proven by regression tests.

**Interfaces:**
- Consumes: all previous task interfaces.
- Produces: review findings, final validation evidence, and explicit remaining live/user actions.

- [ ] Review the complete diff for destructive targeting, secret exposure, path ownership, subprocess error handling, and compatibility.
- [ ] Add a failing regression test for each Critical/Important finding before fixing it.
- [ ] Re-run the complete validation set and `git diff --check`.
- [ ] Report changed files, tests, local/static/live validation boundaries, commit/push status, and the exact command requiring user approval for the live gate.

## Changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 0.1 | 2026-11-13 | 2026-11-13 | OpenAI Codex | Initial approved implementation plan. |
