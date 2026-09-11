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
import time
import unittest
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_codex as runtime


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def created_event(response):
    # Codex's measured counts arrive at completion, not at response creation.
    created = {key: value for key, value in response.items() if key != "usage"}
    return {"type": "response.created", "response": {**created, "status": "in_progress", "output": []}}


def response_events(usage=None, text="OK"):
    item_id = f"msg_{uuid.uuid4().hex}"
    content = {"type": "output_text", "text": text, "annotations": []}
    item = {"id": item_id, "type": "message", "role": "assistant", "status": "completed", "content": [content]}
    response = {"id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": 1783616400,
                "model": runtime.MODEL, "status": "completed", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    if usage is not None:
        response["usage"] = usage
    return [
        created_event(response),
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.content_part.added", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {**content, "text": ""}},
        {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text},
        {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": text},
        {"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": content},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


def tool_events(file_path, usage=None, call_id=None):
    item_id = f"fc_{uuid.uuid4().hex}"
    arguments = json.dumps({"file_path": str(file_path)})
    item = {"id": item_id, "type": "function_call", "call_id": call_id or f"call_{uuid.uuid4().hex}",
            "name": "Read", "arguments": arguments}
    response = {"id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": 1783616400,
                "model": runtime.MODEL, "status": "completed", "output": [item],
                "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    return [
        created_event(response),
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": item_id, "output_index": 0, "delta": arguments},
        {"type": "response.function_call_arguments.done", "item_id": item_id, "output_index": 0, "arguments": arguments},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


def function_call_events(name, arguments, usage=None, call_id=None):
    item_id = f"fc_{uuid.uuid4().hex}"
    encoded = json.dumps(arguments)
    item = {"id": item_id, "type": "function_call", "call_id": call_id or f"call_{uuid.uuid4().hex}",
            "name": name, "arguments": encoded}
    response = {"id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": 1783616400,
                "model": runtime.MODEL, "status": "completed", "output": [item],
                "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    return [
        created_event(response),
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": item_id, "output_index": 0, "delta": encoded},
        {"type": "response.function_call_arguments.done", "item_id": item_id, "output_index": 0, "arguments": encoded},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


class GatedReadSequence:
    """Read → Read → text in one turn, with the next requests held before SSE."""

    def __init__(self, base):
        self.markers = ("first-real-read-output", "second-real-read-output")
        self.fixtures = [base / f"live-read-{index}.txt" for index in range(2)]
        self.call_ids = [f"call_{uuid.uuid4().hex}" for _ in self.fixtures]
        for fixture, marker in zip(self.fixtures, self.markers):
            fixture.write_text(marker + "\n")
        self.usages = [
            {"input_tokens": 120000, "output_tokens": 500, "total_tokens": 120500,
             "input_tokens_details": {"cached_tokens": 90000}},
            {"input_tokens": 180000, "output_tokens": 650, "total_tokens": 180650,
             "input_tokens_details": {"cached_tokens": 150000}},
            {"input_tokens": 210000, "output_tokens": 800, "total_tokens": 210800,
             "input_tokens_details": {"cached_tokens": 170000}},
        ]
        self.final_text = "OK: both real Read calls completed."
        self.responses = [
            tool_events(fixture, usage, call_id)
            for fixture, usage, call_id in zip(self.fixtures, self.usages, self.call_ids)
        ] + [response_events(self.usages[-1], self.final_text)]
        self.requests = []
        self.lock = threading.Lock()
        self.arrived = [threading.Event() for _ in self.responses]
        self.release = [threading.Event() for _ in self.responses]
        self.release[0].set()
        self.cancelled = threading.Event()
        self.errors = queue.Queue()

    def events_for(self, body):
        with self.lock:
            index = len(self.requests)
            self.requests.append(body)
        if index >= len(self.responses):
            self.errors.put(f"Unexpected upstream request {index + 1}")
            return None
        self.arrived[index].set()
        if not self.release[index].wait(timeout=90):
            self.errors.put(f"Timed out waiting to release request {index + 1}")
            return None
        if self.cancelled.is_set():
            return None
        return self.responses[index]

    def close(self):
        self.cancelled.set()
        for event in self.release:
            event.set()


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.put((self.path, body))
        sequence = getattr(self.server, "sequence", None)
        if sequence is not None:
            events = sequence.events_for(body)
            if events is None:
                self.send_error(503, "Test sequence stopped")
                return
        elif "<transcript>" in json.dumps(body.get("input", [])):
            # Claude's auto-mode classifier prompt; a verdict shape is not needed to
            # observe that the classifier ran through this gateway.
            events = response_events(text="<block>false</block>")
        else:
            events = response_events(getattr(self.server, "usage", None))
            fixture = getattr(self.server, "tool_fixture", None)
            call = getattr(self.server, "tool_call", None)
            if not any(item.get("type") == "function_call_output" for item in body.get("input", [])):
                if fixture:
                    events = tool_events(fixture)
                elif call:
                    events = function_call_events(*call)
        data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class UpstreamFixtureTests(unittest.TestCase):
    def test_usage_is_late_and_response_and_tool_ids_are_unique(self):
        usage = {"input_tokens": 120000, "output_tokens": 500, "total_tokens": 120500,
                 "input_tokens_details": {"cached_tokens": 90000}}
        streams = [response_events(usage), response_events(usage),
                   tool_events("/unused/first.txt", usage), tool_events("/unused/second.txt", usage)]
        response_ids, item_ids, call_ids = [], [], []
        for events in streams:
            self.assertNotIn("usage", events[0]["response"])
            self.assertFalse(any("usage" in event.get("response", {}) for event in events[:-1]))
            response = events[-1]["response"]
            self.assertEqual(response["usage"], usage)
            self.assertEqual(events[0]["response"]["id"], response["id"])
            response_ids.append(response["id"])
            item = response["output"][0]
            item_ids.append(item["id"])
            if "call_id" in item:
                call_ids.append(item["call_id"])
            self.assertTrue(all(event["item_id"] == item["id"] for event in events if "item_id" in event))
        for ids in (response_ids, item_ids, call_ids):
            self.assertEqual(len(ids), len(set(ids)))

    def test_read_sequence_holds_following_request_until_released(self):
        with tempfile.TemporaryDirectory(prefix="claude-codex-gates-") as temp:
            sequence = GatedReadSequence(Path(temp))
            try:
                self.assertEqual(sequence.events_for({}), sequence.responses[0])
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    try:
                        pending = pool.submit(sequence.events_for, {"input": []})
                        self.assertTrue(sequence.arrived[1].wait(timeout=5))
                        self.assertFalse(pending.done(), "Following request escaped its Event gate")
                        sequence.release[1].set()
                        self.assertEqual(pending.result(timeout=5), sequence.responses[1])
                    finally:
                        sequence.close()
                self.assertTrue(all(event.is_set() for event in sequence.release))
            finally:
                sequence.close()


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
        self.upstream.usage = {"input_tokens": 120000, "output_tokens": 500, "total_tokens": 120500,
                               "input_tokens_details": {"cached_tokens": 90000}}
        req = urllib.request.Request(f"http://127.0.0.1:{self.settings['port']}/v1/messages",
            data=json.dumps(self.payload("max", stream=True)).encode(),
            headers={"Authorization": f"Bearer {self.settings['api_key']}",
                     "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as res:
            text = res.read().decode()
        self.assertIn("event: message_start", text)
        self.assertIn("event: content_block_delta", text)
        self.assertIn("event: message_stop", text)
        events = [json.loads(line.removeprefix("data: ")) for line in text.splitlines() if line.startswith("data: {")]
        start = next(event for event in events if event["type"] == "message_start")
        # The proxy may synthesize a small provisional estimate at message_start;
        # the measured input/cache pair must arrive only in message_delta.
        start_usage = start["message"].get("usage", {})
        self.assertNotEqual((start_usage.get("input_tokens"), start_usage.get("cache_read_input_tokens", 0)),
                            (30000, 90000), start)
        delta = next(event for event in events if event["type"] == "message_delta")
        self.assertEqual(delta["usage"]["input_tokens"], 30000, delta)
        self.assertEqual(delta["usage"]["cache_read_input_tokens"], 90000, delta)
        self.assertEqual(delta["usage"]["output_tokens"], 500, delta)
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

    def run_claude_stream_json(self, extra_args=(), allow_tools=True, launcher_settings=True):
        binary = shutil.which(os.environ["CLAUDE_TEST_BINARY"])
        self.assertIsNotNone(binary)
        # A clean cwd/profile and explicit settings sources exclude local plugins.
        args = ["--model", "gpt-6-astra(max)", *extra_args, "-p", "Reply with OK.",
                "--output-format", "stream-json", "--verbose", "--tools", getattr(self, "claude_tools", ""),
                "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if getattr(self, "claude_tools", None) and allow_tools:
            args += ["--allowedTools", self.claude_tools]
        forwarded, effort = runtime.parse_launch_args(args, "high")
        if launcher_settings:
            forwarded = runtime.apply_launcher_settings(forwarded)
        source = {key: value for key, value in os.environ.items()
                  if not key.startswith("PASEO_") and key != "CLAUDE_CODEX_PASEO_USAGE"}
        # These direct-Claude tests must not inherit a parent Paseo launcher's
        # daemon-profile mode; claude_env must select this fixture's profile.
        env = runtime.claude_env(self.settings, effort, source)
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
        self.claude_events = events
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

    def stream_claude_session(self, extra_args, launcher_settings, prompt="Do the thing."):
        """Drive Claude the way Paseo's SDK does: stream-json in and out, prompts over stdio."""
        binary = shutil.which(os.environ["CLAUDE_TEST_BINARY"])
        self.assertIsNotNone(binary)
        args = ["--model", "gpt-6-astra(max)", "--output-format", "stream-json", "--verbose",
                "--input-format", "stream-json", "--permission-prompt-tool", "stdio", *extra_args,
                "--tools", "Bash", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        forwarded, effort = runtime.parse_launch_args(args, "high")
        if launcher_settings:
            forwarded = runtime.apply_launcher_settings(forwarded)
        source = {key: value for key, value in os.environ.items()
                  if not key.startswith("PASEO_") and key != "CLAUDE_CODEX_PASEO_USAGE"}
        env = runtime.claude_env(self.settings, effort, source)
        child = subprocess.Popen([binary, *forwarded], cwd=self.base, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        events, prompts = [], []
        try:
            message = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
                       "parent_tool_use_id": None}
            child.stdin.write((json.dumps(message) + "\n").encode())
            child.stdin.flush()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                line = child.stdout.readline()
                if not line:
                    break
                event = json.loads(line)
                events.append(event)
                if event.get("type") == "control_request":
                    request = event.get("request", {})
                    if request.get("subtype") == "can_use_tool":
                        prompts.append(request)
                        answer = {"behavior": "deny", "message": "Denied by the test harness"}
                    else:
                        answer = {}
                    response = {"type": "control_response", "response": {
                        "subtype": "success", "request_id": event.get("request_id"), "response": answer}}
                    child.stdin.write((json.dumps(response) + "\n").encode())
                    child.stdin.flush()
                if event.get("type") == "result":
                    break
            child.stdin.close()
            child.wait(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            stderr = child.stderr.read().decode(errors="replace")
            child.stdout.close()
            child.stderr.close()
        results = [event for event in events if event.get("type") == "result"]
        self.assertTrue(results, stderr[-3000:])
        self.assertFalse(results[-1].get("is_error"), results[-1])
        requests = []
        while not self.upstream.requests.empty():
            requests.append(self.upstream.requests.get_nowait()[1])
        return events, prompts, requests

    @unittest.skipUnless(os.environ.get("CLAUDE_TEST_BINARY"), "Set CLAUDE_TEST_BINARY to test the auto-mode classifier path")
    def test_real_claude_auto_mode_is_replaced_by_permission_prompts(self):
        # A write outside the working directory is not on auto mode's fast
        # paths, so it is exactly the kind of call that reaches the classifier.
        outside = tempfile.TemporaryDirectory(prefix="claude-codex-auto-mode-")
        self.addCleanup(outside.cleanup)
        marker = Path(outside.name) / "marker.txt"
        self.upstream.tool_call = ("Bash", {
            "command": f"mkdir -p {marker.parent} && printf classifier-probe > {marker} && cat {marker}",
            "description": "Write a marker file"})
        # Without the launcher setting, Claude's auto mode runs its safety
        # classifier prompt on the session model through this gateway for every
        # tool call, and denies on its own when it cannot read a verdict.
        events, prompts, requests = self.stream_claude_session(["--permission-mode", "auto"], launcher_settings=False)
        init = next(event for event in events if event.get("type") == "system" and event.get("subtype") == "init")
        self.assertEqual(init.get("permissionMode"), "auto")
        classifier = [req for req in requests if "<transcript>" in json.dumps(req.get("input", []))]
        self.assertTrue(classifier, "Expected the auto-mode classifier to reach the gateway")
        self.assertTrue(all(req["model"] == runtime.MODEL and req["reasoning"]["effort"] == "max" for req in classifier))
        self.assertEqual(prompts, [], "Auto mode decides without a permission prompt")
        # With it, Claude starts in its default prompting mode: no classifier
        # request is made and the Bash call is offered to the approval surface.
        events, prompts, requests = self.stream_claude_session(["--permission-mode", "auto"], launcher_settings=True)
        init = next(event for event in events if event.get("type") == "system" and event.get("subtype") == "init")
        self.assertEqual(init.get("permissionMode"), "default")
        self.assertFalse([req for req in requests if "<transcript>" in json.dumps(req.get("input", []))])
        self.assertEqual([prompt.get("tool_name") for prompt in prompts], ["Bash"])
        results = [item for req in requests for item in req.get("input", [])
                   if item.get("type") == "function_call_output"]
        self.assertTrue(results, "Claude did not return a tool result for the Bash call")
        self.assertIn("Denied by the test harness", json.dumps(results))
        self.assertFalse(marker.exists(), "A denied prompt must not run the command")

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
