"""Spec 5 T5.5: artifact verification.

Same rule as T5.4's status sources: an artifact counts as verified only
from a check the code itself runs -- confirming a file exists (and its
hash matches), or that a commit SHA is a real object in an allowlisted
repo. The assistant's own "I wrote X" never counts; it isn't consulted
at all here. A check that can't be run (no allowlisted repo, an
unsupported kind like 'pr'/'eval' with no network access, or a
malformed ref) leaves the artifact unverified -- never silently marked
passed.

No file contents are ever read into memory for storage -- only a path
existence check and a sha256 hex digest (a path and a hash, never
bytes going anywhere but through hashlib).
"""
from __future__ import annotations

import os

from hermes_memory.work_artifact_verify import verify_one_artifact

REPO = None  # set per-test to a tmp_path-based repo root


def _write(repo_root, rel_path: str, content: str = "hello") -> None:
    full = os.path.join(repo_root, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


def test_existing_file_is_verified_with_a_hash(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "src/thing.py", "print('hi')\n")
    checked, exists_now, content_hash = verify_one_artifact(
        kind="file", ref="src/thing.py", repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is True
    assert exists_now is True
    assert content_hash is not None
    import hashlib
    assert content_hash == hashlib.sha256(b"print('hi')\n").hexdigest()


def test_missing_file_is_verified_as_not_existing(tmp_path):
    repo_root = str(tmp_path)
    checked, exists_now, content_hash = verify_one_artifact(
        kind="file", ref="src/does_not_exist.py", repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is True
    assert exists_now is False
    assert content_hash is None


def test_path_traversal_is_rejected_and_never_checked(tmp_path):
    repo_root = str(tmp_path)
    secret_dir = tmp_path.parent / "secret"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "passwd").write_text("root:x:0:0")
    checked, exists_now, content_hash = verify_one_artifact(
        kind="file", ref="../secret/passwd", repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is False
    assert exists_now is None
    assert content_hash is None


def test_repo_not_in_allowlist_is_never_checked(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "src/thing.py")
    checked, exists_now, content_hash = verify_one_artifact(
        kind="file", ref="src/thing.py", repo=repo_root, repo_roots=["/some/other/allowlisted/repo"],
    )
    assert checked is False
    assert exists_now is None


def test_real_commit_sha_is_verified_against_git(tmp_path):
    import subprocess
    repo_root = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo_root], check=True)
    subprocess.run(["git", "-C", repo_root, "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", repo_root, "config", "user.name", "t"], check=True)
    _write(repo_root, "README.md", "x")
    subprocess.run(["git", "-C", repo_root, "add", "."], check=True)
    subprocess.run(["git", "-C", repo_root, "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    checked, exists_now, content_hash = verify_one_artifact(
        kind="commit", ref=sha, repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is True
    assert exists_now is True
    assert content_hash is None  # commits are never hashed, only files


def test_fabricated_commit_sha_is_verified_as_not_existing(tmp_path):
    import subprocess
    repo_root = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo_root], check=True)
    checked, exists_now, _ = verify_one_artifact(
        kind="commit", ref="deadbeef" * 5, repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is True
    assert exists_now is False


def test_malformed_commit_ref_is_never_checked(tmp_path):
    repo_root = str(tmp_path)
    checked, exists_now, _ = verify_one_artifact(
        kind="commit", ref="HEAD; rm -rf /", repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is False
    assert exists_now is None


def test_pr_kind_has_no_available_check_and_stays_unverified(tmp_path):
    """Verifying a PR is real would require a network call, which T5.5
    explicitly forbids. No check exists for this kind -- it must never
    be silently marked passed."""
    repo_root = str(tmp_path)
    checked, exists_now, _ = verify_one_artifact(
        kind="pr", ref="108", repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is False
    assert exists_now is None


def test_eval_kind_has_no_available_check_and_stays_unverified(tmp_path):
    repo_root = str(tmp_path)
    checked, exists_now, _ = verify_one_artifact(
        kind="eval", ref="run-42", repo=repo_root, repo_roots=[repo_root],
    )
    assert checked is False
    assert exists_now is None


def test_assistant_claiming_a_file_was_written_is_never_consulted():
    """verify_one_artifact has no text/claim input at all -- there is no
    parameter through which "I wrote X" could reach it, by construction."""
    import inspect
    params = set(inspect.signature(verify_one_artifact).parameters)
    assert params == {"kind", "ref", "repo", "repo_roots"}
