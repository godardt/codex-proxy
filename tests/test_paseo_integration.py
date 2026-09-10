"""Optional real Paseo daemon → wrapper → Claude → proxy integration test."""

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_proxy_integration import Upstream, free_port

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex as runtime


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

    def run_paseo_session(self, effort, thinking=None, check_usage=False):
        with tempfile.TemporaryDirectory(prefix="claude-codex-paseo-") as temp:
            base = Path(temp)
            paseo_home = base / "paseo"
            env = {key: value for key, value in os.environ.items() if not key.startswith("PASEO_")}
            env["PASEO_HOME"] = str(paseo_home)
            # The daemon's profile is where Paseo reloads transcripts from; keep
            # the test out of ~/.claude and check that sessions land there.
            daemon_profile = base / "daemon-claude"
            env["CLAUDE_CONFIG_DIR"] = str(daemon_profile)
            env["NO_PROXY"] = "127.0.0.1,localhost"
            env["no_proxy"] = "127.0.0.1,localhost"
            runtime.write_json(paseo_home / "config.json", {
                "version": 1,
                "daemon": {"listen": f"127.0.0.1:{free_port()}", "relay": {"enabled": False},
                           "mcp": {"enabled": False, "injectIntoAgents": False}},
                "agents": {"providers": {name: {"enabled": False} for name in
                           ("claude", "codex", "copilot", "opencode", "pi", "omp")}},
            })
            result = subprocess.run([
                "bash", str(ROOT / "install.sh"), "--config-dir", str(base / "config"),
                "--data-dir", str(base / "data"), "--state-dir", str(base / "state"),
                "--bin-dir", str(base / "bin"), "--paseo-home", str(paseo_home),
                "--port", str(free_port()), "--claude-bin", os.environ["CLAUDE_TEST_BINARY"],
                "--paseo-bin", os.environ["PASEO_TEST_BINARY"],
                "--proxy-binary", os.environ["CLIPROXYAPI_TEST_BINARY"],
                "--skip-login", "--skip-paseo-start", "--no-path",
            ], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
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
                    client_module = Path(os.environ["PASEO_TEST_BINARY"]).resolve().parents[1] / "dist" / "utils" / "client.js"
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
                    client_module = Path(os.environ["PASEO_TEST_BINARY"]).resolve().parents[1] / "dist" / "utils" / "client.js"
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
                subprocess.run([paseo, "daemon", "stop", "--timeout", "10"], env=env,
                               capture_output=True, text=True, timeout=30)
                proxy.stop()
                upstream.shutdown()
                upstream.server_close()


if __name__ == "__main__":
    unittest.main()
