"""Offline regression tests. Run: python3 -m unittest discover -s tests -v"""

import hashlib
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime
import install


class LauncherTests(unittest.TestCase):
    def test_sdk_arguments_preserved_and_model_effort_respected(self):
        args = ["--output-format", "stream-json", "--input-format", "stream-json",
                "--resume", "session-id", "--model", "gpt-6-astra(max)",
                "--settings", '{"hooks":{}}', "--permission-prompt-tool", "stdio"]
        forwarded, effort = runtime.parse_launch_args(args, "high")
        self.assertEqual(effort, "max")
        self.assertEqual(forwarded[:2], ["--model", "gpt-6-astra(max)"])
        self.assertEqual(forwarded[2:], args[:6] + args[8:])

    def test_all_efforts_and_literal_prompt_separator(self):
        for effort in runtime.EFFORTS:
            args, actual = runtime.parse_launch_args(
                [f"--reasoning={effort}", "-p", "--", "--reasoning", "is prompt text"], "high"
            )
            self.assertEqual(actual, effort)
            self.assertEqual(args, ["--model", f"gpt-6-astra({effort})", "-p", "--", "--reasoning", "is prompt text"])

    def test_ultracode_selection_enables_native_mode_and_proxy_alias(self):
        for selection in (["--reasoning", "ultracode"], ["--model", runtime.ULTRACODE_MODEL],
                          ["--model", runtime.ULTRACODE_MODEL, "--effort", "xhigh"]):
            args, mode = runtime.parse_launch_args([*selection, "--output-format", "stream-json"], "high")
            self.assertEqual(mode, "ultracode")
            self.assertEqual(args, ["--model", runtime.ULTRACODE_MODEL, "--output-format", "stream-json",
                                    "--effort", "ultracode"])
        with self.assertRaises(runtime.SetupError):
            runtime.parse_launch_args(["--reasoning", "ultracode", "--effort", "max"], "high")

    def test_invalid_and_conflicting_options_fail(self):
        for args in (["--reasoning", "ultra"], ["--reasoning"], ["--model", "gpt-5"],
                     ["--model", "gpt-6-astra(none)"],
                     ["--model", "gpt-6-astra(max)", "--reasoning", "low"]):
            with self.subTest(args=args), self.assertRaises(runtime.SetupError):
                runtime.parse_launch_args(args, "high")

    def test_environment_isolated_and_all_helpers_mapped(self):
        source = {"ANTHROPIC_API_KEY": "old", "CLAUDE_CODE_OAUTH_TOKEN": "old",
                  "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: old",
                  "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
                  "CLAUDE_CODE_EFFORT_LEVEL": "low", "PATH": "/bin", "NO_PROXY": "example.test"}
        env = runtime.claude_env({"port": 8317, "api_key": "local-test-key", "config_dir": "/tmp/test config"}, "max", source)
        self.assertEqual(source["ANTHROPIC_API_KEY"], "old")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8317")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1050000")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gpt-6-astra(max)")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-6-astra(max)")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/test config/claude")
        self.assertEqual(env["NO_PROXY"], "example.test,127.0.0.1,localhost")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-codex-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "space ' quote $dollar"
        self.base.mkdir()
        self.settings = {"config_dir": str(self.base / "config"), "data_dir": str(self.base / "data"),
                         "state_dir": str(self.base / "state"), "bin_dir": str(self.base / "bin"),
                         "port": 8317, "api_key": "test-local-key", "reasoning": "high"}

    def fake_cli(self, name, body):
        target = self.base / name
        target.write_text(f"#!{sys.executable}\n" + body)
        target.chmod(0o755)
        return target

    def test_paseo_merge_preserves_config_and_is_idempotent(self):
        config_file = self.base / "paseo.json"
        original = {"version": 1, "daemon": {"port": 6768}, "agents": {
            "providers": {"claude": {"enabled": True}, "other": {"extends": "codex", "label": "Other"}}
        }}
        runtime.write_json(config_file, original)
        install.merge_paseo(config_file, self.settings, {})
        actual = runtime.read_json(config_file)
        self.assertEqual(actual["daemon"], original["daemon"])
        self.assertEqual(actual["agents"]["providers"]["claude"], {"enabled": True})
        provider = actual["agents"]["providers"]["claude-codex"]
        self.assertEqual(provider["extends"], "claude")
        self.assertEqual(provider["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1050000")
        self.assertEqual(provider["env"]["CLAUDE_CODEX_PASEO_USAGE"], "1")
        self.assertEqual(len(provider["models"]), 6)
        xhigh = next(model for model in provider["models"] if model["id"] == "gpt-6-astra(xhigh)")
        self.assertEqual([option["id"] for option in xhigh["thinkingOptions"]], ["default", "ultracode"])
        self.assertTrue(xhigh["thinkingOptions"][0]["isDefault"])
        self.assertEqual(provider["models"][-1]["id"], runtime.ULTRACODE_MODEL)
        self.assertEqual(sum(m["isDefault"] for m in provider["models"]), 1)
        self.assertEqual(provider["models"][4]["id"], "gpt-6-astra(max)")
        self.assertNotIn("test-local-key", config_file.read_text())
        first = config_file.read_bytes()
        install.merge_paseo(config_file, self.settings, {"paseo_config": str(config_file)})
        self.assertEqual(config_file.read_bytes(), first)
        self.assertEqual(len(list(self.base.glob("paseo.json.claude-codex-backup-*"))), 1)

    def test_invalid_paseo_config_not_overwritten(self):
        for content in ('{"agents":[]}', '{"agents":{"providers":[]}}', '{bad', '[]'):
            path = self.base / "invalid.json"
            path.write_text(content)
            with self.assertRaises(runtime.SetupError):
                install.merge_paseo(path, self.settings, {})
            self.assertEqual(path.read_text(), content)

    def test_download_checksum_failure_never_installs(self):
        def fake_download(url, path):
            Path(path).write_bytes(b"tampered artifact")
        with patch.object(install, "download", fake_download), \
             patch.object(install.platform, "system", return_value="Linux"), \
             patch.object(install.platform, "machine", return_value="x86_64"), \
             self.assertRaisesRegex(runtime.SetupError, "SHA-256"):
            install.install_proxy(self.base, install.PROXY_VERSION)
        self.assertFalse((self.base / "releases" / install.PROXY_VERSION / "cli-proxy-api").exists())

    def test_safe_archive_extraction(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for name, content in (("cli-proxy-api", b"test binary"), ("../../escape", b"bad")):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        data = archive.getvalue()
        with patch.object(install, "download", lambda url, path: Path(path).write_bytes(data)), \
             patch.object(install.platform, "system", return_value="Linux"), \
             patch.object(install.platform, "machine", return_value="x86_64"), \
             patch.dict(install.CHECKSUMS, {"linux_amd64_no-plugin": hashlib.sha256(data).hexdigest()}):
            binary = install.install_proxy(self.base, install.PROXY_VERSION)
        self.assertEqual(binary.read_bytes(), b"test binary")
        self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.base.parent / "escape").exists())

    def test_full_staged_install_and_launch_with_sdk_arguments(self):
        claude = self.fake_cli("claude-original", "import json,os,sys\nprint(json.dumps({'args':sys.argv[1:],'model':os.environ.get('ANTHROPIC_MODEL'),'base':os.environ.get('ANTHROPIC_BASE_URL')}))\n")
        paseo = self.fake_cli("paseo-original", "print('0.8.0-beta.1')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        claude_before = claude.read_bytes()
        args = ["bash", str(ROOT / "install.sh"), "--config-dir", self.settings["config_dir"],
                "--data-dir", self.settings["data_dir"], "--state-dir", self.settings["state_dir"],
                "--bin-dir", self.settings["bin_dir"], "--paseo-home", str(self.base / "paseo"),
                "--claude-bin", str(claude), "--paseo-bin", str(paseo), "--proxy-binary", str(proxy),
                "--skip-login", "--skip-paseo-start", "--no-path"]
        subprocess.run(args, check=True, capture_output=True, text=True)
        config_file = Path(self.settings["config_dir"]) / "settings.json"
        first = runtime.read_json(config_file)
        subprocess.run(args, check=True, capture_output=True, text=True)
        self.assertEqual(first["api_key"], runtime.read_json(config_file)["api_key"])
        self.assertEqual(claude.read_bytes(), claude_before)
        self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.settings["config_dir"]).stat().st_mode & 0o777, 0o700)
        launcher = Path(self.settings["bin_dir"]) / "claude-codex"
        result = subprocess.run([str(launcher), "--version"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["args"], ["--version"])
        self.assertEqual(result.stderr, "")
        with patch.object(runtime.Runtime, "has_login", return_value=True), \
             patch.object(runtime.Runtime, "start"), patch.object(runtime.os, "execve") as execute:
            runtime.launch(first, ["--model", "gpt-6-astra(xhigh)", "--input-format", "stream-json", "-p"])
        binary, forwarded, env = execute.call_args.args
        self.assertEqual(binary, str(claude))
        self.assertEqual(forwarded, [str(claude), "--model", "gpt-6-astra(xhigh)", "--input-format", "stream-json", "-p"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-6-astra(xhigh)")

    def test_absent_paseo_is_skipped_without_npm_or_config_changes(self):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        args = ["--config-dir", self.settings["config_dir"], "--data-dir", self.settings["data_dir"],
                "--state-dir", self.settings["state_dir"], "--bin-dir", self.settings["bin_dir"],
                "--paseo-home", str(self.base / "absent-paseo"), "--claude-bin", str(claude),
                "--proxy-binary", str(proxy), "--skip-login", "--no-path"]
        with patch.dict(os.environ, {"PATH": ""}), patch.object(install.subprocess, "run") as run:
            install.install(install.parser().parse_args(args))
        run.assert_not_called()
        self.assertFalse((self.base / "absent-paseo").exists())
        self.assertFalse((Path(self.settings["bin_dir"]) / "paseo-codex").exists())
        self.assertFalse((Path(self.settings["data_dir"]) / "npm" / "paseo").exists())

    def test_installer_reuses_system_paseo_over_saved_private_beta(self):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        system_paseo = self.fake_cli("paseo", "print('0.7.2')\n")
        private_paseo = self.fake_cli("paseo-private", "print('0.8.0-beta.1')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        original = system_paseo.read_bytes()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        args = ["--config-dir", self.settings["config_dir"], "--data-dir", self.settings["data_dir"],
                "--state-dir", self.settings["state_dir"], "--bin-dir", self.settings["bin_dir"],
                "--paseo-home", str(self.base / "paseo-home"), "--port", str(port),
                "--claude-bin", str(claude), "--proxy-binary", str(proxy), "--skip-login", "--no-path"]
        install.install(install.parser().parse_args(args + ["--paseo-bin", str(private_paseo), "--skip-paseo-start"]))
        test_path = str(self.base) + os.pathsep + os.environ.get("PATH", "")
        with patch.dict(os.environ, {"PATH": test_path}), patch.object(install.subprocess, "run") as run:
            install.install(install.parser().parse_args(args))
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [[str(system_paseo), "daemon", "restart"], [str(system_paseo), "reload"]])
        saved = runtime.read_json(Path(self.settings["config_dir"]) / "settings.json")
        self.assertEqual(saved["paseo_bin"], str(system_paseo))
        self.assertEqual(system_paseo.read_bytes(), original)
        self.assertFalse((Path(self.settings["data_dir"]) / "npm" / "paseo").exists())
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        result = subprocess.run([str(wrapper), "--version"], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "0.7.2")

    def test_explicit_paseo_is_used_without_version_checks(self):
        custom = self.fake_cli("paseo-custom", "print('0.9.0')\n")
        with patch.object(install.subprocess, "check_output") as version_probe:
            self.assertEqual(install.detect_paseo(str(custom)), str(custom))
        version_probe.assert_not_called()

    def test_missing_system_paseo_does_not_select_legacy_private_copy(self):
        private = self.base / "npm" / "paseo" / "node_modules" / ".bin" / "paseo"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\necho 0.8.0-beta.1\n")
        private.chmod(0o755)
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertIsNone(install.detect_paseo(None))

    def test_launcher_refuses_to_replace_unrelated_program(self):
        target = self.base / "claude-codex"
        target.write_text("existing program")
        with self.assertRaises(runtime.SetupError):
            install.write_launcher(target, ["false"])
        self.assertEqual(target.read_text(), "existing program")


if __name__ == "__main__":
    unittest.main()
