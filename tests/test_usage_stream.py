"""Regression coverage for late Codex usage and Paseo's context meter."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from claude_codex import PaseoUsageStream, is_stream_json


class UsageStreamTests(unittest.TestCase):
    def normalize(self, adapter, message):
        return json.loads(adapter.normalize(json.dumps(message).encode() + b"\n"))

    def event(self, kind, **fields):
        return {"type": "stream_event", "parent_tool_use_id": None,
                "event": {"type": kind, **fields}}

    def test_uses_final_request_with_caches_instead_of_estimate_or_cumulative_total(self):
        adapter = PaseoUsageStream()
        start = self.normalize(adapter, self.event("message_start", message={
            "id": "response-1", "usage": {"input_tokens": 17000, "output_tokens": 0}, "content": []}))
        self.assertNotIn("usage", start["event"]["message"])
        self.assertEqual(start["event"]["message"]["id"], "response-1")
        measured = {"input_tokens": 20000, "cache_read_input_tokens": 90000,
                    "cache_creation_input_tokens": 10000, "output_tokens": 500}
        delta = self.event("message_delta", usage=measured)
        self.assertEqual(self.normalize(adapter, delta), delta)
        result = self.normalize(adapter, {"type": "result", "usage": {
            "input_tokens": 999999, "output_tokens": 7777, "iterations": []}})
        self.assertEqual(result["usage"]["input_tokens"], 999999)
        self.assertEqual(result["usage"]["iterations"], [measured])
        self.assertEqual(sum(result["usage"]["iterations"][-1].values()), 120500)

    def test_last_tool_iteration_wins_and_subagent_usage_is_excluded(self):
        adapter = PaseoUsageStream()
        self.normalize(adapter, self.event("message_start", message={"usage": {"input_tokens": 10}}))
        self.normalize(adapter, self.event("message_delta", usage={"input_tokens": 40000, "output_tokens": 50}))
        self.normalize(adapter, self.event("message_start", message={"usage": {"input_tokens": 20}}))
        self.normalize(adapter, self.event("message_delta", usage={"input_tokens": 42000, "output_tokens": 75}))
        child = self.event("message_delta", usage={"input_tokens": 900000, "output_tokens": 500})
        child["parent_tool_use_id"] = "tool-child"
        self.assertEqual(self.normalize(adapter, child), child)
        self.normalize(adapter, {"type": "result", "parent_tool_use_id": "tool-child", "usage": {"input_tokens": 900000}})
        result = self.normalize(adapter, {"type": "result", "usage": {"input_tokens": 1000000}})
        self.assertEqual(sum(result["usage"]["iterations"][-1].values()), 42075)
        second = self.normalize(adapter, {"type": "result", "usage": {"input_tokens": 3}})
        self.assertNotIn("iterations", second["usage"])

    def test_compaction_and_unknown_usage_do_not_reuse_stale_counts(self):
        adapter = PaseoUsageStream()
        self.normalize(adapter, self.event("message_delta", usage={"input_tokens": 400000, "output_tokens": 5}))
        self.normalize(adapter, {"type": "system", "subtype": "compact_boundary"})
        result = self.normalize(adapter, {"type": "result", "usage": {"input_tokens": 400000}})
        self.assertNotIn("iterations", result["usage"])
        for usage in ({"output_tokens": 12}, {"input_tokens": -1}, {"input_tokens": True}, {"input_tokens": "100"}):
            self.normalize(adapter, self.event("message_start", message={"usage": {"input_tokens": 2}}))
            self.normalize(adapter, self.event("message_delta", usage=usage))
            result = self.normalize(adapter, {"type": "result", "usage": {"input_tokens": 15}})
            self.assertNotIn("iterations", result["usage"])

    def test_existing_sdk_iterations_and_other_messages_are_preserved(self):
        adapter = PaseoUsageStream()
        self.normalize(adapter, self.event("message_delta", usage={"input_tokens": 100, "output_tokens": 1}))
        result = {"type": "result", "usage": {"iterations": [{"input_tokens": 42, "output_tokens": 2}]}}
        self.assertEqual(self.normalize(adapter, result), result)
        for line in (b'not JSON\n', b'{"type":"assistant","message":{"content":[{"text":"streamed text"}]}}\n', b'[]\n'):
            self.assertEqual(adapter.normalize(line), line)

    def test_stream_detection(self):
        self.assertTrue(is_stream_json(["--output-format", "stream-json", "-p"]))
        self.assertTrue(is_stream_json(["--output-format=stream-json"]))
        self.assertFalse(is_stream_json(["--output-format", "json"]))
        self.assertFalse(is_stream_json(["-p", "--", "--output-format", "stream-json"]))

    def test_runner_preserves_stdin_stderr_exit_code_and_signal_forwarding(self):
        module = str(Path(__file__).resolve().parents[1] / "scripts")
        runner = "import os,sys; sys.path.insert(0,sys.argv[1]); from claude_codex import run_paseo_stream; sys.exit(run_paseo_stream(sys.executable,['-c',sys.argv[2]],dict(os.environ)))"
        child = "import sys; print(sys.stdin.readline().strip(),flush=True); print('diagnostic',file=sys.stderr); sys.exit(7)"
        result = subprocess.run([sys.executable, "-c", runner, module, child],
                                input='{"type":"test"}\n', capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, '{"type":"test"}\n')
        self.assertEqual(result.stderr, "diagnostic\n")
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "terminated"
            child = ("import signal,time; from pathlib import Path; "
                     f"signal.signal(signal.SIGTERM,lambda *a:(Path({str(marker)!r}).write_text('forwarded'),exit(0))); "
                     "print('ready',flush=True); time.sleep(30)")
            process = subprocess.Popen([sys.executable, "-c", runner, module, child], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(marker.read_text(), "forwarded")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
