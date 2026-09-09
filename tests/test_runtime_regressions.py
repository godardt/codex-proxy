"""Offline regressions for argument boundaries, login verification, and locks."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_codex as runtime


class ArgumentBoundaryTests(unittest.TestCase):
    def test_required_operands_are_not_wrapper_options(self):
        for option in ("--append-system-prompt", "--system-prompt", "--settings",
                       "--system-prompt-file", "--append-system-prompt-file",
                       "--permission-prompt-tool", "--json-schema", "--name", "-n"):
            for value in ("--model=gpt-6-astra(max)", "--reasoning=low", "--effort=max", ""):
                with self.subTest(option=option, value=value):
                    original = [option, value, "-p", "Hi", "--reasoning", "xhigh"]
                    args, effort = runtime.parse_launch_args(original, "high")
                    self.assertEqual(effort, "xhigh")
                    self.assertEqual(args, ["--model", "gpt-6-astra(xhigh)", *original[:-2]])

    def test_required_operand_can_be_a_literal_separator(self):
        original = ["--append-system-prompt", "--", "--reasoning=low", "-p", "Hi"]
        args, effort = runtime.parse_launch_args(original, "high")
        self.assertEqual(effort, "low")
        self.assertEqual(args, ["--model", "gpt-6-astra(low)",
                                "--append-system-prompt", "--", "-p", "Hi"])

    def test_inline_operands_and_unknown_flags_are_forwarded(self):
        original = ["--append-system-prompt=--reasoning=low", "--settings={}",
                    "--future-sdk-flag", "value", "--future-sdk-flag=--model=literal",
                    "--model", "gpt-6-astra(max)", "-p", "Hi"]
        args, effort = runtime.parse_launch_args(original, "high")
        self.assertEqual(effort, "max")
        self.assertEqual(args, ["--model", "gpt-6-astra(max)", *original[:5], *original[7:]])

    def test_optional_operands_and_boolean_flags_do_not_hide_selectors(self):
        for option in ("--resume", "-r", "--debug", "-d", "--worktree", "-w"):
            for operand in ([], ["session-or-filter"], ["-"], [""]):
                with self.subTest(option=option, operand=operand):
                    original = [option, *operand, "--model=gpt-6-astra(max)", "-p", "Hi"]
                    args, effort = runtime.parse_launch_args(original, "high")
                    self.assertEqual(effort, "max")
                    self.assertEqual(args, ["--model", "gpt-6-astra(max)",
                                            option, *operand, "-p", "Hi"])
        args, effort = runtime.parse_launch_args(["-p", "--reasoning=low", "Hi"], "high")
        self.assertEqual((args, effort), (["--model", "gpt-6-astra(low)", "-p", "Hi"], "low"))

    def test_variadic_options_preserve_first_required_operand(self):
        for option in ("--mcp-config", "--tools", "--allowedTools", "--allowed-tools",
                       "--disallowedTools", "--disallowed-tools", "--add-dir", "--betas", "--file"):
            with self.subTest(option=option):
                original = [option, "--model=gpt-6-astra(max)", "another-value", "-",
                            "--reasoning=low", "-p", "Hi"]
                args, effort = runtime.parse_launch_args(original, "high")
                self.assertEqual(effort, "low")
                self.assertEqual(args, ["--model", "gpt-6-astra(low)", *original[:4], *original[5:]])
        args, effort = runtime.parse_launch_args(
            ["--tools=Read", "Edit", "--reasoning=low", "-p", "Hi"], "high")
        self.assertEqual((args, effort),
                         (["--model", "gpt-6-astra(low)", "--tools=Read", "Edit", "-p", "Hi"], "low"))

    def test_separator_preserves_literal_tail_after_forwarded_values(self):
        original = ["--append-system-prompt", "--model=literal", "--resume", "-p",
                    "--", "--reasoning=low", "--model=literal"]
        args, effort = runtime.parse_launch_args(original, "high")
        self.assertEqual(effort, "high")
        self.assertEqual(args, ["--model", "gpt-6-astra(high)", *original])

    def test_native_missing_operand_is_not_filled_by_normalized_effort(self):
        with self.assertRaisesRegex(runtime.SetupError, "--system-prompt needs a value"):
            runtime.parse_launch_args(["--reasoning=ultracode", "--system-prompt"], "high")


class LoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claude-codex-login-test-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.auth_dir = self.base / "config" / "auth"
        self.auth_dir.mkdir(parents=True)
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()
        self.runtime = runtime.Runtime({"config_dir": str(self.auth_dir.parent),
                                        "state_dir": str(self.state_dir), "proxy_bin": "/unused/proxy"})
        self.old_file = self.auth_dir / "old-account.json"
        self.old_auth = {"type": "codex", "refresh_token": "old-test-token"}
        runtime.write_json(self.old_file, self.old_auth)
        stop = patch.object(self.runtime, "stop")
        self.stop = stop.start()
        self.addCleanup(stop.stop)

    def test_zero_exit_without_saved_credentials_rejects_old_login(self):
        before = self.old_file.read_bytes()
        self.assertTrue(self.runtime.has_login())
        with patch.object(runtime.subprocess, "run") as run, \
             self.assertRaisesRegex(runtime.SetupError, "No new or updated Codex OAuth credentials"):
            self.runtime.login()
        self.stop.assert_called_once_with()
        run.assert_called_once_with(["/unused/proxy", "-config", str(self.auth_dir.parent / "proxy.yaml"),
                                     "-codex-login"], cwd=self.state_dir, check=True)
        self.assertEqual(self.old_file.read_bytes(), before)
        self.assertTrue(self.runtime.has_login())

    def test_zero_exit_without_any_credentials_fails(self):
        self.old_file.unlink()
        self.assertFalse(self.runtime.has_login())
        with patch.object(runtime.subprocess, "run"), self.assertRaises(runtime.SetupError):
            self.runtime.login()
        self.assertEqual(list(self.auth_dir.iterdir()), [])

    def test_new_credentials_and_device_flags_are_accepted(self):
        saved = self.auth_dir / "new-account.json"
        def save(*args, **kwargs):
            runtime.write_json(saved, {"type": "codex", "refresh_token": "new-test-token"})
            saved.chmod(0o644)
        with patch.object(runtime.subprocess, "run", side_effect=save) as run:
            self.runtime.login(device=True, no_browser=True)
        self.assertEqual(run.call_args.args[0][-2:], ["-codex-device-login", "-no-browser"])
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(runtime.read_json(self.old_file), self.old_auth)

    def test_updated_credentials_are_accepted_even_if_mtime_is_unchanged(self):
        original_stat = self.old_file.stat()
        def save(*args, **kwargs):
            self.old_file.write_text('{"type":"codex","refresh_token":"new-test-token"}')
            os.utime(self.old_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with patch.object(runtime.subprocess, "run", side_effect=save):
            self.runtime.login()
        self.assertEqual(runtime.read_json(self.old_file)["refresh_token"], "new-test-token")

    def test_same_content_rewrite_is_accepted(self):
        before = self.old_file.read_bytes()
        original_stat = self.old_file.stat()
        def save(*args, **kwargs):
            self.old_file.write_bytes(before)
            os.utime(self.old_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))
        with patch.object(runtime.subprocess, "run", side_effect=save):
            self.runtime.login()
        self.assertEqual(self.old_file.read_bytes(), before)

    def test_same_content_atomic_replacement_is_accepted(self):
        original_stat = self.old_file.stat()
        def save(*args, **kwargs):
            runtime.write_json(self.old_file, self.old_auth)
            os.utime(self.old_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with patch.object(runtime.subprocess, "run", side_effect=save):
            self.runtime.login()
        self.assertEqual(runtime.read_json(self.old_file), self.old_auth)

    def test_invalid_new_credentials_cannot_reuse_old_login(self):
        saved = self.auth_dir / "invalid.json"
        for content in ('not json', '[]', '{}',
                        '{"type":"other","refresh_token":"test"}',
                        '{"type":"codex","refresh_token":"test","disabled":true}',
                        '{"type":"codex","refresh_token":""}'):
            with self.subTest(content=content):
                def save(*args, **kwargs):
                    saved.write_text(content)
                with patch.object(runtime.subprocess, "run", side_effect=save), \
                     self.assertRaises(runtime.SetupError):
                    self.runtime.login()
                self.assertEqual(runtime.read_json(self.old_file), self.old_auth)

    def test_nonzero_exit_does_not_accept_credentials(self):
        before = self.old_file.read_bytes()
        with patch.object(runtime.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "login")), \
             self.assertRaises(subprocess.CalledProcessError):
            self.runtime.login()
        self.assertEqual(self.old_file.read_bytes(), before)


class FileLockTests(unittest.TestCase):
    def test_timeout_release_and_private_stable_lock_file(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-lock-test-") as temp:
            path = Path(temp) / "state" / "operation.lock"
            with self.assertRaisesRegex(ValueError, "fixture failure"):
                with runtime.file_lock(path):
                    inode = path.stat().st_ino
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    with self.assertRaisesRegex(runtime.SetupError, "fixture busy"):
                        with runtime.file_lock(path, timeout=0, busy_message="fixture busy"):
                            self.fail("A second lock was acquired")
                    raise ValueError("fixture failure")
            with runtime.file_lock(path, timeout=0):
                self.assertEqual(path.stat().st_ino, inode)


if __name__ == "__main__":
    unittest.main()
