"""The built distribution exposes the Hermes provider discovery contract."""

import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import PathDistribution
from pathlib import Path
from zipfile import ZipFile

from hermes_memory.provider import HybridAgeMemoryProvider


def test_dev_dependencies_include_offline_wheel_build_requirements() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        dev = tomllib.load(f)["project"]["optional-dependencies"]["dev"]
    assert "setuptools>=68" in dev
    assert "wheel" in dev


def test_built_wheel_entry_point_registers_one_provider(tmp_path: Path, monkeypatch) -> None:
    # Loading the entry point calls hermes_memory.register(), which calls
    # load_config() with no explicit path -- that reads $HERMES_HOME/config.yaml
    # (defaulting to ~/.hermes) from the REAL ambient environment, since this
    # happens in-process rather than through a sandboxed subprocess (unlike
    # scripts/release_gate.py's own smoke step, which spawns an isolated
    # HOME/HERMES_HOME). On a dev machine with a real ~/.hermes/config.yaml
    # that names a profile-scoped dsn_env (e.g. HYBRID_AGE_DSN_LINKIBINKI),
    # this test would fail on missing that var instead of testing what it's
    # meant to test: that the entry point resolves to one provider instance.
    # Point HERMES_HOME at an empty directory and supply a non-listening
    # placeholder DSN so registration succeeds without touching any real
    # config or network target.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated_hermes_home"))
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgresql://127.0.0.1:1/hermes_provider_registration_smoke")
    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(repo / name, source / name)
    shutil.copytree(repo / "src", source / "src", ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
    output = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(output), "."],
        cwd=source, check=True, capture_output=True, text=True,
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    unpacked = tmp_path / "unpacked"
    with ZipFile(wheels[0]) as wheel:
        wheel.extractall(unpacked)
    metadata = list(unpacked.glob("*.dist-info"))
    assert len(metadata) == 1
    entry_points = [
        ep for ep in PathDistribution(metadata[0]).entry_points
        if ep.group == "hermes_agent.memory_providers"
    ]
    assert [(ep.name, ep.value) for ep in entry_points] == [("hybrid-age", "hermes_memory:register")]

    class Context:
        def __init__(self):
            self.providers = []

        def register_memory_provider(self, provider):
            self.providers.append(provider)

    context = Context()
    entry_points[0].load()(context)
    assert len(context.providers) == 1
    assert isinstance(context.providers[0], HybridAgeMemoryProvider)
