"""Tests for hermes-memory-install enhancements (pre-flight checks, embed warm, gateway bounce)."""

import io
import subprocess
import unittest
from unittest.mock import patch

from hermes_memory import install_cli


class TestInstallCliEnhancements(unittest.TestCase):
    def test_check_docker_daemon_success(self):
        with patch.object(install_cli.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(install_cli.subprocess, "run", return_value=subprocess.CompletedProcess(["docker", "info"], 0, stdout="Docker version 27", stderr="")):
            ok, err = install_cli.check_docker_daemon()
            self.assertTrue(ok)
            self.assertEqual(err, "")

    def test_check_docker_daemon_missing_executable(self):
        with patch.object(install_cli.shutil, "which", return_value=None):
            ok, err = install_cli.check_docker_daemon()
            self.assertFalse(ok)
            self.assertIn("not found on PATH", err)

    def test_check_docker_daemon_error(self):
        with patch.object(install_cli.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(install_cli.subprocess, "run", return_value=subprocess.CompletedProcess(["docker", "info"], 1, stdout="", stderr="Cannot connect to docker daemon")):
            ok, err = install_cli.check_docker_daemon()
            self.assertFalse(ok)
            self.assertIn("Cannot connect to docker daemon", err)

    def test_ensure_local_embed_model_warms(self):
        captured = io.StringIO()
        with patch("hermes_memory.embed.warm_local_embedder") as warm, \
             patch("sys.stdout", captured):
            install_cli.ensure_local_embed_model("nomic-embed-text")
        warm.assert_called_once_with("nomic-embed-text")
        self.assertIn("in-process", captured.getvalue())

    def test_ensure_local_embed_model_warns_on_failure(self):
        captured_err = io.StringIO()
        with patch("hermes_memory.embed.warm_local_embedder", side_effect=RuntimeError("no net")), \
             patch("sys.stderr", captured_err), \
             patch("sys.stdout", io.StringIO()):
            install_cli.ensure_local_embed_model("nomic-embed-text")
        self.assertIn("WARNING", captured_err.getvalue())

    def test_restart_hermes_gateway_success(self):
        restarted_cmd = []

        def fake_run(cmd, **kwargs):
            restarted_cmd.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        captured = io.StringIO()
        with patch.object(install_cli.shutil, "which", return_value="/usr/bin/hermes"), \
             patch.object(install_cli.subprocess, "run", side_effect=fake_run), \
             patch("sys.stdout", captured):
            install_cli.restart_hermes_gateway("hermes")

        self.assertEqual(restarted_cmd, [["/usr/bin/hermes", "gateway", "restart"]])
        out = captured.getvalue()
        self.assertIn("Hermes gateway restarted.", out)

    def test_restart_hermes_gateway_missing_binary(self):
        captured = io.StringIO()
        with patch.object(install_cli.shutil, "which", return_value=None), \
             patch("sys.stdout", captured):
            install_cli.restart_hermes_gateway("hermes")
        self.assertEqual(captured.getvalue(), "")

    def test_print_install_summary(self):
        target = install_cli.INSTALLED_TARGET
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            install_cli.print_install_summary(target, "nomic-embed-text")
        out = captured.getvalue()
        self.assertIn("The Hermes Librarian installed successfully!", out)
        self.assertIn("5452/hermes_memory_installed", out)
        self.assertIn("http://127.0.0.1:7890/api/librarian/pane", out)
        self.assertIn("in-process", out)
        self.assertIn("no Ollama required", out)


if __name__ == "__main__":
    unittest.main()
