"""Real CLIProxyAPI tests against a local fake Codex upstream; no account needed."""

import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_codex as runtime


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def response_events(usage=None):
    content = {"type": "output_text", "text": "OK", "annotations": []}
    item = {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed", "content": [content]}
    response = {"id": "resp_test", "object": "response", "created_at": 1783616400,
                "model": runtime.MODEL, "status": "completed", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    if usage is not None:
        response["usage"] = usage
    return [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.content_part.added", "item_id": "msg_test", "output_index": 0, "content_index": 0, "part": {**content, "text": ""}},
        {"type": "response.output_text.delta", "item_id": "msg_test", "output_index": 0, "content_index": 0, "delta": "OK"},
        {"type": "response.output_text.done", "item_id": "msg_test", "output_index": 0, "content_index": 0, "text": "OK"},
        {"type": "response.content_part.done", "item_id": "msg_test", "output_index": 0, "content_index": 0, "part": content},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


def tool_events(file_path):
    arguments = json.dumps({"file_path": str(file_path)})
    item = {"id": "fc_test", "type": "function_call", "call_id": "call_test",
            "name": "Read", "arguments": arguments}
    response = {"id": "resp_tool", "object": "response", "created_at": 1783616400,
                "model": runtime.MODEL, "status": "completed", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    return [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_test", "output_index": 0, "delta": arguments},
        {"type": "response.function_call_arguments.done", "item_id": "fc_test", "output_index": 0, "arguments": arguments},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.put((self.path, body))
        events = response_events(getattr(self.server, "usage", None))
        fixture = getattr(self.server, "tool_fixture", None)
        if fixture and not any(item.get("type") == "function_call_output" for item in body.get("input", [])):
            events = tool_events(fixture)
        data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(os.environ.get("CLIPROXYAPI_TEST_BINARY"), "Set CLIPROXYAPI_TEST_BINARY to run real proxy tests")
class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claude-codex-integration-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.requests = queue.Queue()
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.settings = {
            "config_dir": str(self.base / "config"), "state_dir": str(self.base / "state"),
            "port": free_port(), "api_key": "local-integration-test-key", "reasoning": "high",
            "proxy_bin": str(Path(os.environ["CLIPROXYAPI_TEST_BINARY"]).resolve()),
        }
        (self.base / "config" / "auth").mkdir(parents=True)
        (self.base / "config" / "claude").mkdir()
        config = runtime.proxy_config(self.settings)
        config["request-retry"] = 0
        # Only a fake API-key credential pointing at loopback is registered.
        # No OAuth credentials are loaded, and no real OpenAI endpoint is used.
        config["codex-api-key"] = [{
            "api-key": "fake-local-upstream-key",
            "base-url": f"http://127.0.0.1:{self.upstream.server_port}",
            "models": [{"name": runtime.MODEL, "alias": runtime.MODEL}],
        }]
        runtime.write_json(self.base / "config" / "proxy.yaml", config)
        self.proxy = runtime.Runtime(self.settings)
        self.addCleanup(self.proxy.stop)
        self.proxy.start()

    def payload(self, effort, stream=False):
        return {"model": runtime.model_id(effort), "max_tokens": 128, "stream": stream,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [{"role": "user", "content": "Say OK."}]}

    def test_all_reasoning_levels_reach_codex_unchanged(self):
        for effort in runtime.EFFORTS:
            with self.subTest(effort=effort):
                response = self.proxy.request("/v1/messages", self.payload(effort), timeout=15)
                self.assertEqual(response["type"], "message")
                self.assertIn("OK", "".join(part.get("text", "") for part in response["content"]))
                path, payload = self.upstream.requests.get(timeout=5)
                self.assertTrue(path.endswith("/responses"), path)
                self.assertEqual(payload["model"], runtime.MODEL)
                self.assertEqual(payload["reasoning"]["effort"], effort)

    def test_streaming_anthropic_protocol(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.settings['port']}/v1/messages",
            data=json.dumps(self.payload("max", stream=True)).encode(),
            headers={"Authorization": f"Bearer {self.settings['api_key']}",
                     "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as res:
            text = res.read().decode()
        self.assertIn("event: message_start", text)
        self.assertIn("event: content_block_delta", text)
        self.assertIn("event: message_stop", text)
        self.assertEqual(self.upstream.requests.get(timeout=5)[1]["reasoning"]["effort"], "max")

    def test_concurrent_start_reuses_process_and_restart_works(self):
        pid = self.proxy.owned_pid()
        self.assertIsNotNone(pid)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.proxy.start(), range(4)))
        self.assertEqual(self.proxy.owned_pid(), pid)
        self.proxy.stop()
        self.assertFalse(self.proxy.healthy())
        self.proxy.start()
        self.assertTrue(self.proxy.healthy())
        self.assertNotEqual(self.proxy.owned_pid(), pid)

    def test_unauthenticated_access_rejected(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.settings['port']}/v1/models")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3)
        self.assertEqual(caught.exception.code, 401)

    def run_claude_stream_json(self, extra_args=()):
        binary = shutil.which(os.environ["CLAUDE_TEST_BINARY"])
        self.assertIsNotNone(binary)
        # A clean cwd/profile and explicit settings sources exclude local plugins.
        args = ["--model", "gpt-6-astra(max)", *extra_args, "-p", "Reply with OK.",
                "--output-format", "stream-json", "--verbose", "--tools", getattr(self, "claude_tools", ""),
                "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if getattr(self, "claude_tools", None):
            args += ["--allowedTools", self.claude_tools]
        forwarded, effort = runtime.parse_launch_args(args, "high")
        env = runtime.claude_env(self.settings, effort)
        result = subprocess.run([binary, *forwarded], cwd=self.base, env=env, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        completions = [event for event in events if event.get("type") == "result"]
        self.assertTrue(completions, result.stdout[-3000:])
        self.assertFalse(completions[-1].get("is_error"), completions[-1])
        self.assertIn("OK", completions[-1].get("result", ""))
        usage = completions[-1].get("modelUsage", {})
        self.assertTrue(usage, "Claude did not report its model context window")
        self.assertEqual({item.get("contextWindow") for item in usage.values()}, {1_050_000})
        requests = []
        while not self.upstream.requests.empty():
            requests.append(self.upstream.requests.get_nowait()[1])
        self.assertTrue(requests)
        self.assertTrue(all(req["model"] == runtime.MODEL for req in requests))
        self.assertTrue(all(req["reasoning"]["effort"] == "max" for req in requests))
        return requests

    @unittest.skipUnless(os.environ.get("CLAUDE_TEST_BINARY"), "Set CLAUDE_TEST_BINARY to test the actual Claude harness")
    def test_real_claude_stream_json(self):
        self.run_claude_stream_json()

    @unittest.skipUnless(os.environ.get("CLAUDE_TEST_BINARY"), "Set CLAUDE_TEST_BINARY to test actual argument forwarding")
    def test_real_claude_preserves_option_like_system_prompt(self):
        for literal in ("--model=gpt-6-astra(max)", "--reasoning=low", "--effort=low", "--"):
            with self.subTest(literal=literal):
                requests = self.run_claude_stream_json(["--append-system-prompt", literal])
                for request in requests:
                    system = [item for item in request.get("input", [])
                              if item.get("role") in ("system", "developer")]
                    text = json.dumps(system) + request.get("instructions", "")
                    self.assertIn(literal, text)

    @unittest.skipUnless(os.environ.get("CLAUDE_TEST_BINARY"), "Set CLAUDE_TEST_BINARY to test actual tool execution")
    def test_real_claude_tool_round_trip(self):
        fixture = self.base / "fixture.txt"
        fixture.write_text("local-file-round-trip-succeeded")
        self.upstream.tool_fixture = fixture
        self.claude_tools = "Read"
        requests = self.run_claude_stream_json()
        results = [item for req in requests for item in req.get("input", [])
                   if item.get("type") == "function_call_output"]
        self.assertTrue(results, "Claude did not return a tool result")
        self.assertIn("local-file-round-trip-succeeded", json.dumps(results))


if __name__ == "__main__":
    unittest.main()
