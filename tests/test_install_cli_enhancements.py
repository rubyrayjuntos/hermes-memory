"""Tests for hermes-memory-install enhancements (pre-flight checks, model pull, gateway bounce)."""

import io
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

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

    def test_ensure_ollama_model_already_present(self):
        fake_response = MagicMock()
        fake_response.read.return_value = b'{"models": [{"name": "nomic-embed-text:latest"}]}'
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = None

        captured = io.StringIO()
        with patch("urllib.request.urlopen", return_value=fake_response), \
             patch("sys.stdout", captured):
            install_cli.ensure_ollama_model("http://localhost:11434/v1", "nomic-embed-text")

        out = captured.getvalue()
        self.assertIn("Embedding model 'nomic-embed-text' is ready in Ollama.", out)

    def test_ensure_ollama_model_auto_pull(self):
        fake_response = MagicMock()
        fake_response.read.return_value = b'{"models": []}'
        fake_response.__enter__.return_value = fake_response
        fake_response.__exit__.return_value = None

        pulled_commands = []

        def fake_run(cmd, **kwargs):
            pulled_commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="pulled", stderr="")

        captured = io.StringIO()
        with patch("urllib.request.urlopen", return_value=fake_response), \
             patch.object(install_cli.shutil, "which", side_effect=lambda cmd: "/usr/bin/ollama" if cmd == "ollama" else None), \
             patch.object(install_cli.subprocess, "run", side_effect=fake_run), \
             patch("sys.stdout", captured):
            install_cli.ensure_ollama_model("http://localhost:11434/v1", "nomic-embed-text")

        out = captured.getvalue()
        self.assertIn("Auto-pulling from Ollama", out)
        self.assertEqual(pulled_commands, [["/usr/bin/ollama", "pull", "nomic-embed-text"]])
        self.assertIn("Successfully pulled", out)

    def test_ensure_ollama_model_unreachable_is_graceful(self):
        captured = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")), \
             patch("sys.stdout", captured):
            install_cli.ensure_ollama_model("http://localhost:11434/v1", "nomic-embed-text")

        out = captured.getvalue()
        self.assertIn("not reachable", out)

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


if __name__ == "__main__":
    unittest.main()
