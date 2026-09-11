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
TASK_SOURCE_FIXTURE = FIXTURE.parent / "subagents" / "live-source.js"
AGENT_SOURCE = Path("dist/server/server/agent/providers/claude/agent.js")
TASK_SOURCE = AGENT_SOURCE.parent / "subagents" / "live-source.js"
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
        self.edits = paseo_compat.replacements()

    def test_patch_is_narrow_reversible_and_idempotent(self):
        transformed = paseo_compat.patch_source(self.source)
        self.assertNotEqual(transformed, self.source)
        self.assertEqual(paseo_compat.patch_source(transformed), transformed)
        for upstream, patched in self.edits:
            self.assertEqual(transformed.count(patched), 1, patched[:80])
        self.assertEqual(transformed.count(PATCHED_INITIALIZER), 1)
        self.assertNotIn(INITIALIZER + ";", transformed)
        self.assertNotIn(paseo_compat.TASK_SOURCE_IMPORT, transformed)
        self.assertNotIn(paseo_compat.AVAILABLE_MODES, transformed)
        native = self.source.split("class ClaudeContextUsageState {", 1)[1].split(
            "\nconst DEFAULT_MODES = [", 1)[0]
        self.assertIn("class ClaudeCodexBaseContextUsageState {" + native, transformed)
        # Every edit is a one-to-one rewrite: undoing them restores the upstream file exactly.
        self.assertEqual(paseo_compat.reverse_edits(transformed, self.edits), self.source)
        # Skills, tasks, and modes are only rewired; no upstream function is removed.
        for retained in ("function readClaudeHistoricalSubagentToolCalls(entries) {",
                         "function claudeAutoModeUnavailableOn(env) {",
                         "translateSidechainFrameToEvents(message, parentToolUseId) {"):
            self.assertEqual(transformed.count(retained), 1, retained)

    def test_unknown_missing_or_duplicate_anchors_fail_closed(self):
        anchors = (*paseo_compat.AGENT_ANCHORS, *(upstream for upstream, _ in self.edits))
        for anchor in anchors:
            self.assertEqual(self.source.count(anchor), 1, anchor)
            for candidate in (self.source.replace(anchor, "unsupported_anchor", 1),
                              self.source + "\n" + anchor + "\n"):
                with self.subTest(anchor=anchor[:60], duplicate=candidate.startswith(self.source)):
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
            self.source.replace(paseo_compat.MODE_CATALOG, paseo_compat.PATCHED_MODE_CATALOG, 1),
            transformed.replace(PATCHED_INITIALIZER, INITIALIZER, 1),
            transformed.replace(paseo_compat.PATCHED_RESOLVE_SIDECHAIN, paseo_compat.RESOLVE_SIDECHAIN, 1),
            transformed.replace("class ClaudeCodexBaseContextUsageState {",
                                "class ChangedBaseContextUsageState {", 1),
            transformed.replace("CLAUDE_CODEX_PASEO_USAGE", "CHANGED_USAGE_MARKER", 1),
            transformed.replace("declareForkedSkill(parentToolUseId)", "declareForkedSkill(otherId)", 1),
            transformed + "\n" + transformed,
        )
        for index, candidate in enumerate(candidates):
            with self.subTest(source=index):
                with self.assertRaises(claude_codex.SetupError):
                    paseo_compat.patch_source(candidate)

    def test_previous_installer_layout_is_upgraded_to_the_current_patch(self):
        previous = self.source
        for upstream, patched in paseo_compat.previous_replacements():
            self.assertEqual(previous.count(upstream), 1, upstream[:60])
            previous = previous.replace(upstream, patched, 1)
        self.assertIn(paseo_compat.PATCH_MARKER, previous)
        self.assertNotEqual(previous, paseo_compat.patch_source(self.source))
        upgraded = paseo_compat.patch_source(previous)
        self.assertEqual(upgraded, paseo_compat.patch_source(self.source))
        self.assertEqual(paseo_compat.patch_source(upgraded), upgraded)
        # A locally edited earlier patch is neither trusted nor upgraded blindly.
        for candidate in (previous.replace("this.codexLateUsage = launchEnv", "this.codexLateUsage = process.env", 1),
                          previous.replace(PATCHED_INITIALIZER, INITIALIZER, 1)):
            with self.assertRaises(claude_codex.SetupError):
                paseo_compat.patch_source(candidate)


@unittest.skipUnless(NODE, "Node is required to execute the transformed fixture in memory")
class NodeFixtureTests(unittest.TestCase):
    """Runs JavaScript against the transformed fixture with shared message helpers."""

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
            # The fixture imports Paseo's subagent modules from beside itself.
            cwd=FIXTURE.parent,
            # Do not inherit preload hooks or provider configuration from the host.
            env={"PATH": os.defpath, **(process_env or {})},
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class UsageStateTests(NodeFixtureTests):
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
        self.run_js("""
            const originalModule = await import(""" + json.dumps(FIXTURE.as_uri()) + """);
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


class SubagentTrackingTests(NodeFixtureTests):
    """Forked skills are declared and finished like announced tasks, for marked sessions only."""

    SCENARIO = """\
        const skillId = 'call_skill';
        const mainSkill = {type: 'assistant', parent_tool_use_id: null, message: {content: [
            {type: 'tool_use', id: skillId, name: 'Skill', input: {skill: 'forky', args: 'review it'}}]}};
        const forkText = {type: 'assistant', parent_tool_use_id: skillId, message: {model: 'gpt-6-astra',
            content: [{type: 'text', text: 'working'}]}};
        const forkAgent = {type: 'assistant', parent_tool_use_id: skillId, message: {content: [
            {type: 'tool_use', id: 'call_child', name: 'Agent', input: {description: 'grandchild',
             prompt: 'x', subagent_type: 'general-purpose', run_in_background: true}}]}};
        const childStarted = {type: 'system', subtype: 'task_started', task_id: 'a1', tool_use_id: 'call_child',
            task_type: 'local_agent', subagent_type: 'general-purpose', description: 'grandchild',
            is_backgrounded: true, spawn_depth: 2, prompt: 'x'};
        const childFrame = {type: 'assistant', parent_tool_use_id: 'call_child', message: {model: 'gpt-6-astra',
            content: [{type: 'text', text: 'sub done'}]}};
        const childDone = {type: 'system', subtype: 'task_notification', task_id: 'a1', tool_use_id: 'call_child',
            status: 'completed', output_file: '', summary: 'ok'};
        const skillResult = (isError = false) => ({type: 'user', parent_tool_use_id: null, message: {content: [
            {type: 'tool_result', tool_use_id: skillId, is_error: isError,
             content: 'Skill "forky" completed (forked execution).'}]},
            tool_use_result: {status: 'forked', agentId: 'afork', success: !isError}});
        const trace = (session, messages) => messages.flatMap(message => session.translateMessageToEvents(message));
        const upserts = events => events.filter(event => event.type === 'provider_subagent' &&
            event.event.type === 'upsert').map(event => event.event);
        const cards = events => events.filter(event => event.type === 'timeline');
    """

    def run_js(self, body, **kwargs):
        super().run_js(textwrap.dedent(self.SCENARIO) + textwrap.dedent(body), **kwargs)

    def test_forked_skill_is_declared_nested_and_finished_for_marked_sessions(self):
        self.run_js("""
            const current = session('custom-model');
            const opened = trace(current, [mainSkill, forkText]);
            assert.deepEqual(upserts(opened), [
                {type: 'upsert', id: skillId, status: 'running', title: 'forky', description: 'review it', toolCallId: skillId},
                {type: 'upsert', id: skillId, subtitle: 'forky'},
                {type: 'upsert', id: skillId, subtitle: 'forky · gpt-6-astra'},
            ]);
            assert.deepEqual(cards(opened), [], 'The Skill call already has its own card');
            const launched = trace(current, [forkAgent, childStarted]);
            const child = upserts(launched).find(event => event.id === 'call_child');
            assert.equal(child.status, 'running');
            assert.equal(child.parentSubagentId, skillId, 'Children of the fork nest under it');
            assert.deepEqual(cards(launched), [], 'A nested child gets no synthetic Task card in the parent transcript');
            assert.equal(current.taskProtocolSource.ownerSubagentIdByToolUseId.get('call_child'), skillId);
            assert.ok(current.taskProtocolSource.backgroundedIds.has('call_child'),
                'task_started announces the background launch');
            assert.deepEqual(current.taskProtocolSource.cancelRunningForegroundTasks(),
                [{kind: 'status', id: skillId, status: 'canceled'}], 'Only the foreground fork dies with an interrupted turn');
            current.taskProtocolSource.lastStatusById.set(skillId, 'running');
            assert.ok(trace(current, [forkText, childFrame]).length >= 0, 'Later fork frames still route');
            assert.deepEqual(upserts(trace(current, [childDone])), [{type: 'upsert', id: 'call_child', status: 'completed'}]);
            assert.deepEqual(upserts(trace(current, [skillResult()])), [{type: 'upsert', id: skillId, status: 'completed'}]);
            assert.deepEqual(upserts(trace(current, [skillResult()])), [], 'Terminal status is reported once');
            assert.deepEqual([...current.taskProtocolSource.lastStatusById.values()], ['completed', 'completed']);
            const failing = session('custom-model');
            trace(failing, [mainSkill, forkText]);
            assert.deepEqual(upserts(trace(failing, [skillResult(true)])), [{type: 'upsert', id: skillId, status: 'failed'}]);
        """)

    def test_fork_tracking_ignores_auto_mode_override_and_process_environment(self):
        self.run_js("""
            const overridden = session('custom-model', {...markedEnv, CLAUDE_CODEX_AUTO_MODE: '1'});
            assert.equal(upserts(trace(overridden, [mainSkill, forkText]))[0].title, 'forky');
            for (const env of [{}, {CLAUDE_CODEX_PASEO_USAGE: '0'}, {CLAUDE_CODEX_PASEO_USAGE: 'true'}]) {
                const native = session('custom-model', env);
                const events = upserts(trace(native, [mainSkill, forkText]));
                assert.equal(events[0].title, 'Claude subagent', JSON.stringify(env));
            }
            const launchOnly = session('custom-model', {}, markedEnv);
            assert.equal(upserts(trace(launchOnly, [mainSkill, forkText]))[0].title, 'forky');
        """, process_env={"CLAUDE_CODEX_PASEO_USAGE": "1"})

    def test_unmarked_session_matches_original_provider_events(self):
        self.run_js("""
            const originalModule = await import(""" + json.dumps(FIXTURE.as_uri()) + """);
            const messages = [mainSkill, forkText, forkAgent, childStarted, childFrame, childDone, forkText, skillResult()];
            const replay = Session => {
                const current = new Session({model: 'custom-model'}, {runtimeSettings: {env: {}}, launchEnv: {}});
                const events = trace(current, messages);
                return {events, statuses: [...current.taskProtocolSource.lastStatusById],
                        modes: current.getAvailableModes().map(mode => mode.id)};
            };
            const patched = replay(ClaudeAgentSession);
            assert.deepEqual(patched, replay(originalModule.ClaudeAgentSession));
            // The unmarked path keeps Paseo's own behavior, including the orphaned fork row.
            const forkRow = upserts(patched.events).filter(event => event.id === skillId);
            assert.ok(forkRow.length > 0);
            assert.ok(forkRow.every(event => event.status === 'running'), JSON.stringify(forkRow));
        """)

    def test_nested_fork_inside_an_announced_child_is_tracked(self):
        self.run_js("""
            const current = session('custom-model');
            trace(current, [mainSkill, forkText, forkAgent, childStarted]);
            const childSkill = {type: 'assistant', parent_tool_use_id: 'call_child', message: {content: [
                {type: 'tool_use', id: 'call_skill2', name: 'Skill', input: {skill: 'nested'}}]}};
            const nestedFrame = {type: 'assistant', parent_tool_use_id: 'call_skill2', message: {content: [
                {type: 'text', text: 'nested working'}]}};
            const nestedResult = {type: 'user', parent_tool_use_id: 'call_child', message: {content: [
                {type: 'tool_result', tool_use_id: 'call_skill2', content: 'Skill "nested" completed (forked execution).'}]}};
            trace(current, [childSkill]);
            const declared = upserts(trace(current, [nestedFrame]))[0];
            assert.equal(declared.id, 'call_skill2');
            assert.equal(declared.title, 'nested');
            assert.equal(declared.parentSubagentId, 'call_child');
            assert.equal(declared.description, undefined);
            assert.deepEqual(upserts(trace(current, [nestedResult])), [{type: 'upsert', id: 'call_skill2', status: 'completed'}]);
        """)

    def test_only_skill_calls_are_declared_from_frames(self):
        self.run_js("""
            const current = session('custom-model');
            const plainTool = {type: 'assistant', parent_tool_use_id: null, message: {content: [
                {type: 'tool_use', id: 'call_bash', name: 'Bash', input: {command: 'true'}}]}};
            const strayFrame = {type: 'assistant', parent_tool_use_id: 'call_bash', message: {content: [
                {type: 'text', text: 'unexpected'}]}};
            trace(current, [plainTool]);
            const events = trace(current, [strayFrame]);
            assert.equal(current.taskProtocolSource.isDeclared('call_bash'), false);
            assert.equal(upserts(events)[0].title, 'Claude subagent', 'Non-skill parents keep the legacy path');
            const unknownParent = trace(current, [{type: 'assistant', parent_tool_use_id: 'call_unknown',
                message: {content: [{type: 'text', text: 'x'}]}}]);
            assert.equal(current.taskProtocolSource.isDeclared('call_unknown'), false);
        """)


class ModeCatalogTests(NodeFixtureTests):
    def test_marked_provider_environment_removes_auto_mode_unless_overridden(self):
        self.run_js("""
            const ids = catalog => catalog.modes.map(mode => mode.id);
            assert.deepEqual(claudeModeCatalog({}), {modes: DEFAULT_MODES, defaultModeId: 'auto'});
            const marked = claudeModeCatalog({CLAUDE_CODEX_PASEO_USAGE: '1'});
            assert.equal(marked.defaultModeId, 'default');
            assert.deepEqual(ids(marked), ['plan', 'default', 'acceptEdits', 'bypassPermissions']);
            const overridden = claudeModeCatalog({CLAUDE_CODEX_PASEO_USAGE: '1', CLAUDE_CODEX_AUTO_MODE: '1'});
            assert.deepEqual(overridden, {modes: DEFAULT_MODES, defaultModeId: 'auto'});
            for (const marker of [undefined, '', '0', 'true', ' 1']) {
                assert.equal(claudeModeCatalog({CLAUDE_CODEX_PASEO_USAGE: marker}).defaultModeId, 'auto', String(marker));
            }
            for (const value of ['true', '0', '']) {
                assert.equal(claudeModeCatalog({CLAUDE_CODEX_PASEO_USAGE: '1', CLAUDE_CODEX_AUTO_MODE: value}).defaultModeId,
                    'default', value);
            }
            const bedrock = claudeModeCatalog({CLAUDE_CODE_USE_BEDROCK: '1', CLAUDE_CODEX_PASEO_USAGE: '1',
                CLAUDE_CODEX_AUTO_MODE: '1'});
            assert.equal(bedrock.defaultModeId, 'default', "Paseo's own transport rule still wins");
        """)

    def test_running_session_advertises_the_same_modes_as_the_catalog(self):
        self.run_js("""
            const init = {type: 'system', subtype: 'init', permissionMode: 'default'};
            const marked = session('custom-model');
            // Paseo may snapshot the modes before Claude's init message arrives.
            assert.deepEqual(marked.getAvailableModes().map(mode => mode.id), ['plan', 'default', 'acceptEdits', 'bypassPermissions']);
            marked.translateMessageToEvents(init);
            assert.deepEqual(marked.getAvailableModes().map(mode => mode.id), ['plan', 'default', 'acceptEdits', 'bypassPermissions']);
            assert.equal(marked.currentMode, 'default');
            assert.deepEqual(marked.availableModes, DEFAULT_MODES, 'The stored list is not rewritten');
            const overridden = session('custom-model', {...markedEnv, CLAUDE_CODEX_AUTO_MODE: '1'});
            overridden.translateMessageToEvents(init);
            assert.deepEqual(overridden.getAvailableModes(), DEFAULT_MODES);
            const native = session('custom-model', {});
            native.translateMessageToEvents({...init, permissionMode: 'auto'});
            assert.deepEqual(native.getAvailableModes(), DEFAULT_MODES);
            assert.equal(native.currentMode, 'auto');
            const launchOverride = session('custom-model', markedEnv, {CLAUDE_CODEX_AUTO_MODE: '1'});
            launchOverride.translateMessageToEvents(init);
            assert.deepEqual(launchOverride.getAvailableModes(), DEFAULT_MODES);
        """)


class ReplayFactsTests(NodeFixtureTests):
    ENTRIES = """\
        const skillUse = {type: 'assistant', uuid: 'u1', message: {content: [
            {type: 'tool_use', id: 'call_skill', name: 'Skill', input: {skill: 'forky', args: 'review it'}}]}};
        const skillResult = (result, isError = false) => ({type: 'user', uuid: 'u2', message: {content: [
            {type: 'tool_result', tool_use_id: 'call_skill', is_error: isError, content: 'Skill "forky" completed (forked execution).'}]},
            toolUseResult: result});
        const forked = {success: true, commandName: 'forky', status: 'forked', agentId: 'afork', result: 'ok'};
        const agentUse = {type: 'assistant', isSidechain: true, agentId: 'afork', message: {content: [
            {type: 'tool_use', id: 'call_child', name: 'Agent', input: {description: 'grandchild', subagent_type: 'general-purpose'}}]}};
        const lines = entries => entries.map(entry => JSON.stringify(entry)).join('\\n') + '\\n';
    """

    def run_js(self, body, **kwargs):
        super().run_js(textwrap.dedent(self.ENTRIES) + textwrap.dedent(body), **kwargs)

    def test_forked_skill_results_link_the_child_transcript_when_requested(self):
        self.run_js("""
            const facts = readClaudeReplayParentFacts([skillUse, skillResult(forked)], true);
            assert.deepEqual(facts.toolCalls.get('call_skill'), {title: 'forky', description: 'review it'});
            assert.deepEqual(facts.linksByAgentId.get('afork'), {toolCallId: 'call_skill', failed: false});
            assert.deepEqual(facts.outcomesByToolCallId.get('call_skill'), {failed: false});
            const failed = readClaudeReplayParentFacts([skillUse, skillResult({...forked, success: false}, true)], true);
            assert.deepEqual(failed.linksByAgentId.get('afork'), {toolCallId: 'call_skill', failed: true});
            const inline = readClaudeReplayParentFacts([skillUse, skillResult({status: 'completed', result: 'ok'})], true);
            assert.equal(inline.linksByAgentId.size, 0, 'A skill that ran inline has no child transcript');
            assert.ok(inline.toolCalls.has('call_skill'));
            const noCall = readClaudeReplayParentFacts([skillResult(forked)], true);
            assert.equal(noCall.linksByAgentId.size, 0, 'A result without its Skill call proves nothing');
            const snake = readClaudeReplayParentFacts([skillUse, {...skillResult(undefined), tool_use_result: forked}], true);
            assert.deepEqual(snake.linksByAgentId.get('afork'), {toolCallId: 'call_skill', failed: false});
        """)

    def test_default_and_unmarked_replay_keep_upstream_facts(self):
        self.run_js("""
            const originalModule = await import(""" + json.dumps(FIXTURE.as_uri()) + """);
            const entries = [skillUse, skillResult(forked), agentUse];
            const upstream = originalModule.readClaudeReplayParentFacts(entries);
            assert.deepEqual(readClaudeReplayParentFacts(entries), upstream);
            assert.deepEqual(readClaudeReplayParentFacts(entries, false), upstream);
            assert.equal(upstream.toolCalls.has('call_skill'), false);
            assert.deepEqual(upstream.toolCalls.get('call_child'), {title: 'general-purpose', description: 'grandchild'});
            const sidechains = {contents: [lines([agentUse])], metaByAgentId: new Map()};
            const marked = session('custom-model').ingestPersistedSidechains(lines([skillUse, skillResult(forked)]), sidechains);
            assert.deepEqual(marked.parent.linksByAgentId.get('afork'), {toolCallId: 'call_skill', failed: false});
            assert.deepEqual(marked.subagents[0].parentFacts.toolCalls.get('call_child'),
                {title: 'general-purpose', description: 'grandchild'});
            const native = session('custom-model', {}).ingestPersistedSidechains(lines([skillUse, skillResult(forked)]), sidechains);
            assert.equal(native.parent.toolCalls.size, 0);
            assert.deepEqual(native, originalModule.ClaudeAgentSession.prototype.ingestPersistedSidechains.call(
                new originalModule.ClaudeAgentSession({model: 'custom-model'}, {runtimeSettings: {env: {}}, launchEnv: {}}),
                lines([skillUse, skillResult(forked)]), sidechains));
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
        sibling = root / TASK_SOURCE
        sibling.parent.mkdir(parents=True)
        shutil.copyfile(TASK_SOURCE_FIXTURE, sibling)
        return target

    def test_missing_or_unsupported_task_protocol_source_fails_without_changes(self):
        sibling = self.target.parent / "subagents" / "live-source.js"
        original = sibling.read_text()
        backup = Mock()
        for candidate in (original.replace("this.backgroundedIds = new Set();", "", 1),
                          original + "\nexport class ClaudeTaskProtocolSource {}\n", None):
            with self.subTest(candidate="missing" if candidate is None else candidate[-40:]):
                if candidate is None:
                    sibling.unlink()
                else:
                    sibling.write_text(candidate)
                with self.assertRaisesRegex(claude_codex.SetupError, "task protocol source"):
                    paseo_compat.prepare_patch(self.binary)
                with self.assertRaisesRegex(claude_codex.SetupError, "task protocol source"):
                    paseo_compat.apply_patch(self.target, backup)
        backup.assert_not_called()
        self.assertEqual(self.target.read_text(), self.source)
        sibling.write_text(original)
        self.assertEqual(paseo_compat.prepare_patch(self.binary), self.target.resolve())
        self.assertEqual(sibling.read_text(), original, "The sibling module is read, never modified")

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
