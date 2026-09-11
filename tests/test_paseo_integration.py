"""Optional real Paseo daemon → wrapper → Claude → proxy integration test."""

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_proxy_integration import GatedReadSequence, Upstream, free_port, function_call_events, response_events

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime


def copy_paseo_package(binary, destination):
    """Copy, never hard-link, the CLI and its server before running an installer."""
    entrypoint = Path(binary).resolve(strict=True)
    for root in entrypoint.parents:
        manifest = root / "package.json"
        if manifest.is_file() and json.loads(manifest.read_text()).get("name") == "@getpaseo/cli":
            break
    else:
        raise AssertionError(f"Cannot locate the selected Paseo CLI package for {entrypoint}")
    reader = Path("node_modules/@getpaseo/server/dist/server/server/agent/providers/claude/agent.js")
    if not (root / reader).is_file():
        raise AssertionError(f"Selected CLI has no bundled server usage reader at {root / reader}")
    # Dereference package/.bin links as regular private files too. In particular,
    # no installer patch may resolve back into the user's installed server.
    shutil.copytree(root, destination, symlinks=False)
    private_entrypoint = destination / entrypoint.relative_to(root)
    private_reader = destination / reader
    for private, source in ((private_entrypoint, entrypoint), (private_reader, root / reader)):
        if not private.is_file() or private.is_symlink():
            raise AssertionError(f"Private package entry must be a regular file: {private}")
        if destination.resolve() not in private.resolve().parents:
            raise AssertionError(f"Private package entry resolves outside the copy: {private}")
        if private.samefile(source):
            raise AssertionError(f"Private copy shares an inode with {source}")
    return private_entrypoint


class ForkedSkillScenario:
    """Skill fork → background Agent grandchild → fork result, as Claude Code runs it.

    The fork's final request is held until the grandchild has finished, so the
    child's completion is announced while the fork is still running.
    """

    SKILL = ("---\nname: forky\ndescription: harness fork skill\ncontext: fork\n---\n"
             "FORKY-SKILL-BODY: spawn a background helper and report.\n")
    CHILD_PROMPT = "SUBAGENT-PROMPT say done"

    def __init__(self, project, fork_delay=6.0):
        skill = project / ".claude" / "skills" / "forky" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(self.SKILL)
        self.fork_delay = fork_delay
        self.kinds = []
        self.child_started = threading.Event()
        self.lock = threading.Lock()

    @staticmethod
    def text_of(item):
        content = item.get("content")
        if isinstance(content, str):
            return content
        return " ".join(part.get("text", "") for part in content or [] if isinstance(part, dict))

    def classify(self, body):
        inputs = body.get("input", [])
        blob = json.dumps(inputs)
        user_texts = [self.text_of(item) for item in inputs if item.get("role") == "user"]
        has_result = any(item.get("type") == "function_call_output" for item in inputs)
        if "<transcript>" in blob:
            return "classifier"
        if user_texts and "task-notification" in user_texts[-1]:
            return "main-notified"
        if any(self.CHILD_PROMPT in text for text in user_texts) and not has_result:
            return "child"
        if "FORKY-SKILL-BODY" in blob:
            return "fork-after-tool" if has_result else "fork-first"
        return "main-after-tool" if has_result else "main-first"

    def events_for(self, body):
        kind = self.classify(body)
        with self.lock:
            self.kinds.append(kind)
        if kind == "main-first":
            return function_call_events("Skill", {"skill": "forky"})
        if kind == "fork-first":
            return function_call_events("Agent", {"description": "grandchild bg", "prompt": self.CHILD_PROMPT,
                                                  "subagent_type": "general-purpose", "run_in_background": True})
        if kind == "child":
            self.child_started.set()
            return response_events(text="sub done")
        if kind == "fork-after-tool":
            self.child_started.wait(timeout=30)
            time.sleep(self.fork_delay)
            return response_events(text="fork OK")
        return response_events(text="OK")

    def close(self):
        pass


class PrivatePaseoCopyTests(unittest.TestCase):
    def test_launcher_and_symlinked_server_become_private_regular_files(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-copy-") as temp:
            base = Path(temp)
            root = base / "installed-cli"
            entrypoint = root / "dist" / "index.js"
            entrypoint.parent.mkdir(parents=True)
            (root / "package.json").write_text(json.dumps({"name": "@getpaseo/cli"}))
            entrypoint.write_text("#!/usr/bin/env node\n")
            entrypoint.chmod(0o755)
            server = base / "installed-server"
            reader = server / "dist/server/server/agent/providers/claude/agent.js"
            reader.parent.mkdir(parents=True)
            reader.write_text("original reader\n")
            server_link = root / "node_modules/@getpaseo/server"
            server_link.parent.mkdir(parents=True)
            server_link.symlink_to(server, target_is_directory=True)
            selected = base / "paseo"
            selected.symlink_to(entrypoint)
            destination = base / "private-cli"
            private_entrypoint = copy_paseo_package(selected, destination)
            self.assertEqual(private_entrypoint, destination / "dist/index.js")
            private_reader = destination / "node_modules/@getpaseo/server" / reader.relative_to(server)
            private_reader.write_text("patched private reader\n")
            self.assertEqual(reader.read_text(), "original reader\n")
            self.assertFalse((destination / "node_modules/@getpaseo/server").is_symlink())
            self.assertTrue(os.access(private_entrypoint, os.X_OK))


@unittest.skipUnless(all(os.environ.get(key) for key in
    ("PASEO_TEST_BINARY", "CLAUDE_TEST_BINARY", "CLIPROXYAPI_TEST_BINARY")),
    "Set PASEO_TEST_BINARY, CLAUDE_TEST_BINARY and CLIPROXYAPI_TEST_BINARY")
class PaseoIntegrationTests(unittest.TestCase):
    def test_installed_provider_completes_a_session(self):
        self.run_paseo_session("max")

    def test_ultracode_is_a_direct_model_selection(self):
        self.run_paseo_session("ultracode")

    def test_ultracode_runs_through_native_paseo_thinking_option(self):
        self.run_paseo_session("xhigh", "ultracode")

    def test_context_meter_uses_final_input_and_cached_tokens(self):
        self.run_paseo_session("max", check_usage=True)

    def test_context_meter_updates_between_tools_during_first_running_turn(self):
        self.run_paseo_session("max", check_live_usage=True)

    def test_forked_skill_subagents_finish_live_and_after_daemon_restart(self):
        self.run_paseo_session("high", check_forked_skill=True)

    def test_auto_mode_is_not_offered_and_falls_back_to_prompting(self):
        self.run_paseo_session("high", check_auto_mode=True)

    def run_paseo_session(self, effort, thinking=None, check_usage=False, check_live_usage=False,
                          check_forked_skill=False, check_auto_mode=False):
        with tempfile.TemporaryDirectory(prefix="claude-codex-paseo-") as temp:
            base = Path(temp)
            private_root = base / "private-paseo-cli"
            private_paseo = copy_paseo_package(os.environ["PASEO_TEST_BINARY"], private_root)
            client_module = private_root / "dist" / "utils" / "client.js"
            paseo_home = base / "paseo"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PASEO_", "CLAUDE_", "ANTHROPIC_", "OPENAI_", "CODEX_"))
                   and key != "CLAUDECODE"}
            env["PASEO_HOME"] = str(paseo_home)
            endpoint = f"127.0.0.1:{free_port()}"
            env["PASEO_HOST"] = endpoint
            # No home-level profiles, plugins, credentials or remote selectors.
            home = base / "home"
            home.mkdir()
            env["HOME"] = str(home)
            for name, directory in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                    ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
                env[name] = str(home / directory)
            # The daemon's profile is where Paseo reloads transcripts from; keep
            # the test out of ~/.claude and check that sessions land there.
            daemon_profile = base / "daemon-claude"
            env["CLAUDE_CONFIG_DIR"] = str(daemon_profile)
            env["NO_PROXY"] = "127.0.0.1,localhost"
            env["no_proxy"] = "127.0.0.1,localhost"
            runtime.write_json(paseo_home / "config.json", {
                "version": 1,
                "daemon": {"listen": endpoint, "relay": {"enabled": False},
                           "mcp": {"enabled": False, "injectIntoAgents": False}},
                "agents": {"providers": {name: {"enabled": False} for name in
                           ("claude", "codex", "copilot", "opencode", "pi", "omp")}},
            })
            install_command = [
                "bash", str(ROOT / "install.sh"), "--config-dir", str(base / "config"),
                "--data-dir", str(base / "data"), "--state-dir", str(base / "state"),
                "--bin-dir", str(base / "bin"), "--paseo-home", str(paseo_home),
                "--port", str(free_port()), "--claude-bin", os.environ["CLAUDE_TEST_BINARY"],
                "--paseo-bin", str(private_paseo),
                "--proxy-binary", os.environ["CLIPROXYAPI_TEST_BINARY"],
                "--skip-login", "--skip-paseo-start", "--no-path",
            ]
            for attempt in range(2):
                result = subprocess.run(install_command, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, f"Install {attempt + 1}: {result.stderr}")
            settings = runtime.read_json(base / "config" / "settings.json")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            upstream.requests = queue.Queue()
            if check_usage:
                upstream.usage = {"input_tokens": 120000, "output_tokens": 500, "total_tokens": 120500,
                                  "input_tokens_details": {"cached_tokens": 90000}}
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            config = runtime.proxy_config(settings)
            # The launcher's login presence check uses a dummy file, while the
            # real proxy reads a separate EMPTY auth directory. It can only route
            # using the fake API-key credential and the local upstream below.
            config["auth-dir"] = str(base / "empty-proxy-auth")
            (base / "empty-proxy-auth").mkdir()
            config["request-retry"] = 0
            config["codex-api-key"] = [{"api-key": "fake-test-upstream-key",
                "base-url": f"http://127.0.0.1:{upstream.server_port}",
                "models": [{"name": runtime.MODEL, "alias": runtime.MODEL},
                           {"name": runtime.MODEL, "alias": runtime.ULTRACODE_MODEL}]}]
            runtime.write_json(base / "config" / "proxy.yaml", config)
            runtime.write_json(base / "config" / "auth" / "test.json",
                               {"type": "codex", "refresh_token": "dummy-presence-check-only"})
            proxy = runtime.Runtime(settings)
            paseo = str(base / "bin" / "paseo-codex")
            try:
                proxy.start()
                restart = subprocess.run([paseo, "daemon", "restart", "--json"], env=env,
                                         capture_output=True, text=True, timeout=90)
                self.assertEqual(restart.returncode, 0, restart.stderr + restart.stdout)
                reload_result = subprocess.run([paseo, "reload", "--json"], env=env,
                                               capture_output=True, text=True, timeout=30)
                self.assertEqual(reload_result.returncode, 0, reload_result.stderr + reload_result.stdout)
                if effort == "ultracode":
                    catalog = subprocess.run(["node", "--input-type=module", "-e",
                        "const {connectToDaemon}=await import(process.argv[1]); const client=await connectToDaemon(); "
                        "try {console.log(JSON.stringify(await client.listProviderModels('claude-codex',{cwd:process.argv[2]})));} "
                        "finally {await client.close();}", client_module.as_uri(), str(base)],
                        env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(catalog.returncode, 0, catalog.stderr)
                    models = json.loads(catalog.stdout)["models"]
                    selected = next(model for model in models if model["id"] == runtime.ULTRACODE_MODEL)
                    self.assertIn("Ultra Code", selected["label"])
                    self.assertEqual(selected["thinkingOptions"][0]["id"], "ultracode")
                if check_live_usage:
                    self.run_live_context_session(paseo, client_module, env, base, daemon_profile, upstream)
                    return
                if check_forked_skill:
                    self.run_forked_skill_session(paseo, client_module, env, base, upstream)
                    return
                if check_auto_mode:
                    self.run_auto_mode_session(paseo, client_module, env, base, upstream)
                    return
                result = subprocess.run([
                    paseo, "run", "--provider", "claude-codex", "--model", runtime.model_id(effort),
                    *(["--thinking", thinking] if thinking else []),
                    "--cwd", str(base), "--wait-timeout", "60s", "--json", "Reply with OK.",
                ], env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
                self.assertIn("OK", result.stdout)
                # Claude flushes the transcript shortly after the turn completes.
                deadline = time.monotonic() + 20
                while not list((daemon_profile / "projects").rglob("*.jsonl")) and time.monotonic() < deadline:
                    time.sleep(0.5)
                self.assertTrue(list((daemon_profile / "projects").rglob("*.jsonl")),
                                "Paseo session transcript is missing from the daemon's Claude profile")
                self.assertFalse((base / "config" / "claude" / "projects").exists(),
                                 "Paseo session was recorded in the isolated terminal profile")
                if check_usage:
                    agent_id = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
                    report = subprocess.run(["node", "--input-type=module", "-e",
                        "const {connectToDaemon}=await import(process.argv[1]); const client=await connectToDaemon(); "
                        "try {const result=await client.fetchAgent({agentId:process.argv[2]}); "
                        "console.log(JSON.stringify(result.agent.lastUsage));} finally {await client.close();}",
                        client_module.as_uri(), agent_id], env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(report.returncode, 0, report.stderr)
                    usage = json.loads(report.stdout)
                    self.assertEqual(usage.get("contextWindowMaxTokens"), 1050000, usage)
                    self.assertEqual(usage.get("contextWindowUsedTokens"), 120500, usage)
                    upstream.usage = {"input_tokens": 180000, "output_tokens": 650, "total_tokens": 180650,
                                      "input_tokens_details": {"cached_tokens": 150000}}
                    sent = subprocess.run([paseo, "send", agent_id, "Reply with OK again.", "--json"],
                                          env=env, capture_output=True, text=True, timeout=90)
                    self.assertEqual(sent.returncode, 0, sent.stderr + sent.stdout)
                    report = subprocess.run(["node", "--input-type=module", "-e",
                        "const {connectToDaemon}=await import(process.argv[1]); const client=await connectToDaemon(); "
                        "try {const result=await client.fetchAgent({agentId:process.argv[2]}); "
                        "console.log(JSON.stringify(result.agent.lastUsage));} finally {await client.close();}",
                        client_module.as_uri(), agent_id], env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(report.returncode, 0, report.stderr)
                    usage = json.loads(report.stdout)
                    self.assertEqual(usage.get("contextWindowMaxTokens"), 1050000, usage)
                    self.assertEqual(usage.get("contextWindowUsedTokens"), 180650, usage)
                requests = []
                while not upstream.requests.empty():
                    requests.append(upstream.requests.get_nowait()[1])
                self.assertTrue(requests, result.stdout)
                self.assertTrue(all(req["model"] == runtime.MODEL for req in requests))
                self.assertTrue(any(req.get("reasoning", {}).get("effort") == ("xhigh" if effort == "ultracode" else effort) for req in requests))
                if thinking or effort == "ultracode":
                    tool_names = {tool.get("name", tool.get("type", ""))
                                  for req in requests for tool in req.get("tools", [])}
                    self.assertIn("Workflow", tool_names)
                    self.assertTrue(all(req.get("reasoning", {}).get("effort") == "xhigh" for req in requests))
            finally:
                if getattr(upstream, "sequence", None) is not None:
                    upstream.sequence.close()
                try:
                    subprocess.run([paseo, "daemon", "stop", "--timeout", "10"], env=env,
                                   capture_output=True, text=True, timeout=30)
                finally:
                    try:
                        proxy.stop()
                    finally:
                        upstream.shutdown()
                        upstream.server_close()

    def fetch_agent(self, client_module, env, agent_id, with_timeline=False):
        report = subprocess.run([
            "node", "--input-type=module", "-e",
            "const {connectToDaemon}=await import(process.argv[1]); "
            "const client=await connectToDaemon({host:process.env.PASEO_HOST}); "
            "try {const result=await client.fetchAgent({agentId:process.argv[2]}); "
            "if (!result) throw new Error('Temporary agent was not found'); "
            "const agent=result.agent; let timeline; "
            "if (process.argv[3]) {const {fetchAgentTimelineItems}=await import(process.argv[3]); "
            "timeline=await fetchAgentTimelineItems(client,agent.id);} "
            "const subagents=(await client.listProviderSubagents(agent.id)).subagents; "
            "console.log(JSON.stringify({status:agent.status,lastUsage:agent.lastUsage??null,timeline,"
            "currentModeId:agent.currentModeId??null,runtimeModeId:agent.runtimeInfo?.modeId??null,"
            "availableModes:(agent.availableModes??[]).map(mode=>mode.id),subagents})); "
            "} finally {await client.close();}",
            client_module.as_uri(), agent_id,
            *([(client_module.parent.parent / "commands/agent/logs.js").as_uri()] if with_timeline else []),
        ], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(report.returncode, 0, report.stderr)
        return json.loads(report.stdout)

    def run_paseo(self, paseo, env, *args, timeout=180):
        result = subprocess.run([paseo, *args], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
        return result

    def run_forked_skill_session(self, paseo, client_module, env, base, upstream):
        project = base / "project"
        project.mkdir()
        scenario = ForkedSkillScenario(project)
        upstream.sequence = scenario
        result = self.run_paseo(paseo, env, "run", "--provider", "claude-codex", "--model", runtime.model_id("high"),
                                "--mode", "bypassPermissions", "--cwd", str(project), "--wait-timeout", "120s",
                                "--json", "Do the thing.")
        agent_id = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
        self.assertEqual(scenario.kinds[:4], ["main-first", "fork-first", "child", "fork-after-tool"], scenario.kinds)

        def check(snapshot, phase):
            rows = {row["title"]: row for row in snapshot["subagents"]}
            self.assertEqual(sorted(rows), ["forky", "general-purpose"], f"{phase}: {snapshot['subagents']}")
            fork, child = rows["forky"], rows["general-purpose"]
            self.assertEqual(fork["status"], "completed", f"{phase}: the forked skill never finished: {fork}")
            self.assertEqual(child["status"], "completed", f"{phase}: {child}")
            self.assertEqual(child["parentSubagentId"], fork["id"], f"{phase}: the grandchild must nest under the fork")
            self.assertEqual(child["description"], "grandchild bg")
            self.assertTrue(fork["subtitle"].startswith("forky · gpt-6-astra"), fork["subtitle"])
            tool_calls = [item for item in snapshot["timeline"] if item.get("type") == "tool_call"]
            self.assertEqual([(item["name"], item["status"]) for item in tool_calls], [("Skill", "completed")],
                             f"{phase}: no dangling Task card may remain in the parent transcript")

        deadline = time.monotonic() + 30
        while True:
            snapshot = self.fetch_agent(client_module, env, agent_id, with_timeline=True)
            statuses = {row["status"] for row in snapshot["subagents"]}
            if (snapshot["status"] == "idle" and statuses == {"completed"}) or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        self.assertEqual(snapshot["status"], "idle", snapshot)
        check(snapshot, "live")
        # A fresh daemon rebuilds the same rows from the transcripts on disk.
        self.run_paseo(paseo, env, "daemon", "restart", "--json", timeout=90)
        restored = self.fetch_agent(client_module, env, agent_id, with_timeline=True)
        check(restored, "after restart")

    def run_auto_mode_session(self, paseo, client_module, env, base, upstream):
        project = base / "project"
        project.mkdir()
        # Without a mode, the provider's default applies. The upstream only ever
        # answers with text, so a classifier request would be a Bash-free anomaly.
        result = self.run_paseo(paseo, env, "run", "--provider", "claude-codex", "--model", runtime.model_id("high"),
                                "--cwd", str(project), "--wait-timeout", "60s", "--json", "Reply with OK.")
        default_agent = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
        snapshot = self.fetch_agent(client_module, env, default_agent)
        self.assertEqual(snapshot["currentModeId"], "default", snapshot)
        self.assertEqual(snapshot["runtimeModeId"], "default", snapshot)
        self.assertNotIn("auto", snapshot["availableModes"], snapshot)
        self.assertIn("bypassPermissions", snapshot["availableModes"], snapshot)
        # An agent that still asks for auto mode, such as one created from an
        # older profile, is run by Claude in its default prompting mode instead.
        result = self.run_paseo(paseo, env, "run", "--provider", "claude-codex", "--model", runtime.model_id("high"),
                                "--mode", "auto", "--cwd", str(project), "--wait-timeout", "60s", "--json", "Reply with OK.")
        auto_agent = re.search(r'"agentId"\s*:\s*"([^"]+)"', result.stdout).group(1)
        snapshot = self.fetch_agent(client_module, env, auto_agent)
        self.assertEqual(snapshot["runtimeModeId"], "default", snapshot)
        self.assertNotIn("auto", snapshot["availableModes"], snapshot)
        requests = []
        while not upstream.requests.empty():
            requests.append(upstream.requests.get_nowait()[1])
        self.assertTrue(requests)
        self.assertFalse([req for req in requests if "<transcript>" in json.dumps(req.get("input", []))],
                         "No auto-mode classifier request may reach the gateway")

    def assert_live_usage(self, client_module, env, agent_id, used, gate):
        deadline = time.monotonic() + 15
        while True:
            snapshot = self.fetch_agent(client_module, env, agent_id)
            self.assertEqual(snapshot["status"], "running", snapshot)
            usage = snapshot["lastUsage"] or {}
            actual = (usage.get("contextWindowUsedTokens"), usage.get("contextWindowMaxTokens"))
            if actual == (used, 1050000) or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.assertEqual(actual, (used, 1050000),
                         f"First unfinished turn at request {gate}: status={snapshot['status']}; "
                         f"agent.lastUsage={snapshot['lastUsage']}; expected cache-inclusive current-request "
                         f"usage {used}/1050000 before any final result")

    def run_live_context_session(self, paseo, client_module, env, base, daemon_profile, upstream):
        sequence = GatedReadSequence(base)
        upstream.sequence = sequence
        try:
            result = subprocess.run([
                paseo, "run", "--background", "--provider", "claude-codex",
                "--model", runtime.model_id("max"), "--cwd", str(base), "--json",
                "Read the two live-read files in order, then reply with OK.",
            ], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            agent_id = json.loads(result.stdout)["agentId"]
            # The next upstream request is proof that the preceding Read really
            # executed. Its response cannot emit message_start/result until we
            # release the gate, so these are first-turn, not post-turn, samples.
            for gate, used in ((1, 120500), (2, 180650)):
                self.assertTrue(sequence.arrived[gate].wait(timeout=45),
                                f"Request {gate + 1} never reached its gate; "
                                f"agent={self.fetch_agent(client_module, env, agent_id)}")
                self.assertFalse(sequence.release[gate].is_set())
                self.assertEqual(len(sequence.requests), gate + 1)
                outputs = [item for item in sequence.requests[gate].get("input", [])
                           if item.get("type") == "function_call_output"]
                matching = [item for item in outputs if item.get("call_id") == sequence.call_ids[gate - 1]]
                self.assertTrue(matching, f"Read {gate} has no matching real tool result: {outputs}")
                self.assertIn(sequence.markers[gate - 1], json.dumps(matching))
                self.assert_live_usage(client_module, env, agent_id, used, gate + 1)
                # 180650 must replace 120500, never accumulate across requests.
                self.assertFalse(sequence.release[gate].is_set())
                sequence.release[gate].set()

            deadline = time.monotonic() + 30
            while True:
                snapshot = self.fetch_agent(client_module, env, agent_id)
                usage = snapshot["lastUsage"] or {}
                if snapshot["status"] != "running" or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            self.assertEqual(snapshot["status"], "idle", snapshot)
            self.assertEqual(usage.get("contextWindowUsedTokens"), 210800, snapshot)
            self.assertEqual(usage.get("contextWindowMaxTokens"), 1050000, snapshot)
            self.assertEqual(len(sequence.requests), 3, "The first turn should use exactly Read → Read → text")
            self.assertTrue(sequence.errors.empty(), list(sequence.errors.queue))

            deadline = time.monotonic() + 20
            while True:
                transcripts = list((daemon_profile / "projects").rglob("*.jsonl"))
                text = "\n".join(path.read_text() for path in transcripts)
                if all(marker in text for marker in (*sequence.markers, sequence.final_text)):
                    break
                if time.monotonic() >= deadline:
                    self.fail("The daemon-profile transcript did not preserve both Read results and final text")
                time.sleep(0.1)
            self.assertFalse((base / "config/claude/projects").exists(),
                             "Paseo transcript leaked into the terminal-only profile")
            # A fresh daemon must reload the actual transcript, not merely keep
            # an in-memory timeline that happened to render during execution.
            restarted = subprocess.run([paseo, "daemon", "restart", "--json"], env=env,
                                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
            self.assertEqual(restarted.returncode, 0, restarted.stderr + restarted.stdout)
            restored = self.fetch_agent(client_module, env, agent_id, with_timeline=True)
            timeline = json.dumps(restored["timeline"])
            for marker in (*sequence.markers, sequence.final_text):
                self.assertIn(marker, timeline, "Transcript did not survive the private daemon restart")
        finally:
            sequence.close()


if __name__ == "__main__":
    unittest.main()
