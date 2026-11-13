"""The built distribution exposes the Hermes provider discovery contract."""

import shutil
import subprocess
import sys
from importlib.metadata import PathDistribution
from pathlib import Path
from zipfile import ZipFile

from hermes_memory.provider import HybridAgeMemoryProvider


def test_built_wheel_entry_point_registers_one_provider(tmp_path: Path) -> None:
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
