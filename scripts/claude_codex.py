#!/usr/bin/env python3
"""Claude Code launcher and local CLIProxyAPI lifecycle, using only the stdlib."""

import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MODEL = "gpt-6-astra"
CONTEXT_WINDOW = 1_050_000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
REASONING_MODES = (*EFFORTS, "ultracode")
ULTRACODE_MODEL = "gpt-6-astra-ultracode"
MARKER = "# Managed by claude-codex installer"

# Native Claude options with operands, including hidden SDK options. Keep this
# in sync with supported CLI versions; unknown flags still pass through.
CLAUDE_VARIADIC_OPTIONS = {
    "--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
    "--disallowed-tools", "--file", "--mcp-config", "--tools",
}
CLAUDE_VALUE_OPTIONS = CLAUDE_VARIADIC_OPTIONS | {
    "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
    "--autocompact", "--debug-file", "--environment", "--fallback-model", "--input-format",
    "--json-schema", "--max-budget-usd", "--max-turns", "--name", "-n", "--output-format",
    "--permission-mode", "--permission-prompt-tool", "--permission-prompts", "--plugin-dir",
    "--plugin-url", "--remote-control-session-name-prefix", "--resume-session-at",
    "--rewind-files", "--sdk-url", "--session-id", "--setting-sources", "--settings",
    "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot",
}
CLAUDE_OPTIONAL_VALUE_OPTIONS = {
    "--cloud", "--debug", "-d", "--from-pr", "--prompt-suggestions", "--remote-control",
    "--resume", "-r", "--teleport", "--worktree", "-w",
}

# Claude's auto mode judges every tool call with Anthropic's classifier models.
# CLIProxyAPI cannot serve those from a Codex subscription, so Claude falls back
# to running the classifier prompt on the session model, Astra, at the session's
# reasoning effort: each call adds a full-transcript request, verdicts come from
# a model the prompt was not written for, and a denial offers no approval prompt.
# Claude's own setting turns the mode off; the session falls back to its normal
# permission prompts instead. Set CLAUDE_CODEX_AUTO_MODE=1 to keep auto mode.
LAUNCHER_SETTINGS = {"permissions": {"disableAutoMode": "disable"}}
AUTO_MODE_OVERRIDE = "CLAUDE_CODEX_AUTO_MODE"


class SetupError(Exception):
    pass


def say(message):
    # stdout belongs to Claude's stream-json protocol when called by Paseo.
    print(message, file=sys.stderr, flush=True)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SetupError(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Expected a JSON object in {path}")
    return value


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


@contextlib.contextmanager
def file_lock(path, timeout=None, busy_message="Another operation is busy; try again shortly"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        os.fchmod(handle.fileno(), 0o600)
        if timeout is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SetupError(busy_message)
                    time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def effort_value(value):
    if value not in REASONING_MODES:
        raise SetupError(f"Reasoning must be one of: {', '.join(REASONING_MODES)} (got {value!r})")
    return value


def model_id(effort):
    effort = effort_value(effort)
    return ULTRACODE_MODEL if effort == "ultracode" else f"{MODEL}({effort})"


def proxy_config(settings):
    # JSON is valid YAML. Emitting JSON avoids a PyYAML dependency and safely
    # quotes paths and keys for CLIProxyAPI's YAML parser.
    return {
        "host": "127.0.0.1",
        "port": settings["port"],
        "auth-dir": str(Path(settings["config_dir"]) / "auth"),
        "api-keys": [settings["api_key"]],
        "remote-management": {
            "allow-remote": False, "secret-key": "", "disable-control-panel": True,
        },
        "debug": False,
        "request-log": False,
        "logging-to-file": True,
        "logs-max-total-size-mb": 20,
        "error-logs-max-files": 0,
        "usage-statistics-enabled": False,
        "request-retry": 2,
        "max-retry-interval": 5,
        "quota-exceeded": {"switch-project": False, "switch-preview-model": False},
        "claude-code": {"disable-cloaking-model-list": True},
        "oauth-model-alias": {"codex": [{"name": MODEL, "alias": ULTRACODE_MODEL, "fork": True}]},
        "payload": {"override": [{
            "models": [{"name": ULTRACODE_MODEL, "protocol": "codex"}],
            "params": {"reasoning.effort": "xhigh"},
        }]},
    }


def parse_launch_args(args, default_effort):
    """Consume our reasoning option, normalize Astra selections, preserve SDK args."""
    forwarded = []
    selected = None
    explicit_effort = None
    native_effort = None
    tail = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            tail = args[i:]
            break
        key, equal, value = arg.partition("=")
        if key in ("--reasoning", "--model", "--effort"):
            if not equal:
                i += 1
                if i >= len(args):
                    raise SetupError(f"{key} needs a value")
                value = args[i]
            if key == "--reasoning":
                explicit_effort = effort_value(value)
            elif key == "--effort":
                native_effort = value
            else:
                selected = value
        else:
            forwarded.append(arg)
            # Required operands belong to the native option even if they look
            # like our selectors or the literal "--" separator.
            if key in CLAUDE_VALUE_OPTIONS and not equal:
                i += 1
                if i >= len(args):
                    raise SetupError(f"{key} needs a value")
                forwarded.append(args[i])
            if key in CLAUDE_VARIADIC_OPTIONS or (key in CLAUDE_OPTIONAL_VALUE_OPTIONS and not equal):
                while i + 1 < len(args) and (not args[i + 1].startswith("-") or args[i + 1] == "-"):
                    i += 1
                    forwarded.append(args[i])
                    if key not in CLAUDE_VARIADIC_OPTIONS:
                        break
        i += 1
    suffix_effort = None
    if selected == ULTRACODE_MODEL:
        suffix_effort = "ultracode"
    elif selected:
        match = re.fullmatch(re.escape(MODEL) + r"(?:\(([^()]+)\))?", selected)
        if not match and selected not in ("default", "opus", "sonnet", "haiku", "best", "fable"):
            raise SetupError(f"This launcher targets {MODEL}; unsupported --model {selected!r}")
        if match and match.group(1):
            suffix_effort = effort_value(match.group(1))
    if explicit_effort and suffix_effort and explicit_effort != suffix_effort:
        raise SetupError("--reasoning conflicts with the effort in --model")
    effort = explicit_effort or suffix_effort or effort_value(default_effort)
    if effort == "ultracode":
        if native_effort and native_effort not in ("xhigh", "ultracode"):
            raise SetupError("Ultra Code requires xhigh reasoning; conflicting --effort")
        native_effort = "ultracode"
    native_args = ["--effort", native_effort] if native_effort else []
    return ["--model", model_id(effort), *forwarded, *native_args, *tail], effort


def auto_mode_allowed(env):
    return env.get(AUTO_MODE_OVERRIDE) == "1"


def merge_settings(base, updates):
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def merged_settings_operand(value, updates):
    """Layer launcher settings onto a native --settings operand (JSON or file path)."""
    text = value.strip()
    try:
        current = json.loads(text) if text.startswith("{") else json.loads(Path(value).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise SetupError(f"Cannot merge launcher settings into --settings {value!r}: {exc}") from exc
    if not isinstance(current, dict):
        raise SetupError(f"--settings must contain a JSON object: {value!r}")
    return json.dumps(merge_settings(current, updates), separators=(",", ":"))


def apply_launcher_settings(args, updates=LAUNCHER_SETTINGS):
    """Add launcher-owned settings, merging into a caller's --settings if present.

    Claude reads a single --settings operand; a second one would replace the
    caller's, such as Paseo's Ultra Code or fast-mode settings.
    """
    args = list(args)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break  # Everything after a standalone separator is literal prompt text.
        key, equal, value = arg.partition("=")
        if key == "--settings":
            if equal:
                args[i] = key + "=" + merged_settings_operand(value, updates)
            elif i + 1 < len(args):
                args[i + 1] = merged_settings_operand(args[i + 1], updates)
            return args
        if key in CLAUDE_VALUE_OPTIONS and not equal:
            i += 1  # A required operand is never an option or the separator, even if it looks like one.
        i += 1
    return args[:i] + ["--settings", json.dumps(updates, separators=(",", ":"))] + args[i:]


def isolated_profile(settings):
    return str(Path(settings["config_dir"]) / "claude")


def paseo_mode(env):
    # Set by the generated Paseo provider entry; never by terminal launches.
    return env.get("CLAUDE_CODEX_PASEO_USAGE") == "1"


def daemon_profile(env):
    """The Claude profile Paseo's daemon reads transcripts from.

    Paseo resolves history files from its own CLAUDE_CONFIG_DIR, or ~/.claude,
    not from the environment of the provider it spawns.
    """
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_env(settings, effort, source=None):
    env = dict(os.environ if source is None else source)
    for name in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "CLAUDE_CODE_EFFORT_LEVEL", "MAX_THINKING_TOKENS",
    ):
        env.pop(name, None)
    selected = model_id(effort)
    env.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings['port']}",
        "ANTHROPIC_AUTH_TOKEN": settings["api_key"],
        "ANTHROPIC_MODEL": selected,
        "ANTHROPIC_DEFAULT_MODEL": selected,
        "ANTHROPIC_SMALL_FAST_MODEL": selected,
        "CLAUDE_CODE_SUBAGENT_MODEL": selected,
        # Declare Astra's full window for this non-Claude gateway model ID.
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(CONTEXT_WINDOW),
        "API_TIMEOUT_MS": "600000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "DISABLE_AUTOUPDATER": "1",
    })
    for alias in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        env[f"ANTHROPIC_DEFAULT_{alias}_MODEL"] = selected
    if paseo_mode(env):
        # Paseo reloads an agent's transcript from the profile its daemon
        # resolves. Sessions recorded in the isolated profile are invisible to
        # it, so the conversation appears empty after a daemon restart or
        # reload. Provider entries written by older installers still pin the
        # isolated profile; drop that so Claude uses the daemon's profile.
        if env.get("CLAUDE_CONFIG_DIR") == isolated_profile(settings):
            del env["CLAUDE_CONFIG_DIR"]
    else:
        env["CLAUDE_CONFIG_DIR"] = isolated_profile(settings)
    # Local traffic must bypass inherited HTTP proxies, including in GUI daemons.
    for name in ("NO_PROXY", "no_proxy"):
        env[name] = ",".join(filter(None, [env.get(name), "127.0.0.1", "localhost"]))
    if settings.get("node_dir"):
        env["PATH"] = settings["node_dir"] + os.pathsep + env.get("PATH", os.defpath)
    return env


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.config_dir = Path(settings["config_dir"])
        self.state_dir = Path(settings["state_dir"])
        self.config_file = self.config_dir / "proxy.yaml"
        self.pid_file = self.state_dir / "proxy.pid"
        self.log_file = self.state_dir / "proxy-startup.log"
        self.child = None

    @property
    def command(self):
        return [self.settings["proxy_bin"], "-config", str(self.config_file)]

    def lock(self):
        return file_lock(self.state_dir / "proxy.lock", timeout=40,
                         busy_message="Another proxy operation is busy; try again shortly")

    def request(self, path, payload=None, timeout=3):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.settings['port']}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.settings['api_key']}",
                     "Content-Type": "application/json", "anthropic-version": "2023-06-01"},
        )
        # This client only ever connects to loopback, regardless of HTTP_PROXY.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)

    def healthy(self):
        try:
            result = self.request("/v1/models")
            return isinstance(result, dict) and isinstance(result.get("data"), list)
        except (OSError, ValueError):
            return False

    def owned_pid(self):
        try:
            pid = int(self.pid_file.read_text().strip())
            if pid <= 1:
                return None
            proc = Path(f"/proc/{pid}/cmdline")
            if sys.platform.startswith("linux"):
                actual = proc.read_bytes().rstrip(b"\0").decode().split("\0")
                return pid if actual == self.command else None
            actual = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="], text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            return pid if actual == " ".join(self.command) else None
        except (OSError, ValueError, subprocess.CalledProcessError):
            return None

    def start(self):
        with self.lock():
            if self.healthy():
                return
            if self.owned_pid():
                raise SetupError("Managed proxy is running but unhealthy; use claude-codex-proxy restart")
            try:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", self.settings["port"]))
            except OSError as exc:
                raise SetupError(
                    f"Port {self.settings['port']} is occupied by another process. "
                    "Rerun install.sh with --port <unused-port>."
                ) from exc
            # Rotate the small startup log; CLIProxyAPI rotates its own application logs.
            if self.log_file.exists() and self.log_file.stat().st_size > 1024 * 1024:
                os.replace(self.log_file, self.log_file.with_suffix(".log.1"))
            with self.log_file.open("ab") as log:
                os.chmod(self.log_file, 0o600)
                child = subprocess.Popen(
                    self.command, cwd=self.state_dir, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    close_fds=True,
                )
            self.child = child
            atomic_write(self.pid_file, str(child.pid) + "\n")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                if self.healthy():
                    return
                time.sleep(0.2)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            self.pid_file.unlink(missing_ok=True)
            raise SetupError(f"CLIProxyAPI did not start. Inspect {self.log_file}")

    def stop(self):
        with self.lock():
            pid = self.owned_pid()
            if pid:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while self.owned_pid() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if self.owned_pid():
                    raise SetupError("Proxy is still shutting down; try status again shortly")
            elif self.healthy():
                raise SetupError("Proxy responds, but its process ownership cannot be verified; refusing to stop it")
            if self.child is not None:
                self.child.wait(timeout=5)
                self.child = None
            self.pid_file.unlink(missing_ok=True)

    def login_credentials(self):
        credentials = {}
        for path in (self.config_dir / "auth").glob("*.json"):
            try:
                auth = read_json(path)
                if auth.get("type") == "codex" and auth.get("refresh_token") and not auth.get("disabled"):
                    stat = path.stat()
                    credentials[path] = (auth, stat.st_dev, stat.st_ino, stat.st_mtime_ns)
            except (SetupError, OSError):
                continue
        return credentials

    def has_login(self):
        return bool(self.login_credentials())

    def login(self, device=False, no_browser=False):
        self.stop()
        previous = self.login_credentials()
        args = [*self.command, "-codex-device-login" if device else "-codex-login"]
        if no_browser:
            args.append("-no-browser")
        say("Sign in with the ChatGPT account whose subscription you want to use.")
        subprocess.run(args, cwd=self.state_dir, check=True)
        # Some CLIProxyAPI login failures are logged but return exit status 0.
        # Old credentials cannot prove this attempt saved a login. File identity
        # and mtime also recognize successful rewrites of identical credentials.
        if not any(previous.get(path) != credential for path, credential in self.login_credentials().items()):
            raise SetupError("No new or updated Codex OAuth credentials were saved. Run claude-codex-proxy login again.")
        for path in (self.config_dir / "auth").glob("*.json"):
            path.chmod(0o600)

    def doctor(self, smoke=False):
        self.start()
        if not self.has_login():
            raise SetupError("ChatGPT login is missing. Run claude-codex-proxy login")
        models = self.request("/v1/models")
        if MODEL not in {item.get("id") for item in models["data"]}:
            raise SetupError(f"CLIProxyAPI does not advertise {MODEL}. Reauthenticate or update CLIProxyAPI.")
        say(f"Proxy ready at http://127.0.0.1:{self.settings['port']}; {MODEL} is in its catalog.")
        if smoke:
            try:
                result = self.request("/v1/messages", {
                    "model": model_id(self.settings["reasoning"]),
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                }, timeout=180)
            except urllib.error.HTTPError as exc:
                raise SetupError(
                    f"Astra request failed (HTTP {exc.code}). Check account access, subscription quota, "
                    f"and the proxy logs in {self.state_dir / 'logs'}."
                ) from exc
            if result.get("type") != "message" or not result.get("content"):
                raise SetupError("Proxy returned no Anthropic-format message during the smoke test")
            say("Live Anthropic Messages → Codex OAuth → Astra request succeeded.")
        else:
            say("Catalog checks do not prove account entitlement; use doctor --smoke-test for a live request.")


class PaseoUsageStream:
    """Use final per-request counts instead of Claude's provisional input estimate.

    CLIProxyAPI receives Codex usage at the end of an HTTP response. Claude
    fills the initial zero input count with a local estimate, which Paseo
    otherwise treats as authoritative even after the real usage arrives.
    """

    def __init__(self):
        self.last_usage = None

    def normalize(self, line):
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError):
            return line
        if not isinstance(message, dict) or message.get("parent_tool_use_id"):
            return line
        changed = False
        if message.get("type") == "stream_event":
            event = message.get("event")
            if not isinstance(event, dict):
                return line
            if event.get("type") == "message_start":
                self.last_usage = None
                body = event.get("message")
                if isinstance(body, dict) and "usage" in body:
                    # Preserve the event and streamed content, but mark the
                    # provisional usage as unknown so Paseo uses final usage.
                    del body["usage"]
                    changed = True
            elif event.get("type") == "message_delta":
                usage = event.get("usage")
                if isinstance(usage, dict) and "input_tokens" in usage:
                    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens")
                    values = {key: usage.get(key, 0) for key in keys}
                    if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and math.isfinite(value) and value >= 0 for value in values.values()):
                        self.last_usage = values
        elif message.get("type") == "result":
            usage = message.get("usage")
            if isinstance(usage, dict) and not usage.get("iterations") and self.last_usage is not None:
                # SDK totals can accumulate across calls or turns. Paseo reads
                # the last iteration for current context, so provide exactly
                # the last main-agent request, with caches counted separately.
                usage["iterations"] = [self.last_usage]
                changed = True
            self.last_usage = None
        elif message.get("type") == "system" and message.get("subtype") == "compact_boundary":
            self.last_usage = None
        return (json.dumps(message, separators=(",", ":")) + "\n").encode() if changed else line


def resumed_session(args):
    """Return the session ID given to a native --resume/-r option, if any."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return None
        key, equal, value = arg.partition("=")
        if key in ("--resume", "-r"):
            if not equal:
                value = args[i + 1] if i + 1 < len(args) else ""
            return value if re.fullmatch(r"[0-9a-fA-F-]{36}", value) else None
        if key in CLAUDE_VALUE_OPTIONS and not equal:
            i += 1  # A required operand is never an option, even if it looks like one.
        i += 1
    return None


def adopt_isolated_transcript(settings, args, env):
    """Move a resumed session recorded by an older launcher into Paseo's profile.

    Before Paseo launches used the daemon's profile, transcripts went to the
    isolated profile. Moving the exact session Paseo resumes lets Claude find
    the conversation and lets Paseo display it after its next reload.
    """
    session_id = resumed_session(args)
    if not session_id:
        return
    source = Path(isolated_profile(settings)) / "projects"
    target = daemon_profile(env) / "projects"
    if not source.is_dir() or (target.exists() and target.samefile(source)):
        return
    for transcript in source.glob(f"*/{session_id}.jsonl"):
        destination = target / transcript.parent.name / transcript.name
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(transcript), str(destination))
        # Subagent and workflow transcripts live in a directory named after the session.
        sidechains = transcript.with_suffix("")
        if sidechains.is_dir() and not destination.with_suffix("").exists():
            shutil.move(str(sidechains), str(destination.with_suffix("")))


def is_stream_json(args):
    options = args[:args.index("--")] if "--" in args else args
    return "--output-format=stream-json" in options or any(
        options[index:index + 2] == ["--output-format", "stream-json"]
        for index in range(len(options))
    )


def run_paseo_stream(binary, args, env):
    # Claude reads the original stdin directly. Only stdout passes through the
    # usage adapter; stderr and the SDK control protocol remain intact.
    child = subprocess.Popen([binary, *args], env=env, stdout=subprocess.PIPE)
    previous_handlers = {}

    def forward_signal(signum, frame):
        if child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, forward_signal)
    normalizer = PaseoUsageStream()
    try:
        for line in child.stdout:
            sys.stdout.buffer.write(normalizer.normalize(line))
            sys.stdout.buffer.flush()
        return_code = child.wait()
        return return_code if return_code >= 0 else 128 - return_code
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        child.stdout.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def launch(settings, args):
    if args == ["--wrapper-help"]:
        print("Usage: claude-codex [--reasoning low|medium|high|xhigh|max|ultracode] [Claude Code arguments]\n"
              "Use claude-codex-proxy --help for login, diagnostics, and proxy controls.")
        return
    # Availability probes must succeed without login or proxy startup.
    probe = args in (["--version"], ["-v"], ["--help"], ["-h"]) or args[:2] == ["auth", "status"]
    if probe:
        forwarded, effort = args, settings["reasoning"]
    else:
        forwarded, effort = parse_launch_args(
            args, os.environ.get("CLAUDE_CODEX_REASONING", settings["reasoning"]),
        )
        if not auto_mode_allowed(os.environ):
            forwarded = apply_launcher_settings(forwarded)
        runtime = Runtime(settings)
        if not runtime.has_login():
            raise SetupError("ChatGPT login is missing. Run claude-codex-proxy login")
        runtime.start()
    binary = settings["claude_bin"]
    env = claude_env(settings, effort)
    if paseo_mode(env):
        if not probe:
            try:
                adopt_isolated_transcript(settings, forwarded, env)
            except OSError as exc:
                say(f"claude-codex: could not move the earlier transcript into Paseo's Claude profile: {exc}")
        if is_stream_json(forwarded):
            raise SystemExit(run_paseo_stream(binary, forwarded, env))
    os.execve(binary, [binary, *forwarded], env)


def control(settings, args):
    parser = argparse.ArgumentParser(prog="claude-codex-proxy")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "stop", "restart", "status", "paths"):
        sub.add_parser(action)
    login = sub.add_parser("login")
    login.add_argument("--device", action="store_true")
    login.add_argument("--no-browser", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--smoke-test", action="store_true", help="Send a small request using subscription quota")
    sub.add_parser("reasoning").add_argument("level", choices=REASONING_MODES)
    opts = parser.parse_args(args)
    runtime = Runtime(settings)
    if opts.action == "login":
        runtime.login(opts.device, opts.no_browser)
        runtime.doctor(smoke=True)
    elif opts.action == "doctor":
        runtime.doctor(opts.smoke_test)
    elif opts.action == "start":
        runtime.start()
        say("Proxy ready")
    elif opts.action == "stop":
        runtime.stop()
        say("Proxy stopped; the next claude-codex launch will start it again")
    elif opts.action == "restart":
        runtime.stop()
        runtime.start()
        say("Proxy restarted")
    elif opts.action == "status":
        if not runtime.healthy():
            raise SetupError("Proxy is stopped or unhealthy")
        say(f"Proxy ready at http://127.0.0.1:{settings['port']}")
    elif opts.action == "reasoning":
        settings["reasoning"] = opts.level
        write_json(runtime.config_dir / "settings.json", settings)
        say(f"Default terminal reasoning: {opts.level}. Paseo keeps its selected model variant.")
    else:
        for key in ("config_dir", "data_dir", "state_dir", "bin_dir", "claude_bin", "paseo_config"):
            print(f"{key}: {settings.get(key, '(not configured)')}")


def main():
    if len(sys.argv) < 3:
        raise SetupError("Run install.sh first, then use the installed launchers")
    mode, settings_file, *args = sys.argv[1:]
    settings = read_json(settings_file)
    if mode == "launch":
        launch(settings, args)
    elif mode == "proxy":
        control(settings, args)
    else:
        raise SetupError(f"Unknown launcher mode: {mode}")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (SetupError, OSError, subprocess.CalledProcessError) as exc:
        say(f"claude-codex: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
