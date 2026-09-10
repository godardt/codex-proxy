#!/usr/bin/env python3
"""One-command, per-user installer. No sudo and no global npm installs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from claude_codex import (
    CONTEXT_WINDOW, EFFORTS, REASONING_MODES, ULTRACODE_MODEL, MARKER, MODEL, Runtime, SetupError, atomic_write, model_id,
    file_lock, proxy_config, read_json, say, write_json,
)

PROXY_VERSION = "7.2.155"
CLAUDE_VERSION = "2.1.246"
# Published release checksums, pinned along with the default version.
CHECKSUMS = {
    "darwin_aarch64": "f90c503ce41a798c85b6f61dfe5fe8b812c1b889634f0c80d04ee376424fe305",
    "darwin_amd64": "198794a2fafb9fb8083476ac18232647c57d443422aa3f008d19ed7e75ca4604",
    "linux_aarch64_no-plugin": "ac1a8a38541cbdf4b83cb21e700e452340b31f76583d6effff97114259cc22a9",
    "linux_amd64_no-plugin": "2e4d1280baf48cb9184eb9e95c92079848202bb2ab9c23341176e9a4eca88881",
}


def absolute(value):
    return Path(value).expanduser().resolve()


def backup(path):
    path = Path(path)
    if path.exists():
        destination = path.with_name(f"{path.name}.claude-codex-backup-{time.time_ns()}")
        shutil.copy2(path, destination)
        destination.chmod(0o600)
        say(f"Backup: {destination}")


def download(url, path):
    subprocess.run([
        "curl", "--fail", "--location", "--show-error", "--silent",
        "--retry", "3", "--connect-timeout", "20", "--max-time", "600",
        "--proto", "=https", "--proto-redir", "=https", "--output", str(path), url,
    ], check=True)


def install_proxy(data_dir, version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SetupError("--proxy-version must be a numeric release, such as 7.2.155")
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        platform.machine().lower()
    )
    if system not in ("linux", "darwin") or not arch:
        raise SetupError("Supported platforms: Linux/WSL2 and macOS, x86_64 or ARM64")
    # The Linux no-plugin build is static and avoids requiring a particular glibc.
    target = f"{system}_{arch}" + ("_no-plugin" if system == "linux" else "")
    destination = data_dir / "releases" / version / "cli-proxy-api"
    if destination.is_file() and os.access(destination, os.X_OK):
        return destination
    filename = f"CLIProxyAPI_{version}_{target}.tar.gz"
    base = f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{version}"
    say(f"Downloading CLIProxyAPI {version} ({target})...")
    with tempfile.TemporaryDirectory(prefix="claude-codex-download-") as temp:
        archive = Path(temp) / filename
        download(f"{base}/{filename}", archive)
        expected = CHECKSUMS.get(target) if version == PROXY_VERSION else None
        if expected is None:
            checksum_file = Path(temp) / "checksums.txt"
            download(f"{base}/checksums.txt", checksum_file)
            for line in checksum_file.read_text().splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1].lstrip("*") == filename:
                    expected = fields[0]
                    break
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected is None or not secrets.compare_digest(digest, expected):
            raise SetupError(f"SHA-256 verification failed for {filename}")
        # Extract only a regular binary file; never extract arbitrary archive paths.
        with tarfile.open(archive, "r:gz") as tar:
            matches = [member for member in tar.getmembers()
                       if Path(member.name).name == "cli-proxy-api" and member.isfile()]
            if len(matches) != 1:
                raise SetupError("Release archive does not contain exactly one cli-proxy-api binary")
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = destination.with_suffix(".new")
            with tar.extractfile(matches[0]) as src, stage.open("wb") as out:
                shutil.copyfileobj(src, out)
            stage.chmod(0o700)
            os.replace(stage, destination)
    return destination


def executable(value, name):
    found = shutil.which(str(value))
    if not found:
        raise SetupError(f"Cannot find executable {name}: {value}")
    # Preserve the entrypoint symlink; npm CLIs can depend on their invocation path.
    return str(Path(found).absolute())


def resolve_cli(name, explicit, package, data_dir, previous=None):
    if explicit:
        return executable(explicit, name)
    prefix = data_dir / "npm" / name
    candidates = [shutil.which(name), previous, str(prefix / "node_modules" / ".bin" / name)]
    for found in candidates:
        if not found or not Path(found).is_file() or not os.access(found, os.X_OK):
            continue
        return str(Path(found).absolute())
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise SetupError(f"Installing missing {name} requires Node.js 22+ and npm. Install them and rerun.")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 22:
        raise SetupError(f"Installing {name} requires Node.js 22+; found {version}")
    say(f"Installing {package} privately in {prefix}...")
    subprocess.run([npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund", package], check=True)
    return executable(prefix / "node_modules" / ".bin" / name, name)


def detect_paseo(explicit):
    # Paseo is managed by the user. Never install it or select a cached version
    # from an earlier claude-codex installation over the user's PATH.
    if explicit:
        return executable(explicit, "paseo")
    found = shutil.which("paseo")
    return str(Path(found).absolute()) if found else None


def provider(settings):
    return {
        "extends": "claude",
        "label": "Claude Codex · GPT-6 Astra",
        "description": "Claude Code harness via CLIProxyAPI and ChatGPT subscription OAuth",
        "command": [str(Path(settings["bin_dir"]) / "claude-codex")],
        "enabled": True,
        # No CLAUDE_CONFIG_DIR here: Paseo reloads transcripts from the profile
        # its daemon resolves, so the launcher keeps that profile for Paseo.
        "env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{settings['port']}",
            # Advertises gateway authentication to Paseo's availability detection.
            # The launcher replaces this placeholder with the private local key.
            "ANTHROPIC_AUTH_TOKEN": "provided-by-claude-codex-launcher",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(CONTEXT_WINDOW),
            "CLAUDE_CODEX_PASEO_USAGE": "1",
        },
        "disallowedTools": ["WebSearch"],
        "models": [{
            "id": model_id(effort),
            "label": f"GPT-6 Astra · {effort}",
            "isDefault": effort == settings["reasoning"],
            "thinkingOptions": ([
                {"id": "default", "label": "Standard", "isDefault": True},
                {"id": "ultracode", "label": "Ultra Code",
                 "description": "Claude Code dynamic workflows with xhigh reasoning"},
            ] if effort == "xhigh" else []),
        } for effort in EFFORTS] + [{
            "id": ULTRACODE_MODEL,
            "label": "GPT-6 Astra · Ultra Code",
            "description": "Claude Code dynamic workflows with xhigh reasoning",
            "isDefault": settings["reasoning"] == "ultracode",
            "thinkingOptions": [{"id": "ultracode", "label": "Ultra Code", "isDefault": True}],
        }],
    }


def load_paseo(path):
    config = read_json(path) if path.exists() else {"version": 1}
    if config.get("version", 1) != 1:
        raise SetupError(f"Unsupported Paseo configuration version in {path}")
    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise SetupError(f"agents must be an object in {path}")
    providers = agents.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise SetupError(f"agents.providers must be an object in {path}")
    return config


def merge_paseo(path, settings, previous):
    config = load_paseo(path)
    providers = config["agents"]["providers"]
    if "claude-codex" in providers and str(path) != previous.get("paseo_config"):
        raise SetupError(f"{path} already defines claude-codex; rename that provider before installing")
    updated = provider(settings)
    if providers.get("claude-codex") == updated:
        return
    providers["claude-codex"] = updated
    backup(path)
    write_json(path, config)


def write_launcher(path, command, environment=None, path_prepend=None):
    path = Path(path)
    if path.exists() or path.is_symlink():
        try:
            owned = MARKER in path.read_text()
        except (OSError, UnicodeError):
            owned = False
        if not owned:
            raise SetupError(f"Refusing to overwrite an existing unrelated program: {path}")
    lines = ["#!/bin/sh", MARKER]
    for name, value in (environment or {}).items():
        lines.append(f"export {name}={shlex.quote(str(value))}")
    if path_prepend is not None:
        lines.append(f'export PATH={shlex.quote(str(path_prepend))}:"$PATH"')
    lines.append("exec " + shlex.join([str(part) for part in command]) + ' "$@"')
    atomic_write(path, "\n".join(lines) + "\n", mode=0o755)


def add_path(bin_dir):
    home_dir = Path.home()
    shell = Path(os.environ.get("SHELL", "bash")).name
    if shell == "zsh":
        zsh_dir = absolute(os.environ.get("ZDOTDIR", str(home_dir)))
        profiles = [zsh_dir / ".zshrc", zsh_dir / ".zprofile"]
    elif shell == "bash":
        login = next((home_dir / name for name in (".bash_profile", ".bash_login", ".profile")
                      if (home_dir / name).exists()), home_dir / ".profile")
        profiles = [home_dir / ".bashrc", login]
    elif shell == "fish":
        config_dir = absolute(os.environ.get("XDG_CONFIG_HOME", str(home_dir / ".config")))
        target = config_dir / "fish" / "conf.d" / "claude-codex.fish"
        # POSIX single-quote escaping is also accepted by fish for ordinary paths.
        content = f"{MARKER}\nfish_add_path -- {shlex.quote(str(bin_dir))}\n"
        if not target.exists() or target.read_text() != content:
            backup(target)
            atomic_write(target, content)
        return
    else:
        profiles = [home_dir / ".profile"]
    block = f"\n{MARKER}\nexport PATH={shlex.quote(str(bin_dir))}:\"$PATH\"\n"
    for path in profiles:
        old = path.read_text() if path.exists() else ""
        if block.strip() in old:
            continue
        backup(path)
        # Preserve symlinks used by dotfile managers.
        target = path.resolve() if path.is_symlink() else path
        atomic_write(target, old + block, mode=path.stat().st_mode & 0o777 if path.exists() else 0o644)


def parser():
    p = argparse.ArgumentParser(description="Install claude-codex and CLIProxyAPI; configure Paseo only if detected")
    p.add_argument("--reasoning", choices=REASONING_MODES, help="Default reasoning level (first install: high)")
    p.add_argument("--port", type=int, help="Local proxy port (first install: 8317)")
    p.add_argument("--config-dir", default=os.path.join(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")), "claude-codex"))
    p.add_argument("--data-dir", default=os.path.join(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")), "claude-codex"))
    p.add_argument("--state-dir", default=os.path.join(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")), "claude-codex"))
    p.add_argument("--bin-dir", default=str(Path.home() / ".local/bin"))
    p.add_argument("--paseo-home", default=os.environ.get("PASEO_HOME", str(Path.home() / ".paseo")))
    p.add_argument("--claude-bin", help="Existing Claude executable to use")
    p.add_argument("--paseo-bin", help="Existing Paseo executable to use")
    p.add_argument("--proxy-binary", help="Use an existing trusted CLIProxyAPI binary instead of downloading")
    p.add_argument("--proxy-version", default=PROXY_VERSION)
    p.add_argument("--device-login", action="store_true", help="Use ChatGPT device-code login")
    p.add_argument("--no-browser", action="store_true", help="Print OAuth URL without opening a browser")
    p.add_argument("--skip-login", action="store_true", help="Stage installation without authenticating")
    p.add_argument("--skip-smoke-test", action="store_true", help="Skip the small live Astra verification request")
    p.add_argument("--skip-paseo", action="store_true", help="Install terminal integration only")
    p.add_argument("--skip-paseo-start", action="store_true", help="Write Paseo configuration without restarting its daemon")
    p.add_argument("--no-path", action="store_true", help="Do not edit shell startup files")
    return p


def install(opts):
    if sys.version_info < (3, 9):
        raise SetupError("Python 3.9+ is required")
    if sys.platform not in ("linux", "darwin"):
        raise SetupError("Run this installer on Linux/WSL2 or macOS")
    if not opts.proxy_binary and not shutil.which("curl"):
        raise SetupError("curl is required to download CLIProxyAPI")
    config_dir, data_dir, state_dir, bin_dir = (
        absolute(getattr(opts, key)) for key in ("config_dir", "data_dir", "state_dir", "bin_dir")
    )
    with file_lock(config_dir / "install.lock"):
        return _install_locked(opts, config_dir, data_dir, state_dir, bin_dir)


def _install_locked(opts, config_dir, data_dir, state_dir, bin_dir):
    settings_file = config_dir / "settings.json"
    previous = read_json(settings_file) if settings_file.exists() else {}
    port = opts.port if opts.port is not None else previous.get("port", 8317)
    if not 1024 <= port <= 65535:
        raise SetupError("--port must be between 1024 and 65535")
    detected_paseo = None if opts.skip_paseo else detect_paseo(opts.paseo_bin)
    if detected_paseo and (Path(detected_paseo).name == "paseo-codex"
                           or Path(detected_paseo).resolve() == (bin_dir / "paseo-codex").resolve()):
        raise SetupError("--paseo-bin must point to the original Paseo executable")
    use_paseo = detected_paseo is not None
    if not use_paseo:
        say("Paseo integration skipped" if opts.skip_paseo else "Paseo not detected; skipping Paseo installation and configuration")
    paseo_home = absolute(opts.paseo_home)
    paseo_config = paseo_home / "config.json"
    if use_paseo:
        config = load_paseo(paseo_config)
        if "claude-codex" in config["agents"]["providers"] and str(paseo_config) != previous.get("paseo_config"):
            raise SetupError(f"{paseo_config} already has an unrelated claude-codex provider")
    for path in (config_dir, data_dir, state_dir, config_dir / "auth", config_dir / "claude"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher_names = ["claude-codex", "claude-codex-proxy"] + (["paseo-codex"] if use_paseo else [])
    for name in launcher_names:
        path = bin_dir / name
        if path.exists() or path.is_symlink():
            try:
                owned = MARKER in path.read_text()
            except (OSError, UnicodeError):
                owned = False
            if not owned:
                raise SetupError(f"Refusing to overwrite unrelated program {path}")
    claude_bin = resolve_cli("claude", opts.claude_bin, f"@anthropic-ai/claude-code@{CLAUDE_VERSION}", data_dir, previous.get("claude_bin"))
    if Path(claude_bin).name == "claude-codex" or Path(claude_bin).resolve() == (bin_dir / "claude-codex").resolve():
        raise SetupError("--claude-bin must point to the original Claude executable")
    paseo_bin = detected_paseo
    if paseo_bin:
        say(f"Using installed Paseo: {paseo_bin}")
    proxy_bin = executable(opts.proxy_binary, "CLIProxyAPI") if opts.proxy_binary else str(install_proxy(data_dir, opts.proxy_version))
    settings = {
        "config_dir": str(config_dir), "data_dir": str(data_dir), "state_dir": str(state_dir),
        "bin_dir": str(bin_dir), "port": port, "proxy_bin": proxy_bin, "claude_bin": claude_bin,
        "api_key": previous.get("api_key") or secrets.token_urlsafe(32),
        "reasoning": opts.reasoning or previous.get("reasoning", "high"),
        "node_dir": str(Path(shutil.which("node")).parent) if shutil.which("node") else None,
        "proxy_version": opts.proxy_version if not opts.proxy_binary else "external",
    }
    if paseo_bin:
        settings.update(paseo_bin=paseo_bin, paseo_config=str(paseo_config))
    elif previous.get("paseo_config"):
        settings.update(paseo_config=previous["paseo_config"], paseo_bin=previous.get("paseo_bin"))
    # Stop only the previously managed process, using its old binary and port.
    if previous:
        Runtime(previous).stop()
    runtime_file = data_dir / "claude_codex.py"
    atomic_write(runtime_file, Path(__file__).with_name("claude_codex.py").read_text())
    write_json(settings_file, settings)
    write_json(config_dir / "proxy.yaml", proxy_config(settings))
    # Keep the native claude profile untouched. Project CLAUDE.md and settings
    # still load through Claude Code's normal project settings mechanism.
    claude_settings = config_dir / "claude" / "settings.json"
    if not claude_settings.exists():
        write_json(claude_settings, {"permissions": {"deny": ["WebSearch"]}})
    python_bin = str(Path(sys.executable).absolute())
    for name, mode in (("claude-codex", "launch"), ("claude-codex-proxy", "proxy")):
        write_launcher(bin_dir / name, [python_bin, runtime_file, mode, settings_file])
    if paseo_bin:
        merge_paseo(paseo_config, settings, previous)
        paseo_env = {"PASEO_HOME": str(paseo_home)}
        write_launcher(bin_dir / "paseo-codex", [paseo_bin], paseo_env, path_prepend=settings["node_dir"])
    if not opts.no_path:
        add_path(bin_dir)
    runtime = Runtime(settings)
    if not opts.skip_login:
        if not runtime.has_login():
            runtime.login(opts.device_login, opts.no_browser)
        runtime.doctor(smoke=not opts.skip_smoke_test)
    if paseo_bin and not opts.skip_paseo_start:
        env = dict(os.environ)
        env["PASEO_HOME"] = str(paseo_home)
        # Explicitly target this machine; ignore inherited remote daemon selectors.
        env.pop("PASEO_HOST", None)
        say(f"Restarting the local Paseo daemon with {paseo_bin}...")
        subprocess.run([paseo_bin, "daemon", "restart"], env=env, check=True)
        subprocess.run([paseo_bin, "reload"], env=env, check=True)
    say(f"Installed: {bin_dir / 'claude-codex'}")
    say(f"Run: {shlex.quote(str(bin_dir / 'claude-codex'))} --reasoning high")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f'For this terminal: export PATH={shlex.quote(str(bin_dir))}:"$PATH" (or open a new terminal)')
    if paseo_bin:
        say("In Paseo, select 'Claude Codex · GPT-6 Astra', then select a reasoning variant in the model picker.")
    if opts.skip_login:
        say("Installation staged; login and live verification were skipped. Run claude-codex-proxy login when ready.")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        install(parser().parse_args())
    except (SetupError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        say(f"Install failed: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        say("Interrupted. Rerun install.sh to continue.")
        sys.exit(130)
