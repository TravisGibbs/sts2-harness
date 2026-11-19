"""Offline regressions for reporting incomplete and differently configured runs."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import index
import report
import replay
import rundata


def run(seed="A", outcome="defeat", act=1, **extra):
    value = dict(id="run", model="openrouter:test/model", effort="default", protocol="tools",
                 key="openrouter:test/model|default", series="test/model", seed=seed,
                 attempt=1, character="ironclad", ascension=0, game_version="1", mod_version="1",
                 harness_commit="abc", prompt_fingerprint="prompt", limits={"max_requests": 600},
                 outcome=outcome, censored=outcome in rundata.CENSORED, final_floor=10,
                 max_act=act, act_count=3, floors=[], cost=0.01, requests=10, model_seconds=1,
                 suite="a", infrastructure_failure=outcome in rundata.INFRASTRUCTURE_FAILURES,
                 behavioral_failure=outcome in rundata.BEHAVIORAL_FAILURES)
    value.update(extra)
    return value


class ReportFairness(unittest.TestCase):
    def test_failures_are_not_game_deaths_or_decided_losses(self):
        rs = [run(outcome="victory", act=3), run(), run(outcome="api_error"), run(outcome="stalled")]
        summary = report.series_entry("model", rs, report.run_shape(rs, lambda r: r["seed"]))
        self.assertEqual(summary["deaths"], 1)
        self.assertEqual(summary["infrastructure_failures"], 1)
        self.assertEqual(summary["behavioral_failures"], 1)
        win = summary["stages"][-1]
        self.assertEqual(win["rate"], 0.5)
        self.assertEqual(win["batch_bounds"], [0.25, 0.75])
        self.assertEqual(win["undecided"], 2)

    def test_leaderboard_does_not_pool_different_conditions(self):
        rs = [run(), run(ascension=20), run(character="silent"), run(protocol="text"),
              run(limits={"max_requests": 10}), run(game_version="2"), run(harness_commit="def")]
        rows, _ = index.leaderboard(rs)
        self.assertEqual(len(rows), len(rs))
        self.assertTrue(all(r["runs"] == 1 for r in rows))

    def test_censored_runs_do_not_make_a_row_nonpreliminary(self):
        rows, _ = index.leaderboard([run()] + [run(outcome="api_error") for _ in range(10)])
        self.assertTrue(rows[0]["preliminary"])

    def test_pass_at_k_keeps_unresolved_seeds_in_sample(self):
        rs = [run("A", "victory", 3), run("A", "victory", 3), run("B", "api_error")]
        stage = report.pass_at_k(rs, 3, True)[0]
        self.assertEqual(stage["curve"], [])
        self.assertEqual(len(stage["by_seed"]), 2)

    def test_comparison_matches_pooled_rate_and_interval(self):
        # Unequal attempt counts: macro-average pass@1 is .5, pooled rate is .1.
        rs = [run("A", "victory", 3)] + [run("B") for _ in range(9)]
        shape = report.run_shape(rs, lambda r: r["seed"])
        self.assertEqual(report.series_entry("x", rs, shape)["pass_k"][0]["curve"][0], .5)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def load(name):
                return root / name, {}, [(Path(f"{i}.jsonl"), copy.deepcopy(r)) for i, r in enumerate(rs)]
            with patch.object(report, "REPO_ROOT", root), patch.object(report, "load_suite", side_effect=load), \
                 patch.object(report, "replay_link", return_value="replay.html"):
                page = report.build_compare(["a", "b"])
            data = json.loads(page.with_name("summary.json").read_text())
            row = data["compare"]["models"][0]["rows"][0]
            self.assertEqual(row["values"][0]["pass1"], .1)
            self.assertEqual(row["diffs"][0]["ci"], report.diff_interval(1, 10, 1, 10))

    def test_legacy_and_text_recording_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "run.jsonl"
            base = dict(type="run_start", model="test", snapshot={})
            p.write_text(json.dumps(base) + '\n' + json.dumps(dict(type="run_end", outcome="api_error")))
            old = rundata.summarize(p)
            self.assertEqual(old["protocol"], "tools")
            self.assertTrue(old["infrastructure_failure"])
            base["protocol"] = "text"
            p.write_text(json.dumps(base) + '\n' + json.dumps(dict(type="run_end", outcome="stalled", fallbacks=2)))
            text = rundata.summarize(p)
            self.assertNotEqual(text["key"], old["key"])
            self.assertTrue(text["behavioral_failure"])
            self.assertEqual(text["text_protocol"]["fallbacks"], 2)

    def test_game_rejection_is_distinct_from_normal_batch_stop(self):
        rejected = dict(tool="act", is_error=False, input={"commands": "play 0; end"},
                        result="! play 0: cannot play Strike: not enough energy\n! skipped 1 later command(s)\nCOMBAT turn 2")
        normal = dict(tool="act", is_error=False,
                      result="! stopped after 'play 0' (new cards entered your hand); skipped 1 later command(s)\nCOMBAT turn 2")
        render = dict(tool="act", is_error=False, result="! could not render COMBAT: failure")
        quality = rundata.action_quality([{"calls": [rejected, normal, render]}])
        self.assertEqual(quality["rejected_action_calls"], 1)
        self.assertEqual(quality["rejected_action_rate"], .3333)
        self.assertEqual(quality["bridge_error_calls"], 1)
        self.assertEqual(quality["tool_error_calls"], 0)
        self.assertTrue(rundata.call_diagnostics(rejected)["has_game_state"])
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "run.jsonl"
            p.write_text(json.dumps({"type": "step", "calls": [rejected]}))
            call = replay.build_data(p)["frames"][0]["calls"][0]
            self.assertEqual(call["result"], rejected["result"])
            self.assertFalse(call["is_error"])
            self.assertTrue(call["game_rejected"])

    def test_cost_audit_includes_archives_without_scoring_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder, cost in [("runs", .1), ("archive", .2)]:
                (root / folder).mkdir()
                (root / folder / "run.jsonl").write_text(json.dumps({"type": "step", "cost": cost}))
            (root / "cost_adjustments.json").write_text(json.dumps([
                {"amount_usd": .482, "reason": "Original recording missing", "source": "stdout"}]))
            audit = report.cost_audit(root)
            self.assertEqual(audit["scored_recordings_usd"], .1)
            self.assertEqual(audit["retry_archive_usd"], .2)
            self.assertEqual(audit["total_recorded_usd"], .782)

    def test_decisions_replay_keeps_typed_response_without_inventing_reasoning(self):
        detail = {"candidates": {"a000": "end", "a001": "play 0 e0"},
                  "criteria": {"a000": "Execute game command: end", "a001": "Execute game command: play 0 e0"},
                  "menu_truncated": True, "choice": "a001", "command": "play 0 e0",
                  "confidence": .91, "probabilities": {"a000": .09, "a001": .91}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'decisions.jsonl'
            path.write_text('\n'.join(json.dumps(event) for event in [
                {"type": "run_start", "model": "openrouter:jev", "protocol": "decisions"},
                {"type": "step", "text": "play 0 e0", "decision_response": detail, "provider_cost": .000012345}]),
                encoding='utf-8')
            data = replay.build_data(path)
            self.assertEqual(data['meta']['protocol'], 'decisions')
            self.assertEqual(data['frames'][0]['decision_response'], detail)
            self.assertEqual(data['frames'][0]['provider_cost'], .000012345)
            self.assertEqual(data['frames'][0]['thinking'], '')


if __name__ == "__main__":
    unittest.main()
