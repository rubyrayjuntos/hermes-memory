"""Spec 5 T5.5: artifact verification.

Same rule as work_status.py's status sources: an artifact counts as
verified only from a check the code itself runs -- never from the
assistant's own account. There is no text/claim parameter anywhere in
this module; it cannot be influenced by "I wrote X" by construction.

Per the spec: verify refs only inside configured repo roots, using
subprocess argument arrays (never a shell), bounded timeouts, and
validated refs; never fetch or otherwise access the network. A kind
with no locally-runnable check (pr, eval -- confirming either is real
would require a network call) always returns checked=False: it must
stay unverified, never silently marked passed.

Only a path and a sha256 hex digest are ever produced for a file
artifact -- file bytes are read only long enough to hash them, never
stored or returned.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Tuple

_VERIFY_TIMEOUT_S = 5.0

# Deliberately strict: a 7-40 char hex string, the only shape a real git
# commit SHA (full or abbreviated) can take. Anything else is rejected
# before it ever reaches subprocess argv -- not because argv is
# shell-injectable (it isn't), but so a malformed ref is honestly
# "never checked" rather than passed through to git and misread.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

# No local, network-free check exists for these kinds in this codebase.
_UNCHECKABLE_KINDS = ("pr", "eval")


def _resolve_allowlisted_repo_root(repo: Optional[str], repo_roots: Sequence[str]) -> Optional[Path]:
    """repo must resolve to exactly one of the configured repo_roots
    (realpath comparison, so a symlink or trailing slash can't sneak a
    non-allowlisted directory past this check). None if not allowlisted
    or repo is missing."""
    if not repo:
        return None
    try:
        repo_real = Path(repo).resolve(strict=False)
    except OSError:
        return None
    for root in repo_roots:
        try:
            root_real = Path(root).resolve(strict=False)
        except OSError:
            continue
        if repo_real == root_real:
            return root_real
    return None


def _resolve_file_within_repo(repo_root: Path, ref: str) -> Optional[Path]:
    """repo_root/ref, rejecting (returning None for) any ref that
    escapes repo_root -- the path-traversal defense ('../../etc/passwd')."""
    if not ref or ref.startswith("/") or ".." in Path(ref).parts:
        return None
    candidate = (repo_root / ref).resolve(strict=False)
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        return None
    return candidate


def _file_hash(path: Path, hmac_key: Optional[bytes]) -> Optional[str]:
    """Reads the file only long enough to hash it -- the digest is the
    only thing that ever leaves this function; content never is.

    No hmac_key -> None, never a silent unkeyed fallback. A repo root
    can hold a .env or other secret file, and a plain sha256 of a short
    secret has the identical brute-force problem work_event.content_hash
    was fixed for; the same rule applies here."""
    if hmac_key is None:
        return None
    try:
        h = hmac.new(hmac_key, digestmod=hashlib.sha256)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _git_ref_exists(repo_root: Path, git_ref: str) -> Optional[bool]:
    """subprocess argv array, never a shell; bounded timeout; no network
    flags of any kind."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", git_ref],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_VERIFY_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def verify_one_artifact(
    *, kind: str, ref: str, repo: Optional[str], repo_roots: Sequence[str],
    hmac_key: Optional[bytes] = None,
) -> Tuple[bool, Optional[bool], Optional[str]]:
    """(checked, exists_now, content_hash).

    checked=False means no real check ran at all -- the caller must
    leave the artifact's exists_now/content_hash/verified_at untouched
    (unverified), never write a False/True result for a check that
    never happened. checked=True means a real, local, network-free
    check ran and exists_now holds its honest result.

    content_hash is populated for 'file' artifacts only, and only when
    a hmac_key is provided (see _file_hash) -- commits/branches/pr/eval
    never get a content hash; there is no file content to hash for
    those kinds."""
    if kind in _UNCHECKABLE_KINDS:
        return False, None, None

    repo_root = _resolve_allowlisted_repo_root(repo, repo_roots)
    if repo_root is None:
        return False, None, None

    if kind == "file":
        path = _resolve_file_within_repo(repo_root, ref)
        if path is None:
            return False, None, None
        exists = path.is_file()
        content_hash = _file_hash(path, hmac_key) if exists else None
        return True, exists, content_hash

    if kind == "commit":
        if not _COMMIT_SHA_RE.match(ref or ""):
            return False, None, None
        git_exists = _git_ref_exists(repo_root, f"{ref}^{{commit}}")
        if git_exists is None:
            return False, None, None
        return True, git_exists, None

    if kind == "branch":
        if not _BRANCH_NAME_RE.match(ref or ""):
            return False, None, None
        git_exists = _git_ref_exists(repo_root, f"refs/heads/{ref}")
        if git_exists is None:
            return False, None, None
        return True, git_exists, None

    return False, None, None
