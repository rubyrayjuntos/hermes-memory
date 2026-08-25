# Apache AGE 1.7.0 for PostgreSQL 17 + pgvector.
#
# Image pin decision (verified against Docker Hub API, 2026-08-24):
#   apache/age:release_PG17_1.7.0 does NOT exist. Closest PG17 release tag is
#   apache/age:release_PG17_1.6.0 — pinned below. Recorded in docs/plans/v0.1.md
#   appendix "Image pin"; bump to release_PG17_1.7.x if/when published.
FROM apache/age:release_PG17_1.6.0

# pgvector via apt (PGDG package). Clean lists to keep the layer thin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*
