"""Offline coverage for the installer-managed Paseo usage compatibility patch.

Only checked-in JavaScript and temporary package trees are read or executed.
No installed Paseo package, daemon, upstream service, or credentials are needed.
"""

import json
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_codex
import paseo_compat

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "paseo_claude_usage.js"
AGENT_SOURCE = Path("dist/server/server/agent/providers/claude/agent.js")
NODE = shutil.which("node")
INITIALIZER = (
    "new ClaudeContextUsageState(findClaudeModel(this.config.model)?.contextWindowMaxTokens)"
)
PATCHED_INITIALIZER = (
    "new ClaudeContextUsageState(findClaudeModel(this.config.model)?.contextWindowMaxTokens, "
    "{ ...this.runtimeSettings?.env, ...this.launchEnv })"
)


class PatchSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = FIXTURE.read_text()

    def test_patch_is_narrow_and_idempotent(self):
        transformed = paseo_compat.patch_source(self.source)
        self.assertNotEqual(transformed, self.source)
        self.assertEqual(paseo_compat.patch_source(transformed), transformed)
        self.assertEqual(transformed.count("class ClaudeCodexBaseContextUsageState {"), 1)
        self.assertEqual(transformed.count(
            "class ClaudeContextUsageState extends ClaudeCodexBaseContextUsageState {"), 1)
        self.assertEqual(transformed.count(PATCHED_INITIALIZER), 1)
        self.assertNotIn(INITIALIZER + ";", transformed)
        native = self.source.split("class ClaudeContextUsageState {", 1)[1].split(
            "class ClaudeAgentSession {", 1)[0]
        self.assertIn("class ClaudeCodexBaseContextUsageState {" + native, transformed)
        self.assertTrue(transformed.endswith(self.source.split("class ClaudeAgentSession {", 1)[1]
                                           .replace(INITIALIZER, PATCHED_INITIALIZER)))

    def test_unknown_missing_or_duplicate_anchors_fail_closed(self):
        anchors = (
            "class ClaudeContextUsageState {",
            "class ClaudeAgentSession {",
            "function toObjectRecord(value) {",
            "constructor(initialContextWindowMaxTokens) {",
            "setInitialContextWindowMaxTokens(contextWindowMaxTokens) {",
            "recordModelUsage(modelUsage) {",
            "buildStreamUsageEvent(event) {",
            "createUsageUpdatedEvent(contextWindowUsedTokens) {",
            "buildCompactionUsageEvent(postTokens) {",
            "this.contextUsage.setInitialContextWindowMaxTokens(findClaudeModel(this.config.model)?.contextWindowMaxTokens);",
            "this.streamUsedTokens() ?? activeResultUsageTokens ?? this.compactedContextWindowUsedTokens",
            "this.streamRequestInputTokens = inputTokens;",
            "this.streamRequestOutputTokens = outputTokens;",
            INITIALIZER,
        )
        for anchor in anchors:
            self.assertEqual(self.source.count(anchor), 1, anchor)
            for candidate in (self.source.replace(anchor, "unsupported_anchor", 1),
                              self.source + "\n" + anchor + "\n"):
                with self.subTest(anchor=anchor, duplicate=candidate.startswith(self.source)):
                    with self.assertRaises(claude_codex.SetupError):
                        paseo_compat.patch_source(candidate)
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.patch_source("export const unrelated = true;\n")

    def test_provider_environment_must_be_available_before_usage_construction(self):
        for assignment in ("this.runtimeSettings = options.runtimeSettings;",
                           "this.launchEnv = options.launchEnv;"):
            for candidate in (self.source.replace(assignment, "", 1),
                              self.source.replace(assignment, "", 1).replace(
                                  INITIALIZER + ";", INITIALIZER + ";\n        " + assignment, 1)):
                with self.subTest(assignment=assignment):
                    with self.assertRaises(claude_codex.SetupError):
                        paseo_compat.patch_source(candidate)

    def test_partial_or_modified_patch_is_not_treated_as_already_installed(self):
        transformed = paseo_compat.patch_source(self.source)
        candidates = (
            self.source.replace("class ClaudeContextUsageState {",
                                "class ClaudeCodexBaseContextUsageState {", 1),
            self.source.replace(INITIALIZER, PATCHED_INITIALIZER, 1),
            transformed.replace(PATCHED_INITIALIZER, INITIALIZER, 1),
            transformed.replace("class ClaudeCodexBaseContextUsageState {",
                                "class ChangedBaseContextUsageState {", 1),
            transformed.replace("CLAUDE_CODEX_PASEO_USAGE", "CHANGED_USAGE_MARKER", 1),
            transformed + "\n" + transformed,
        )
        for index, candidate in enumerate(candidates):
            with self.subTest(source=index):
                with self.assertRaises(claude_codex.SetupError):
                    paseo_compat.patch_source(candidate)


@unittest.skipUnless(NODE, "Node is required to execute the transformed fixture in memory")
class UsageStateTests(unittest.TestCase):
    def run_js(self, body, *, transformed=True, process_env=None):
        source = FIXTURE.read_text()
        if transformed:
            source = paseo_compat.patch_source(source)
        helpers = """\
            import assert from 'node:assert/strict';
            const markedEnv = {CLAUDE_CODEX_PASEO_USAGE: '1',
                               CLAUDE_CODE_MAX_CONTEXT_TOKENS: '1050000'};
            const first = {input_tokens: 20000, cache_read_input_tokens: 90000,
                           cache_creation_input_tokens: 10000, output_tokens: 500};
            const second = {input_tokens: 35000, cache_read_input_tokens: 130000,
                            cache_creation_input_tokens: 15000, output_tokens: 650};
            const start = usage => ({type: 'message_start', message: {usage}});
            const delta = usage => ({type: 'message_delta', usage});
            const session = (model = 'custom-model', providerEnv = markedEnv, launchEnv = {}) =>
                new ClaudeAgentSession({model}, {runtimeSettings: {env: providerEnv}, launchEnv});
            function assertUsage(event, used, max = 1050000) {
                const usage = {contextWindowUsedTokens: used};
                if (max !== null) usage.contextWindowMaxTokens = max;
                assert.deepEqual(event, {type: 'usage_updated', provider: 'claude', usage});
            }
        """
        result = subprocess.run(
            [NODE, "--input-type=module"],
            input=source + "\n" + textwrap.dedent(helpers) + textwrap.dedent(body),
            text=True, capture_output=True, timeout=10,
            # Do not inherit preload hooks or provider configuration from the host.
            env={"PATH": os.defpath, **(process_env or {})},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_original_fixture_reproduces_missing_live_usage_before_patch(self):
        self.run_js("""
            const state = session().contextUsage;
            assert.equal(state.buildStreamUsageEvent(start(undefined)), null);
            assert.equal(state.buildStreamUsageEvent(delta(first)), null);
            assert.equal(state.contextWindowMaxTokens, undefined);
            const final = state.buildResultUsage({usage: {iterations: [first]},
                modelUsage: {custom: {contextWindow: 1050000}}});
            assert.equal(final.contextWindowUsedTokens, 120500);
            assert.equal(final.contextWindowMaxTokens, 1050000);
        """, transformed=False)

    def test_late_usage_is_cache_inclusive_and_replaced_between_tool_requests(self):
        self.run_js("""
            const state = session().contextUsage;
            assert.equal(state.contextWindowMaxTokens, 1050000);
            assert.equal(state.streamUsedTokens(), undefined);
            state.beginTurn();
            const provisional = start({input_tokens: 17000, output_tokens: 7,
                cache_read_input_tokens: 80000, cache_creation_input_tokens: 9000});
            const original = structuredClone(provisional);
            assert.equal(state.buildStreamUsageEvent(provisional), null);
            assert.deepEqual(provisional, original);
            assert.equal(state.buildStreamUsageEvent(delta({output_tokens: 25})), null);
            assert.equal(state.streamUsedTokens(), undefined);
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            assert.equal(state.completedResultTurns, 0, 'Must update before a result');
            assert.equal(state.buildStreamUsageEvent(start({input_tokens: 999999})), null);
            assert.equal(state.buildStreamUsageEvent(delta({output_tokens: 650})), null);
            assert.equal(state.streamUsedTokens(), undefined);
            assertUsage(state.buildStreamUsageEvent(delta(second)), 180650);
            assert.equal(state.completedResultTurns, 0, 'Tool calls are not completed turns');
            const final = state.buildResultUsage({usage: {
                input_tokens: 999999, cache_read_input_tokens: 777777, output_tokens: 8888,
                iterations: [first, second]}, total_cost_usd: 1.25,
                modelUsage: {custom: {contextWindow: 200000}}});
            assert.deepEqual(final, {inputTokens: 999999, cachedInputTokens: 777777,
                outputTokens: 8888, totalCostUsd: 1.25,
                contextWindowUsedTokens: 180650, contextWindowMaxTokens: 1050000});
            assert.equal(state.completedResultTurns, 1);
        """)

    def test_delta_output_is_a_snapshot_and_can_refine_only_current_request(self):
        self.run_js("""
            const state = session().contextUsage;
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            assertUsage(state.buildStreamUsageEvent(delta({output_tokens: 700})), 120700);
            assertUsage(state.buildStreamUsageEvent(delta({output_tokens: 650})), 120650);
            assertUsage(state.buildStreamUsageEvent(delta({...first, input_tokens: 10000})), 110500);
            assert.equal(state.buildStreamUsageEvent(start(undefined)), null);
            assert.equal(state.buildStreamUsageEvent(delta({output_tokens: 1000})), null);
            assert.equal(state.streamUsedTokens(), undefined);
            assertUsage(state.buildStreamUsageEvent(delta(second)), 180650);
        """)

    def test_missing_usage_and_input_remain_unknown_until_measured_delta(self):
        self.run_js("""
            const state = session().contextUsage;
            for (const usage of [undefined, null, {}, [], true, '100',
                    {output_tokens: 5}, {cache_read_input_tokens: 90000, output_tokens: 5}]) {
                state.beginTurn();
                assert.equal(state.buildStreamUsageEvent(start(undefined)), null);
                assert.equal(state.buildStreamUsageEvent(delta(usage)), null);
                assert.equal(state.streamUsedTokens(), undefined);
            }
            state.beginTurn();
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            assert.equal(state.buildStreamUsageEvent(delta({input_tokens: 100})), null);
            assert.equal(state.streamUsedTokens(), undefined, 'Incomplete input must not reuse previous output');
            assertUsage(state.buildStreamUsageEvent(delta({output_tokens: 7})), 107);
        """)

    def test_malformed_measured_fields_do_not_publish_or_seed_later_output(self):
        self.run_js("""
            const state = session().contextUsage;
            const invalid = [undefined, null, true, false, '100', -1, NaN,
                             Infinity, -Infinity, {}, []];
            for (const field of Object.keys(first)) {
                for (const value of invalid) {
                    state.beginTurn();
                    assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
                    const event = delta({...first, [field]: value});
                    assert.equal(state.buildStreamUsageEvent(event), null,
                        `${field}=${String(value)} must be rejected`);
                    assert.equal(state.streamUsedTokens(), undefined);
                    assert.equal(state.buildStreamUsageEvent(delta({output_tokens: 750})), null,
                        'Malformed measurements must not expose stale input');
                }
            }
            state.beginTurn();
            assert.equal(state.buildStreamUsageEvent(delta({input_tokens: Number.MAX_VALUE,
                cache_read_input_tokens: Number.MAX_VALUE, output_tokens: 1})), null);
            assert.equal(state.streamUsedTokens(), undefined);
            assert.equal(state.buildStreamUsageEvent(delta({input_tokens: Number.MAX_VALUE,
                output_tokens: Number.MAX_VALUE})), null);
            assert.equal(state.streamUsedTokens(), undefined);
        """)

    def test_invalid_output_only_delta_preserves_last_valid_snapshot(self):
        self.run_js("""
            const state = session().contextUsage;
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            for (const value of [undefined, null, true, false, '7', -1, NaN, Infinity, -Infinity]) {
                assert.equal(state.buildStreamUsageEvent(delta({output_tokens: value})), null);
                assert.equal(state.streamUsedTokens(), 120500);
            }
            assertUsage(state.buildStreamUsageEvent(delta({output_tokens: 750})), 120750);
            for (const cache of [900000, null, true, '900000', -1, NaN, Infinity]) {
                assertUsage(state.buildStreamUsageEvent(delta({output_tokens: 800,
                    cache_read_input_tokens: cache, cache_creation_input_tokens: cache})), 120800);
            }
        """)

    def test_zero_input_cache_only_requests_optional_caches_and_zero_counts(self):
        self.run_js("""
            const state = session().contextUsage;
            for (const [usage, expected] of [
                [{input_tokens: 0, cache_read_input_tokens: 90000,
                  cache_creation_input_tokens: 10000, output_tokens: 500}, 100500],
                [{input_tokens: 0, output_tokens: 7}, 7],
                [{input_tokens: 42, output_tokens: 0}, 42],
                [{input_tokens: 0, cache_creation_input_tokens: 10, output_tokens: 0}, 10],
                [{input_tokens: 1.5, output_tokens: 0.5}, 2],
            ]) {
                state.beginTurn();
                assertUsage(state.buildStreamUsageEvent(delta(usage)), expected);
            }
            state.beginTurn();
            assert.equal(state.buildStreamUsageEvent(delta({input_tokens: 0,
                cache_read_input_tokens: 0, cache_creation_input_tokens: 0, output_tokens: 0})), null);
            // Zero input is known, even though the native class omits a zero-total update.
            assertUsage(state.buildStreamUsageEvent(delta({output_tokens: 1})), 1);
        """)

    def test_marker_requires_exact_configured_environment_string(self):
        self.run_js("""
            for (const marker of [undefined, null, false, true, 0, 1, '', '0', 'true', ' 1', '1 ']) {
                const state = session('native-model', {...markedEnv,
                    CLAUDE_CODEX_PASEO_USAGE: marker}).contextUsage;
                assert.equal(state.contextWindowMaxTokens, 200000);
                assertUsage(state.buildStreamUsageEvent(start(first)), 120000, 200000);
                assertUsage(state.buildStreamUsageEvent(delta(second)), 120650, 200000);
            }
            const missing = new ClaudeAgentSession({model: 'native-model'}, {}).contextUsage;
            assert.equal(missing.contextWindowMaxTokens, 200000);
            assertUsage(missing.buildStreamUsageEvent(start(first)), 120000, 200000);
            const marked = session('native-model').contextUsage;
            assert.equal(marked.buildStreamUsageEvent(start(first)), null);
            assertUsage(marked.buildStreamUsageEvent(delta(first)), 120500);
        """, process_env={"CLAUDE_CODEX_PASEO_USAGE": "1",
                           "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "9999999"})

    def test_provider_environment_is_used_with_per_launch_override_precedence(self):
        self.run_js("""
            const providerEnv = {...markedEnv};
            const launchEnv = {CLAUDE_CODE_MAX_CONTEXT_TOKENS: '1200000'};
            const current = session('native-model', providerEnv, launchEnv);
            assert.equal(current.contextUsage.buildStreamUsageEvent(start(first)), null);
            assertUsage(current.contextUsage.buildStreamUsageEvent(delta(first)), 120500, 1200000);
            await current.setModel('larger-native-model');
            assert.equal(current.contextUsage.contextWindowMaxTokens, 1200000);
            assert.deepEqual(providerEnv, markedEnv, 'Provider configuration must not be mutated');
            assert.deepEqual(launchEnv, {CLAUDE_CODE_MAX_CONTEXT_TOKENS: '1200000'});

            const disabled = session('native-model', markedEnv, {CLAUDE_CODEX_PASEO_USAGE: '0'});
            assertUsage(disabled.contextUsage.buildStreamUsageEvent(start(first)), 120000, 200000);
            await disabled.setModel('larger-native-model');
            assert.equal(disabled.contextUsage.contextWindowMaxTokens, 300000);

            const launchOnly = session('custom-model', {}, markedEnv).contextUsage;
            assert.equal(launchOnly.buildStreamUsageEvent(start(first)), null);
            assertUsage(launchOnly.buildStreamUsageEvent(delta(first)), 120500);

            const invalidOverride = session('native-model', markedEnv,
                {CLAUDE_CODE_MAX_CONTEXT_TOKENS: 'invalid'}).contextUsage;
            assertUsage(invalidOverride.buildStreamUsageEvent(delta(second)), 180650, 200000);

            const inheritedOnly = session('native-model', {}, {}).contextUsage;
            assertUsage(inheritedOnly.buildStreamUsageEvent(start(first)), 120000, 200000);
        """, process_env={"CLAUDE_CODEX_PASEO_USAGE": "1",
                           "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "9999999"})

    def test_unmarked_behavior_matches_original_across_native_state_transitions(self):
        original = json.dumps(FIXTURE.read_text())
        self.run_js("""
            const originalModule = await import('data:text/javascript,' + encodeURIComponent(
        """ + original + """));
            function trace(State) {
                const state = new State(200000, {CLAUDE_CODE_MAX_CONTEXT_TOKENS: '1050000'});
                const events = [];
                events.push(state.beginTurn());
                for (const event of [start(first), delta(second), start(undefined),
                    delta({output_tokens: 750}), null, [], {type: 'content_block_delta'},
                    delta({output_tokens: -1}), start({input_tokens: 0, cache_read_input_tokens: 20})]) {
                    events.push(state.buildStreamUsageEvent(event));
                }
                events.push(state.buildResultUsage({usage: {input_tokens: 999999,
                    cache_read_input_tokens: 7, output_tokens: 8}, total_cost_usd: 2,
                    modelUsage: {one: {contextWindow: 250000}, two: {contextWindow: 400000}}}));
                events.push(state.beginTurn());
                events.push(state.buildStreamUsageEvent(delta(first)));
                events.push(state.buildResultUsage({usage: {iterations: [first, second]}}));
                events.push(state.buildCompactionUsageEvent(777));
                events.push(state.buildResultUsage({usage: {input_tokens: 999999}}));
                events.push(state.beginTurn());
                events.push(state.buildResultUsage({}));
                events.push(state.setInitialContextWindowMaxTokens(undefined));
                events.push(state.recordModelUsage({one: {contextWindow: 300000}}));
                return {events, used: state.streamUsedTokens(), turns: state.completedResultTurns,
                        max: state.contextWindowMaxTokens};
            }
            assert.deepEqual(trace(ClaudeContextUsageState), trace(originalModule.ClaudeContextUsageState));
        """)

    def test_begin_turn_and_compaction_reset_stream_without_losing_capacity(self):
        self.run_js("""
            const state = session().contextUsage;
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            assertUsage(state.buildCompactionUsageEvent(32000), 32000);
            assert.equal(state.streamUsedTokens(), undefined);
            assert.equal(state.buildStreamUsageEvent(delta({output_tokens: 800})), null);
            // Preserve the native result fallback after a compaction boundary.
            state.completedResultTurns = 1;
            assert.equal(state.buildResultUsage({usage: {input_tokens: 999999}})
                .contextWindowUsedTokens, 32000);
            assert.equal(state.compactedContextWindowUsedTokens, undefined);
            assert.equal(state.buildResultUsage({usage: {input_tokens: 999999}})
                .contextWindowUsedTokens, undefined);
            assertUsage(state.buildStreamUsageEvent(delta(second)), 180650);
            state.beginTurn();
            assert.equal(state.streamUsedTokens(), undefined);
            assert.equal(state.buildStreamUsageEvent(delta({output_tokens: 9})), null);
            assert.equal(state.contextWindowMaxTokens, 1050000);
            assertUsage(state.buildStreamUsageEvent(delta(first)), 120500);
            assert.deepEqual(state.buildCompactionUsageEvent(undefined), {
                type: 'usage_updated', provider: 'claude', usage: {contextWindowMaxTokens: 1050000}});
            state.buildCompactionUsageEvent(42);
            state.beginTurn();
            assert.equal(state.compactedContextWindowUsedTokens, undefined);
            assert.equal(state.contextWindowMaxTokens, 1050000);
        """)

    def test_invalid_maximum_falls_back_to_native_model_and_result_metadata(self):
        self.run_js("""
            const invalid = [undefined, null, true, false, 0, 1050000, '', '0', '-1', '+1050000',
                '1.5', '1e6', 'Infinity', 'NaN', ' 1050000', '1050000 ', '1_050_000',
                '9007199254740992', '999999999999999999999999999999999999999999'];
            for (const max of invalid) {
                const state = session('native-model', {...markedEnv,
                    CLAUDE_CODE_MAX_CONTEXT_TOKENS: max}).contextUsage;
                assert.equal(state.contextWindowMaxTokens, 200000, String(max));
                assert.equal(state.buildStreamUsageEvent(start(first)), null);
                assertUsage(state.buildStreamUsageEvent(delta(first)), 120500, 200000);
                assert.equal(state.buildResultUsage({usage: first,
                    modelUsage: {custom: {contextWindow: 300000}}}).contextWindowMaxTokens, 300000);
            }
            const unknown = session('custom-model', {CLAUDE_CODEX_PASEO_USAGE: '1'}).contextUsage;
            assertUsage(unknown.buildStreamUsageEvent(delta(first)), 120500, null);
            assert.equal(unknown.buildResultUsage({usage: first,
                modelUsage: {custom: {contextWindow: 300000}}}).contextWindowMaxTokens, 300000);
        """)

    def test_valid_maximum_survives_set_model_and_final_model_metadata(self):
        self.run_js("""
            for (const configured of ['1', '1050000', '0001050000', '9007199254740991']) {
                const current = session('native-model', {...markedEnv,
                    CLAUDE_CODE_MAX_CONTEXT_TOKENS: configured});
                const state = current.contextUsage;
                const expected = Number(configured);
                for (const model of ['larger-native-model', 'custom-model', '', null, ' native-model ']) {
                    await current.setModel(model);
                    assert.equal(current.contextUsage, state, 'Model changes must not reconstruct usage state');
                    assert.equal(state.contextWindowMaxTokens, expected);
                    assertUsage(state.buildStreamUsageEvent(delta(first)), 120500, expected);
                    const result = state.buildResultUsage({usage: first,
                        modelUsage: {custom: {contextWindow: 200000}}},
                        {another: {contextWindow: 4000000}});
                    assert.equal(result.contextWindowMaxTokens, expected);
                    assert.equal(result.contextWindowUsedTokens, 120500);
                }
            }
            const native = session('native-model', {});
            await native.setModel('larger-native-model');
            assert.equal(native.contextUsage.contextWindowMaxTokens, 300000);
            await native.setModel('custom-model');
            assert.equal(native.contextUsage.contextWindowMaxTokens, undefined);
        """)

    def test_final_iterations_remain_fallback_without_cumulative_turn_leakage(self):
        self.run_js("""
            const state = session().contextUsage;
            const message = {usage: {input_tokens: 999999, output_tokens: 9999,
                iterations: [first, second]}};
            const original = structuredClone(message);
            assert.equal(state.buildResultUsage(message).contextWindowUsedTokens, 180650);
            assert.deepEqual(message, original);
            state.beginTurn();
            const cumulative = state.buildResultUsage({usage: {input_tokens: 999999, output_tokens: 9999}});
            assert.equal(cumulative.contextWindowUsedTokens, undefined);
            assert.equal(cumulative.contextWindowMaxTokens, 1050000);
            state.beginTurn();
            assert.equal(state.buildResultUsage({usage: {iterations: [second, first]}})
                .contextWindowUsedTokens, 120500);
            state.beginTurn();
            assert.equal(state.buildResultUsage({}), undefined);
            assert.equal(state.completedResultTurns, 4);
        """)


@unittest.skipUnless(NODE, "Node is required to syntax-check temporary package fixtures")
class PatchFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paseo-compat-offline-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "space ' quote $literal"
        self.modules = self.base / "prefix/lib/node_modules"
        self.cli = self.modules / "@getpaseo/cli"
        self.manifest(self.cli, "@getpaseo/cli")
        self.executable = self.cli / "dist/bin/paseo.js"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("throw new Error('Discovery must not execute the CLI');\n")
        self.executable.chmod(0o755)
        self.binary = self.base / "prefix/bin/paseo"
        self.binary.parent.mkdir(parents=True)
        self.binary.symlink_to(self.executable)
        self.source = FIXTURE.read_text()
        self.target = self.server(self.cli / "node_modules")
        self.backups = []

    @staticmethod
    def manifest(directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text(json.dumps({"name": name, "type": "module"}))

    def server(self, modules):
        root = modules / "@getpaseo/server"
        self.manifest(root, "@getpaseo/server")
        target = root / AGENT_SOURCE
        target.parent.mkdir(parents=True)
        target.write_text(self.source)
        return target

    def backup(self, path):
        self.assertEqual(path, self.target.resolve())
        # The callback must see the complete upstream file, never a partial edit.
        self.assertEqual(path.read_text(), self.source)
        backup = self.base / f"upstream-backup-{len(self.backups)}.js"
        shutil.copy2(path, backup)
        self.backups.append(backup)
        return backup

    def test_resolves_symlink_cli_and_actual_named_package_ancestor_without_writing(self):
        # An intermediate package boundary is not the CLI package root.
        self.manifest(self.cli / "dist", "unrelated-fixture-package")
        outer_alias = self.base / "selected-paseo"
        outer_alias.symlink_to(self.binary)
        before = self.target.read_bytes(), self.target.stat()
        result = paseo_compat.prepare_patch(str(outer_alias))
        self.assertIsInstance(result, Path)
        self.assertEqual(result, self.target.resolve())
        self.assertEqual(self.target.read_bytes(), before[0])
        self.assertEqual(self.target.stat().st_ino, before[1].st_ino)
        self.assertEqual(self.target.stat().st_mtime_ns, before[1].st_mtime_ns)

    def test_node_style_resolution_prefers_nested_server_over_hoisted(self):
        hoisted = self.server(self.modules)
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())
        self.assertEqual(hoisted.read_text(), self.source)

    def test_node_style_resolution_finds_hoisted_server(self):
        shutil.rmtree(self.cli / "node_modules")
        self.target = self.server(self.modules)
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())

    def test_server_symlink_is_resolved_inside_temporary_package_tree(self):
        server_root = self.cli / "node_modules/@getpaseo/server"
        real_root = self.base / "private-package-store/server"
        real_root.parent.mkdir(parents=True)
        server_root.rename(real_root)
        server_root.symlink_to(real_root, target_is_directory=True)
        self.target = real_root / AGENT_SOURCE
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())

    def test_unsupported_cli_layout_leaves_source_untouched(self):
        for name in ("unrelated-fixture-package", "@getpaseo/server"):
            with self.subTest(name=name):
                self.manifest(self.cli, name)
                with self.assertRaises(claude_codex.SetupError):
                    paseo_compat.prepare_patch(self.binary)
                self.assertEqual(self.target.read_text(), self.source)
        (self.cli / "package.json").write_text("{not-json\n")
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.prepare_patch(self.binary)
        self.assertEqual(self.target.read_text(), self.source)

    def test_missing_executable_or_server_is_actionable(self):
        for executable in (self.base / "missing-paseo", self.base / "dangling-paseo"):
            with self.subTest(executable=executable):
                if executable.name == "dangling-paseo":
                    executable.symlink_to(self.base / "missing-target")
                with self.assertRaises(claude_codex.SetupError):
                    paseo_compat.prepare_patch(executable)
        self.target.unlink()
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.prepare_patch(self.binary)
        self.assertFalse(self.target.exists())

    def test_unsupported_nearest_server_does_not_patch_a_hoisted_decoy(self):
        hoisted = self.server(self.modules)
        bad_source = "export const unknownProviderLayout = true;\n"
        self.target.write_text(bad_source)
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.prepare_patch(self.binary)
        self.assertEqual(self.target.read_text(), bad_source)
        self.assertEqual(hoisted.read_text(), self.source)

    def test_syntax_errors_are_rejected_without_mutation_or_backup(self):
        invalid = self.source + "\nconst invalid = ;\n"
        self.target.write_text(invalid)
        backup = Mock()
        before = self.target.stat()
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.prepare_patch(self.binary)
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.apply_patch(self.target, backup)
        backup.assert_not_called()
        self.assertEqual(self.target.read_text(), invalid)
        self.assertEqual(self.target.stat().st_ino, before.st_ino)
        self.assertEqual(self.target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_syntax_check_does_not_evaluate_valid_module(self):
        self.target.write_text(self.source + "\nthrow new Error('Do not evaluate installed source');\n")
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())

    def test_syntax_validation_does_not_execute_host_node_preload_hooks(self):
        preload = self.base / "unexpected-preload.cjs"
        preload.write_text("throw new Error('Do not execute NODE_OPTIONS preload hooks');\n")
        with patch.dict(os.environ, {"NODE_OPTIONS": "--require " + json.dumps(str(preload))}):
            self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())
        self.assertEqual(self.target.read_text(), self.source)

    def test_missing_node_fails_without_mutation_or_backup(self):
        backup = Mock()
        with patch.object(paseo_compat.shutil, "which", return_value=None):
            with self.assertRaisesRegex(claude_codex.SetupError, "Node"):
                paseo_compat.prepare_patch(self.binary)
            with self.assertRaisesRegex(claude_codex.SetupError, "Node"):
                paseo_compat.apply_patch(self.target, backup)
        backup.assert_not_called()
        self.assertEqual(self.target.read_text(), self.source)

    def test_unwritable_package_directory_is_actionable_without_changes(self):
        original_access = os.access

        def access(path, mode, *args, **kwargs):
            if Path(path) == self.target.parent and mode == os.W_OK:
                return False
            return original_access(path, mode, *args, **kwargs)

        with patch.object(paseo_compat.os, "access", side_effect=access):
            with self.assertRaisesRegex(claude_codex.SetupError, "writable"):
                paseo_compat.prepare_patch(self.binary)
        self.assertEqual(self.target.read_text(), self.source)

    def test_atomic_replacement_preserves_read_only_file_mode(self):
        # File write permission is unnecessary when its directory permits replacement.
        self.target.chmod(0o444)
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())
        paseo_compat.apply_patch(self.target, self.backup)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o444)
        self.assertEqual(self.target.read_text(), paseo_compat.patch_source(self.source))
        self.assertEqual(len(self.backups), 1)
        self.assertEqual(stat.S_IMODE(self.backups[0].stat().st_mode), 0o444)

    def test_apply_backs_up_then_atomically_replaces_and_preserves_mode(self):
        self.target.chmod(0o751)
        target = paseo_compat.prepare_patch(self.binary)
        with target.open("rb") as upstream_handle:
            old_inode = os.fstat(upstream_handle.fileno()).st_ino
            self.assertIsNone(paseo_compat.apply_patch(target, self.backup))
            # An open reader must still see the old inode's complete content.
            self.assertEqual(upstream_handle.read(), self.source.encode())
            self.assertNotEqual(target.stat().st_ino, old_inode)
        self.assertEqual(target.read_text(), paseo_compat.patch_source(self.source))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o751)
        self.assertEqual(len(self.backups), 1)
        self.assertEqual(self.backups[0].read_text(), self.source)
        self.assertEqual(stat.S_IMODE(self.backups[0].stat().st_mode), 0o751)

    def test_repeated_application_is_noop_and_restored_upstream_can_be_reapplied(self):
        paseo_compat.apply_patch(self.target, self.backup)
        patched = self.target.read_bytes()
        before = self.target.stat()
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())
        paseo_compat.apply_patch(self.target, self.backup)
        self.assertEqual(len(self.backups), 1)
        self.assertEqual(self.target.read_bytes(), patched)
        self.assertEqual(self.target.stat().st_ino, before.st_ino)
        self.assertEqual(self.target.stat().st_mtime_ns, before.st_mtime_ns)
        self.target.write_text(self.source)
        paseo_compat.apply_patch(self.target, self.backup)
        self.assertEqual(self.target.read_bytes(), patched)
        self.assertEqual(len(self.backups), 2)
        self.assertTrue(all(backup.read_text() == self.source for backup in self.backups))

    def test_apply_revalidates_source_changed_after_prepare(self):
        target = paseo_compat.prepare_patch(self.binary)
        unsupported = "export const replacedByUpstream = true;\n"
        target.write_text(unsupported)
        backup = Mock()
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.apply_patch(target, backup)
        backup.assert_not_called()
        self.assertEqual(target.read_text(), unsupported)

    def test_apply_revalidates_already_patched_source_changed_after_prepare(self):
        target = paseo_compat.prepare_patch(self.binary)
        transformed = paseo_compat.patch_source(self.source)
        target.write_text(transformed)
        before = target.stat()
        backup = Mock()
        paseo_compat.apply_patch(target, backup)
        backup.assert_not_called()
        self.assertEqual(target.read_text(), transformed)
        self.assertEqual(target.stat().st_ino, before.st_ino)

    def test_already_patched_source_is_syntax_revalidated_after_prepare(self):
        paseo_compat.apply_patch(self.target, self.backup)
        target = paseo_compat.prepare_patch(self.binary)
        invalid = target.read_text() + "\nconst invalidAfterPrepare = ;\n"
        target.write_text(invalid)
        backup = Mock()
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.apply_patch(target, backup)
        backup.assert_not_called()
        self.assertEqual(target.read_text(), invalid)
        self.assertEqual(len(self.backups), 1)

    def test_backup_failure_keeps_original_file_and_mode(self):
        self.target.chmod(0o640)
        before = self.target.stat()
        backup = Mock(side_effect=claude_codex.SetupError("Cannot create backup"))
        with self.assertRaises(claude_codex.SetupError):
            paseo_compat.apply_patch(self.target, backup)
        backup.assert_called_once_with(self.target.resolve())
        self.assertEqual(self.target.read_text(), self.source)
        self.assertEqual(self.target.stat().st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)

    def test_atomic_replace_failure_is_actionable_and_leaves_source_intact(self):
        before = self.target.stat()
        with patch("os.replace", side_effect=PermissionError("Read-only package directory")):
            with self.assertRaises(claude_codex.SetupError):
                paseo_compat.apply_patch(self.target, self.backup)
        self.assertEqual(self.target.read_text(), self.source)
        self.assertEqual(self.target.stat().st_ino, before.st_ino)
        self.assertEqual(len(self.backups), 1)

    def test_concurrent_application_revalidates_under_lock_and_backs_up_once(self):
        worker = textwrap.dedent("""\
            import sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import paseo_compat
            target, backup_log = map(Path, sys.argv[2:4])
            def backup(path):
                with backup_log.open('a') as log:
                    log.write('backup\\n')
                if sys.argv[4] == 'hold':
                    print('backup-held', flush=True)
                    sys.stdin.readline()
            print('applying', flush=True)
            paseo_compat.apply_patch(target, backup)
            print('applied', flush=True)
        """)
        log = self.base / "backup-calls"
        processes = []

        def launch(mode):
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", worker, str(ROOT / "scripts"),
                 str(self.target), str(log), mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # Raw pipes avoid readline buffering a future readiness signal.
                bufsize=0,
            )
            processes.append(process)
            return process

        def line(process):
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "Patch worker did not report progress")
            return process.stdout.readline().decode().strip()

        try:
            first = launch("hold")
            self.assertEqual(line(first), "applying")
            self.assertEqual(line(first), "backup-held")
            second = launch("follow")
            self.assertEqual(line(second), "applying")
            ready, _, _ = select.select([second.stdout], [], [], 0.2)
            self.assertFalse(ready, "Second patch completed while first held its backup lock")
            first.stdin.write(b"release\n")
            first.stdin.flush()
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), b"applied")
            self.assertEqual(log.read_text(), "backup\n")
            self.assertEqual(self.target.read_text(), paseo_compat.patch_source(self.source))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
