"""Offline regression tests. Run: python3 -m unittest discover -s tests -v"""

import hashlib
import io
import json
import os
import shutil
import socket
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime
import install
import paseo_compat


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

    def test_paseo_sessions_use_the_daemon_profile(self):
        # Paseo reloads transcripts from the daemon's CLAUDE_CONFIG_DIR or ~/.claude,
        # not from the provider environment, so Paseo launches keep that profile.
        settings = {"port": 8317, "api_key": "local-test-key", "config_dir": "/tmp/test config"}
        paseo = {"CLAUDE_CODEX_PASEO_USAGE": "1", "PATH": "/bin"}
        self.assertNotIn("CLAUDE_CONFIG_DIR", runtime.claude_env(settings, "high", paseo))
        daemon = runtime.claude_env(settings, "high", {**paseo, "CLAUDE_CONFIG_DIR": "/daemon/claude"})
        self.assertEqual(daemon["CLAUDE_CONFIG_DIR"], "/daemon/claude")
        # Provider entries written by older installers pinned the isolated profile.
        stale = runtime.claude_env(settings, "high", {**paseo, "CLAUDE_CONFIG_DIR": "/tmp/test config/claude"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", stale)
        self.assertEqual(stale["ANTHROPIC_MODEL"], "gpt-6-astra(high)")
        terminal = runtime.claude_env(settings, "high", {"CLAUDE_CONFIG_DIR": "/shell/claude", "PATH": "/bin"})
        self.assertEqual(terminal["CLAUDE_CONFIG_DIR"], "/tmp/test config/claude")


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

    def fake_paseo(self, name, body):
        if not shutil.which("node"):
            self.skipTest("Node.js is required to validate the installed usage adapter")
        package = self.base / (name + "-package")
        entry = package / "bin" / "paseo"
        entry.parent.mkdir(parents=True)
        entry.write_text(f"#!{sys.executable}\n" + body)
        entry.chmod(0o755)
        runtime.write_json(package / "package.json", {"name": "@getpaseo/cli", "type": "module"})
        server = package / "node_modules" / "@getpaseo" / "server"
        runtime.write_json(server / "package.json", {"name": "@getpaseo/server", "type": "module"})
        reader = server / paseo_compat.AGENT_PATH
        reader.parent.mkdir(parents=True)
        reader.write_text((ROOT / "tests" / "fixtures" / "paseo_claude_usage.js").read_text())
        target = self.base / name
        target.symlink_to(entry)
        return target

    def staged_args(self, *extra):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        return ["--config-dir", self.settings["config_dir"], "--data-dir", self.settings["data_dir"],
                "--state-dir", self.settings["state_dir"], "--bin-dir", self.settings["bin_dir"],
                "--paseo-home", str(self.base / "paseo-home"), "--claude-bin", str(claude),
                "--proxy-binary", str(proxy), "--skip-login", "--skip-paseo-start", "--no-path", *extra]

    def test_paseo_wrapper_uses_runtime_path_and_quoted_node_prefix(self):
        node = self.fake_cli("node", "print('v22.0.0')\n")
        paseo = self.fake_paseo("paseo", "import json,os,sys\nprint(json.dumps({'path':os.environ['PATH'], 'home':os.environ['PASEO_HOME'], 'args':sys.argv[1:]}))\n")
        args = self.staged_args("--paseo-bin", str(paseo))
        installation_path = str(node.parent) + os.pathsep + "/installation-only"
        with patch.dict(os.environ, {"PATH": installation_path}):
            install.install(install.parser().parse_args(args))
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        self.assertNotIn("/installation-only", wrapper.read_text())
        forwarded = ["--version", "space ' quote $literal", ""]
        for new_path in ("/new node ' $prefix/bin:/usr/bin", ""):
            with self.subTest(path=new_path):
                result = subprocess.run([str(wrapper), *forwarded], check=True, capture_output=True,
                                        text=True, env={**os.environ, "PATH": new_path})
                self.assertEqual(json.loads(result.stdout), {
                    "path": str(node.parent) + os.pathsep + new_path,
                    "home": str(self.base / "paseo-home"), "args": forwarded,
                })

    def test_launcher_without_prefix_preserves_empty_runtime_path(self):
        cli = self.fake_cli("print-path", "import os\nprint(repr(os.environ['PATH']))\n")
        wrapper = self.base / "wrapper"
        install.write_launcher(wrapper, [cli])
        result = subprocess.run([str(wrapper)], check=True, capture_output=True, text=True,
                                env={**os.environ, "PATH": ""})
        self.assertEqual(result.stdout.strip(), "''")

    def test_bash_login_precedence_preserves_profiles_and_is_idempotent(self):
        for names, chosen in (((".bash_profile", ".bash_login", ".profile"), ".bash_profile"),
                              ((".bash_login", ".profile"), ".bash_login"),
                              ((".profile",), ".profile"), ((), ".profile")):
            with self.subTest(profiles=names):
                home = self.base / (chosen + str(len(names)))
                home.mkdir()
                originals = {name: f"# existing {name}\n" for name in (".bashrc", *names)}
                for name, content in originals.items():
                    (home / name).write_text(content)
                with patch.dict(os.environ, {"HOME": str(home), "SHELL": "/bin/bash"}):
                    install.add_path(Path(self.settings["bin_dir"]))
                    before = {path.name: path.read_bytes() for path in home.iterdir()}
                    install.add_path(Path(self.settings["bin_dir"]))
                self.assertEqual({path.name: path.read_bytes() for path in home.iterdir()}, before)
                for name in (".bashrc", ".bash_profile", ".bash_login", ".profile"):
                    path = home / name
                    if name in (".bashrc", chosen):
                        self.assertTrue(path.read_text().startswith(originals.get(name, "")))
                        self.assertEqual(path.read_text().count(runtime.MARKER), 1)
                    elif name in names:
                        self.assertEqual(path.read_text(), originals[name])
                    else:
                        self.assertFalse(path.exists())

    def test_bash_path_update_preserves_dotfile_symlinks_and_modes(self):
        home = self.base / "home"
        home.mkdir()
        targets = []
        for name in (".bashrc", ".bash_login"):
            target = self.base / (name + "-target")
            target.write_text(f"# original {name}\n")
            target.chmod(0o640)
            (home / name).symlink_to(target)
            targets.append(target)
        profile = home / ".profile"
        profile.write_text("# lower priority\n")
        with patch.dict(os.environ, {"HOME": str(home), "SHELL": "/bin/bash"}):
            install.add_path(Path(self.settings["bin_dir"]))
            before = {path: path.read_bytes() for path in targets}
            backups = list(home.glob("*.claude-codex-backup-*"))
            install.add_path(Path(self.settings["bin_dir"]))
        self.assertEqual(profile.read_text(), "# lower priority\n")
        self.assertEqual(list(home.glob("*.claude-codex-backup-*")), backups)
        for name, target in zip((".bashrc", ".bash_login"), targets):
            self.assertTrue((home / name).is_symlink())
            self.assertEqual((home / name).resolve(), target)
            self.assertEqual(target.read_bytes(), before[target])
            self.assertTrue(target.read_text().startswith(f"# original {name}\n"))
            self.assertEqual(target.read_text().count(runtime.MARKER), 1)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_recursive_paseo_selections_preserve_installed_wrapper(self):
        paseo = self.fake_paseo("paseo-original", "print('original Paseo')\n")
        args = self.staged_args()
        install.install(install.parser().parse_args([*args, "--paseo-bin", str(paseo)]))
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        alias = self.base / "paseo-alias"
        alias.symlink_to(wrapper)
        discovered = self.base / "paseo"
        discovered.symlink_to(wrapper)
        named_wrapper = self.fake_cli("paseo-codex", "print('another wrapper')\n")
        protected = [wrapper, Path(self.settings["config_dir"]) / "settings.json",
                     Path(self.settings["config_dir"]) / "proxy.yaml",
                     self.base / "paseo-home" / "config.json"]
        before = {path: path.read_bytes() for path in protected}
        for candidate in (wrapper, alias, named_wrapper, None):
            with self.subTest(candidate=candidate), \
                 patch.dict(os.environ, {"PATH": str(self.base)}), \
                 patch.object(install.Runtime, "stop") as stop, \
                 patch.object(install, "atomic_write") as write, \
                 patch.object(install, "write_json") as write_json:
                extra = ["--paseo-bin", str(candidate)] if candidate else []
                with self.assertRaisesRegex(runtime.SetupError, "original Paseo executable"):
                    install.install(install.parser().parse_args([*args, *extra]))
                stop.assert_not_called()
                write.assert_not_called()
                write_json.assert_not_called()
                self.assertEqual({path: path.read_bytes() for path in protected}, before)
        result = subprocess.run([str(wrapper)], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "original Paseo")

    def concurrent_installers(self, fail_first=False):
        # Each child instruments the real installer/lock and reports over a socket.
        # Gates pause the first before settings are written and at transaction end;
        # the second reports an actual failed nonblocking flock before it waits.
        script = textwrap.dedent('''
            import json, os, socket, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import install
            import claude_codex as runtime
            channel = socket.socket(fileno=int(sys.argv[2]))
            role, fail_first = sys.argv[3], sys.argv[4] == "True"
            opts = install.parser().parse_args(json.loads(sys.argv[5]))
            settings_path = Path(opts.config_dir) / "settings.json"
            lock_path = Path(opts.config_dir) / "install.lock"
            def emit(*event):
                channel.sendall((json.dumps(event) + "\\n").encode())
            def gate():
                if channel.recv(1) != b"x":
                    raise RuntimeError("parent closed coordination channel")
            original_flock = runtime.fcntl.flock
            def flock(handle, operation):
                if Path(handle.name) == lock_path and operation == runtime.fcntl.LOCK_EX:
                    try:
                        original_flock(handle, operation | runtime.fcntl.LOCK_NB)
                    except BlockingIOError:
                        emit("contended")
                        original_flock(handle, operation)
                    emit("acquired")
                else:
                    original_flock(handle, operation)
            runtime.fcntl.flock = flock
            original_exists = Path.exists
            def exists(path):
                value = original_exists(path)
                if path == settings_path:
                    emit("previous-check", value)
                return value
            Path.exists = exists
            original_read = install.read_json
            def read_json(path):
                value = original_read(path)
                if path == settings_path:
                    emit("previous-read", value)
                return value
            install.read_json = read_json
            original_write = install.write_json
            def write_json(path, value):
                if path == settings_path:
                    emit("settings", value)
                    if role == "first":
                        gate()
                original_write(path, value)
            install.write_json = write_json
            original_say = install.say
            def say(message):
                original_say(message)
                if role == "first" and message.startswith("Installation staged;"):
                    emit("transaction-end")
                    gate()
                    if fail_first:
                        raise RuntimeError("injected installer failure")
            install.say = say
            try:
                install.install(opts)
            except RuntimeError as exc:
                emit("failed", str(exc))
                sys.exit(19)
            emit("done")
        ''')
        args = self.staged_args("--skip-paseo")

        def start(role, extra):
            parent, child = socket.socketpair()
            parent.settimeout(10)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(ROOT / "scripts"), str(child.fileno()),
                     role, str(fail_first), json.dumps([*args, *extra])],
                    pass_fds=(child.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env={**os.environ, "HOME": str(self.base)},
                )
            finally:
                child.close()
            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
                parent.close()
            self.addCleanup(cleanup)
            return process, parent

        def receive(channel):
            line = bytearray()
            while not line.endswith(b"\n"):
                piece = channel.recv(1)
                self.assertTrue(piece, "Installer exited before its expected coordination event")
                line.extend(piece)
            return json.loads(line)

        first, first_channel = start("first", ["--port", "18431", "--reasoning", "low"])
        self.assertEqual(receive(first_channel), ["acquired"])
        self.assertEqual(receive(first_channel), ["previous-check", False])
        event, first_settings = receive(first_channel)
        self.assertEqual(event, "settings")
        self.assertFalse((Path(self.settings["config_dir"]) / "settings.json").exists())
        lock_path = Path(self.settings["config_dir"]) / "install.lock"
        lock_inode = lock_path.stat().st_ino
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lock_path.parent.stat().st_mode & 0o777, 0o700)
        second, second_channel = start("second", ["--reasoning", "max"])
        # This event proves actual kernel lock contention, not just scheduling:
        # no previous-settings existence check or read is allowed before it.
        self.assertEqual(receive(second_channel), ["contended"])
        first_channel.sendall(b"x")
        self.assertEqual(receive(first_channel), ["transaction-end"])
        self.assertEqual(runtime.read_json(lock_path.parent / "settings.json"), first_settings)
        self.assertEqual(runtime.read_json(lock_path.parent / "proxy.yaml"), runtime.proxy_config(first_settings))
        with self.assertRaises(runtime.SetupError):
            with runtime.file_lock(lock_path, timeout=0):
                self.fail("Install lock was released before the transaction ended")
        first_channel.sendall(b"x")
        expected = ["failed", "injected installer failure"] if fail_first else ["done"]
        self.assertEqual(receive(first_channel), expected)
        self.assertEqual(receive(second_channel), ["acquired"])
        self.assertEqual(receive(second_channel), ["previous-check", True])
        self.assertEqual(receive(second_channel), ["previous-read", first_settings])
        event, second_settings = receive(second_channel)
        self.assertEqual(event, "settings")
        self.assertEqual(receive(second_channel), ["done"])
        for process, expected_code in ((first, 19 if fail_first else 0), (second, 0)):
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, expected_code, stdout + stderr)
        self.assertEqual(second_settings["api_key"], first_settings["api_key"])
        self.assertEqual(second_settings["port"], 18431)
        self.assertEqual(second_settings["reasoning"], "max")
        self.assertEqual(runtime.read_json(lock_path.parent / "settings.json"), second_settings)
        self.assertEqual(runtime.read_json(lock_path.parent / "proxy.yaml"), runtime.proxy_config(second_settings))
        self.assertEqual(lock_path.stat().st_ino, lock_inode)
        with runtime.file_lock(lock_path, timeout=0):
            pass

    def test_concurrent_installers_wait_before_reading_previous_settings(self):
        self.concurrent_installers()

    def test_installer_failure_releases_lock_for_waiting_installer(self):
        self.concurrent_installers(fail_first=True)

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
        self.assertNotIn("CLAUDE_CONFIG_DIR", provider["env"])
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
        paseo = self.fake_paseo("paseo-original", "print('paseo original')\n")
        proxy = self.fake_cli("proxy-unused", "raise SystemExit(1)\n")
        claude_before = claude.read_bytes()
        args = ["bash", str(ROOT / "install.sh"), "--config-dir", self.settings["config_dir"],
                "--data-dir", self.settings["data_dir"], "--state-dir", self.settings["state_dir"],
                "--bin-dir", self.settings["bin_dir"], "--paseo-home", str(self.base / "paseo"),
                "--claude-bin", str(claude), "--paseo-bin", str(paseo), "--proxy-binary", str(proxy),
                "--skip-login", "--skip-paseo-start", "--no-path"]
        reader = paseo_compat.find_usage_reader(paseo)
        original_reader = reader.read_text()
        subprocess.run(args, check=True, capture_output=True, text=True)
        config_file = Path(self.settings["config_dir"]) / "settings.json"
        first = runtime.read_json(config_file)
        patched_reader = reader.read_text()
        self.assertEqual(patched_reader, paseo_compat.patch_source(original_reader))
        backups = list(reader.parent.glob("agent.js.claude-codex-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original_reader)
        installed_runtime = Path(self.settings["data_dir"]) / "claude_codex.py"
        installed_runtime.write_text("# outdated installed launcher\n")
        auth = Path(self.settings["config_dir"]) / "auth" / "preserved.json"
        runtime.write_json(auth, {"type": "codex", "refresh_token": "dummy-preserve-only"})
        paseo_config = self.base / "paseo" / "config.json"
        providers = runtime.read_json(paseo_config)
        providers["agents"]["providers"]["unrelated"] = {"extends": "codex", "label": "Keep me"}
        runtime.write_json(paseo_config, providers)
        subprocess.run(args, check=True, capture_output=True, text=True)
        self.assertEqual(first, runtime.read_json(config_file))
        self.assertEqual(reader.read_text(), patched_reader)
        self.assertEqual(list(reader.parent.glob("agent.js.claude-codex-backup-*")), backups)
        self.assertEqual(installed_runtime.read_bytes(), (ROOT / "scripts" / "claude_codex.py").read_bytes())
        self.assertEqual(runtime.read_json(auth)["refresh_token"], "dummy-preserve-only")
        self.assertEqual(runtime.read_json(paseo_config), providers)
        # A user-managed Paseo reinstall replaces its files; reapplying this
        # installer must repair that fresh reader, not trust a settings marker.
        reader.write_text(original_reader)
        subprocess.run(args, check=True, capture_output=True, text=True)
        self.assertEqual(reader.read_text(), patched_reader)
        self.assertEqual(len(list(reader.parent.glob("agent.js.claude-codex-backup-*"))), 2)
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

    def test_unsupported_paseo_reader_fails_before_installation_changes(self):
        paseo = self.fake_paseo("paseo-original", "print('original Paseo')\n")
        reader = paseo_compat.find_usage_reader(paseo)
        reader.write_text("// unfamiliar upstream implementation\nexport {};\n")
        original = reader.read_bytes()
        args = self.staged_args("--paseo-bin", str(paseo))
        with patch.object(install.Runtime, "stop") as stop, \
             patch.object(install, "atomic_write") as write, \
             patch.object(install, "write_json") as write_json:
            with self.assertRaisesRegex(runtime.SetupError, "Unsupported Paseo"):
                install.install(install.parser().parse_args(args))
        stop.assert_not_called()
        write.assert_not_called()
        write_json.assert_not_called()
        self.assertEqual(reader.read_bytes(), original)
        self.assertFalse(list(reader.parent.glob("agent.js.claude-codex-backup-*")))
        self.assertFalse((self.base / "paseo-home" / "config.json").exists())

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

    def test_installer_reuses_system_paseo_over_saved_private_copy(self):
        claude = self.fake_cli("claude-original", "print('Claude Code')\n")
        system_paseo = self.fake_paseo("paseo", "print('paseo on PATH')\n")
        private_paseo = self.fake_paseo("paseo-private", "print('private paseo')\n")
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
        def check_activation(command, **kwargs):
            self.assertIn(paseo_compat.PATCH_MARKER, paseo_compat.find_usage_reader(system_paseo).read_text())
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(os.environ, {"PATH": test_path}), patch.object(paseo_compat, "check_syntax"), \
             patch.object(install.subprocess, "run", side_effect=check_activation) as run:
            install.install(install.parser().parse_args(args))
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [[str(system_paseo), "daemon", "restart"], [str(system_paseo), "reload"]])
        saved = runtime.read_json(Path(self.settings["config_dir"]) / "settings.json")
        self.assertEqual(saved["paseo_bin"], str(system_paseo))
        self.assertEqual(system_paseo.read_bytes(), original)
        self.assertFalse((Path(self.settings["data_dir"]) / "npm" / "paseo").exists())
        wrapper = Path(self.settings["bin_dir"]) / "paseo-codex"
        result = subprocess.run([str(wrapper), "--version"], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "paseo on PATH")

    def test_explicit_paseo_is_used_without_version_checks(self):
        custom = self.fake_cli("paseo-custom", "print('custom paseo')\n")
        with patch.object(install.subprocess, "check_output") as version_probe:
            self.assertEqual(install.detect_paseo(str(custom)), str(custom))
        version_probe.assert_not_called()

    def test_missing_system_paseo_does_not_select_legacy_private_copy(self):
        private = self.base / "npm" / "paseo" / "node_modules" / ".bin" / "paseo"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\necho legacy private paseo\n")
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
