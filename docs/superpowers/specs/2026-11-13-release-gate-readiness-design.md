# Release-gate readiness — design

**Status:** approved for implementation.

**Intent:** make the clone-first `hybrid-age` provider installable and removable without targeting unrelated data, and provide a disposable-environment gate that proves the lifecycle before a release.

## Goals

- Preserve the pinned-Git-tree installation contract for this release.
- Record non-secret ownership and topology state under `~/.hermes`.
- Make uninstall fail closed when its database target is not explicit or recorded.
- Stop the installed Compose service and remove only artifacts owned by the installation.
- Preserve unrelated configuration and environment-file content.
- Return nonzero from installation or removal when a required step fails.
- Exercise provider discovery and the install/uninstall lifecycle in isolated tests.
- Provide a disposable-home release-gate command whose destructive operations cannot reach the operator's real Hermes home or development database.

## Non-goals

- Publishing a package, tag, GitHub release, or container image.
- Deploying or deleting the operator's live resources.
- Converting this release to a self-contained PyPI-wheel installer.
- Multi-tenant/RLS support or a hosted database.
- Changing retrieval, scoring, or graph semantics beyond the existing identity-isolation fix.

## Approaches considered

1. **Repository-first hardening (selected).** Retain clone-first installation and make ownership, removal, errors, and lifecycle evidence explicit. This fits the existing pinned-SHA architecture and limits release risk.
2. **Self-contained wheel.** Bundle Docker, migrations, and skills in the wheel. This is a separate distribution redesign and would replace the current Git-pin contract.
3. **Minimal uninstall patch.** Correct the installed port and stop Compose. Rejected because it leaves ambiguous ownership, partial failures, and no release-gate evidence.

## Architecture

### Installation record

`hermes-memory-install` writes `~/.hermes/hermes-memory-install.json` atomically only from non-secret values. The record contains a schema version, installed SHA/ref, Compose project/container/file, database host/port/name/user, and owned plugin, skill, config, and environment paths. It never contains a password or full DSN.

The installer must be running from a Git checkout. If `REPO_ROOT/.git` is absent or the selected ref cannot be resolved, it exits nonzero with clone-first instructions before modifying Hermes state.

### Safe target resolution

Uninstall database coordinates come from, in order: explicit `--dsn`, the installation record, or no target. There is no `:5450/hermes_memory` fallback. Without an explicit/recorded target, destructive database removal is refused; `--keep-db` remains usable.

Before database removal the CLI prints a redacted `host:port/database` target. Non-interactive removal requires `--force`; interactive removal requires the existing confirmation.

### Owned cleanup

Uninstall stops the recorded Compose project using its recorded compose file before database/plugin cleanup. Full removal deletes owned plugin directories, setup-skill directories, and the Compose record by default. `--keep-plugin` and `--keep-db` preserve those categories. The legacy `--remove-plugin` spelling remains accepted as a no-op compatibility alias for the new default.

Environment cleanup removes only `HERMES_PG_PASSWORD`, `HYBRID_AGE_DSN`, `HYBRID_AGE_EMBED_URL`, `HYBRID_AGE_EMBED_MODEL`, and `HYBRID_AGE_GRAPH` when their current values match the installed configuration recorded during installation. Unrelated lines, comments, keys, and later user changes remain. YAML cleanup removes the `hybrid_age` block and changes `memory.provider` to `built-in` only when it still equals `hybrid-age`.

### Failure contract

Each required step contributes to an error list. A failed step is reported once with redacted details. The CLI prints `Uninstall incomplete` and returns nonzero when any required operation fails; it prints `Uninstall complete` only when all requested operations succeed. The ownership record remains when cleanup is incomplete so the operation can be retried.

Installation writes the ownership record after configuration/topology is known and updates its status atomically. On failure it reports owned artifacts and returns nonzero; it does not claim completion. Automatic rollback will be limited to files created during the failed run and will never drop a database.

### Release gate

Unit tests use temporary homes and fake command binaries to prove target resolution, ownership cleanup, failure propagation, config preservation, and provider registration without touching real state. A release-gate script first enforces those tests and packaging checks, then optionally runs the existing live Docker/Ollama verification when explicitly requested. Any destructive live phase requires a generated temporary home and an isolated database name.

## Constraints

- Python 3.11+.
- No secrets in the ownership record, logs, tests, or documentation.
- No broad recursive deletion; every removed path must come from a validated ownership record or a fixed Hermes-owned path.
- Existing uncommitted identity-isolation changes must be preserved.
- No commit, push, release, or live destructive execution without explicit user instruction.

## Changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 0.1 | 2026-11-13 | 2026-11-13 | OpenAI Codex | Approved repository-first release-gate design. |
