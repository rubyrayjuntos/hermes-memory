"""Release-gate installs may target only a disposable, derived Compose database."""

from pathlib import Path

import pytest

from hermes_memory import install_cli

GOOD = "postgres://hermes:pw-not-printed@127.0.0.1:5461/hermes_release_gate_ab12"


def test_release_gate_target_derives_unique_compose_identity():
    target = install_cli.release_gate_target(GOOD)
    assert target == install_cli.DbTarget(
        "hermes-release-gate-ab12", "hermes-release-gate-ab12-postgres", "5461", "hermes_release_gate_ab12"
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://hermes:pw@127.0.0.1:5450/hermes_release_gate_ab12",  # dev stack
        "postgres://hermes:pw@127.0.0.1:5452/hermes_release_gate_ab12",  # installed stack
        "postgres://hermes:pw@127.0.0.1:5461/hermes_memory",  # not a release-gate database
        "postgres://hermes:pw@127.0.0.1:5461/hermes_memory_installed",
        "postgres://hermes:pw@10.0.0.5:5461/hermes_release_gate_ab12",  # not loopback
        "postgres://hermes:pw@127.0.0.1/hermes_release_gate_ab12",  # no explicit port
        "postgres://hermes:pw@127.0.0.1:5461/hermes_release_gate_ab12?host=/var/run",  # libpq override
        "mysql://hermes:pw@127.0.0.1:5461/hermes_release_gate_ab12",
    ],
)
def test_release_gate_target_refuses_unsafe_dsn_without_echo(dsn):
    with pytest.raises(ValueError) as excinfo:
        install_cli.release_gate_target(dsn)
    assert "pw" not in str(excinfo.value)
    assert "5461" not in str(excinfo.value)


def test_compose_for_release_gate_target_uses_derived_names_only(tmp_path):
    target = install_cli.release_gate_target(GOOD)
    path = tmp_path / "compose.yml"
    install_cli.write_installed_compose(path, target)
    text = path.read_text()
    assert "container_name: hermes-release-gate-ab12-postgres" in text
    assert '"127.0.0.1:5461:5432"' in text
    assert "name: hermes-release-gate-ab12_pgdata" in text
    assert install_cli.INSTALLED_PORT not in text
    assert install_cli.INSTALLED_CONTAINER not in text


def test_default_compose_still_names_installed_stack(tmp_path):
    path = tmp_path / "compose.yml"
    install_cli.write_installed_compose(path)
    text = path.read_text()
    assert f'"127.0.0.1:{install_cli.INSTALLED_PORT}:5432"' in text
    assert f"container_name: {install_cli.INSTALLED_CONTAINER}" in text


def test_release_gate_flag_refuses_home_outside_temp_before_any_mutation(monkeypatch, capsys):
    monkeypatch.setattr(install_cli, "HERMES_HOME", Path("/home/operator/.hermes"))
    rc = install_cli.main(["--release-gate-target", "--dsn", GOOD, "--yes"])
    assert rc == 2
    assert "outside the system temp directory" in capsys.readouterr().err


def test_release_gate_flag_requires_explicit_dsn(monkeypatch, capsys):
    monkeypatch.setattr(install_cli, "HERMES_HOME", Path(install_cli.tempfile.gettempdir()) / "x" / ".hermes")
    assert install_cli.main(["--release-gate-target", "--yes"]) == 2
    assert "requires --dsn" in capsys.readouterr().err
