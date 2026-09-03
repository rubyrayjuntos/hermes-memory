# Security Policy

## Supported versions

This project is **Alpha** (`0.1.x` on `main`). Only the latest `main` is supported.

## Reporting a vulnerability

Do **not** open a public GitHub issue for security reports.

- Prefer [GitHub private vulnerability reporting](https://github.com/rubyrayjuntos/hermes-memory/security/advisories/new)
- Or email **rswan@rswan.org** (same contact as the Code of Conduct)

Include: affected version or commit, what you did, and what you observed. We will acknowledge reports and coordinate a fix before any public disclosure.

## What this software assumes

Postgres and the viz API bind **loopback only** (`127.0.0.1`). They are not a public multi-tenant service. Treat the machine as the trust boundary: anyone who can open `http://127.0.0.1:7890` or connect to `127.0.0.1:5450` can read memory data.

Do not put real secrets in issues, PRs, or `config.yaml`. DSNs and embed keys belong in environment variables (see `.env.example`).
