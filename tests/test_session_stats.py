import importlib.util
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "session.jsonl")

spec = importlib.util.spec_from_file_location(
    "session_stats", os.path.join(HERE, "..", "token-coach", "scripts", "session_stats.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

visual_spec = importlib.util.spec_from_file_location(
    "visual_review", os.path.join(HERE, "..", "token-coach", "scripts", "visual_review.py"))
vr = importlib.util.module_from_spec(visual_spec)
visual_spec.loader.exec_module(vr)


class SessionStatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = ss.analyse(FIXTURE)

    def test_only_user_prompts_are_counted(self):
        # typed x3 + suggestion_accepted x1; isMeta and promptSource "system" are excluded
        self.assertEqual(self.out["user_prompts"], 4)

    def test_older_transcripts_without_prompt_source(self):
        entry = {"type": "user", "message": {"role": "user", "content": "hello"}}
        self.assertEqual(ss.real_prompt(entry), "hello")
        entry["isMeta"] = True
        self.assertIsNone(ss.real_prompt(entry))

    def test_corrections_use_typed_text_not_pasted_blocks(self):
        self.assertEqual(self.out["prompt_stats"]["correction_style_prompts"], 1)
        self.assertTrue(ss.is_correction("no, I meant the other file"))
        self.assertFalse(ss.is_correction("log:\n```\nno wrong again\n```"))

    def test_subagent_tokens_read_from_subagents_folder(self):
        # streamed duplicates of one message count once, at the largest value
        self.assertEqual(self.out["subagent_output_tokens"], 500)

    def test_test_run_detection(self):
        run = lambda cmd: ss.diy_category({"name": "Bash", "cmd": cmd, "size": 0})
        self.assertEqual(run("mvn test"), "running_tests")
        self.assertEqual(run("mvn -q clean test"), "running_tests")
        self.assertIsNone(run("mvn test-compile"))
        self.assertEqual(run("git status"), "git_commands")

    def test_chained_commands_must_all_be_simple(self):
        run = lambda cmd: ss.diy_category({"name": "Bash", "cmd": cmd, "size": 0})
        self.assertEqual(run("git log --oneline | head -5"), "git_commands")
        self.assertEqual(run("cd repo && git add -A && git commit -F - <<'EOF'\nmsg\nEOF"),
                         "git_commands")
        self.assertIsNone(run("git status && mvn package"))
        self.assertIsNone(run("for f in *; do git add $f; done"))

    def test_tool_result_totals_group_by_tool(self):
        totals = {t["tool"]: t for t in self.out["tool_result_totals"]}
        self.assertEqual(totals["Bash"]["calls"], 1)
        self.assertEqual(totals["mcp:jira"]["calls"], 2)
        self.assertEqual(totals["mcp:jira"]["est_tokens"], 200)

    def test_output_is_json_serialisable_with_range_text(self):
        json.dumps(self.out)
        self.assertIn("combined_pct_text", self.out["estimated_savings"])

    def test_visual_data_contains_chart_series_and_tool_calls(self):
        report = vr.report_data(self.out, {})
        self.assertEqual(len(report["context_growth"]), self.out["assistant_turns"])
        self.assertTrue(any(row["tool"] == "Bash" for row in report["tool_activity"]))
        self.assertTrue(any(row["label"] == "Output" for row in report["tokens"]))

    def test_visual_html_escapes_untrusted_insights(self):
        insights = {"findings": [{"title": "<img src=x onerror=alert(1)>",
                                   "why": "<script>alert(1)</script>",
                                   "recommendation": "Use <target>"}],
                    "recommendations": [], "prompt_improvements": [], "positives": []}
        page = vr.render(vr.report_data(self.out, insights))
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<img src=x onerror", page)

    def test_visual_report_handles_empty_or_partial_metrics(self):
        partial = {"transcript": "<unknown>", "assistant_turns": 0, "user_prompts": 0,
                   "tokens": {}, "cache_hit_ratio": None, "context_growth": [],
                   "tool_call_totals": [], "tool_result_totals": [],
                   "files_read_3plus_times": {}, "largest_tool_results": [],
                   "prompt_stats": {}, "costliest_prompts": [], "estimated_savings": {}}
        page = vr.render(vr.report_data(partial, {}))
        self.assertIn("Cache data unavailable", page)
        self.assertIn("&lt;unknown&gt;", page)
        self.assertNotIn("NaN", page)


    def test_line_marks_threshold_compaction_and_peak(self):
        values = [50_000, 120_000, 150_000, 40_000, 60_000]
        self.assertEqual(vr.compaction_turns(values), [3])
        points = [{"turn": i + 1, "request_tokens": v} for i, v in enumerate(values)]
        chart = vr.svg_line(points)
        self.assertIn('class="threshold"', chart)
        self.assertIn('class="drop"', chart)
        self.assertIn("peak 150,000", chart)

    def test_time_line_shades_idle_gaps(self):
        points = [{"request_tokens": 10, "timestamp": "2026-01-01T10:00:00Z"},
                  {"request_tokens": 20, "timestamp": "2026-01-01T10:01:00Z"},
                  {"request_tokens": 30, "timestamp": "2026-01-01T10:30:00Z"}]
        chart = vr.svg_time_line(points)
        self.assertEqual(chart.count('class="gap"><title>'), 1)
        self.assertEqual(vr.svg_time_line([{"request_tokens": 1, "timestamp": None}] * 2), "")

    def test_new_sections_render_and_escape_prompt_text(self):
        stats = dict(self.out)
        stats["costliest_prompts"] = [{"n": 2, "turns": 3, "output": 10, "cost_units": 500,
                                       "text": "<b>fix it</b>"}]
        stats["correction_examples"] = [{"n": 2, "text": "no"}]
        stats["estimated_savings"] = {"items": [{"tip": "compact", "pct_of_session": 30.0,
                                                 "confidence": "medium", "assumption": "a"}],
                                      "at_stake_not_savings": [{"tip": "be specific",
                                                                "pct_of_session": 20.0}],
                                      "combined_pct_text": "about 30%"}
        stats["prompt_stats"] = {"median_words": 16, "correction_style_prompts": 1}
        page = vr.render(vr.report_data(stats, {}))
        for title in ("Costliest prompts", "Estimated savings by habit", "Calls vs. result tokens",
                      "Prompt profile"):
            self.assertIn(title, page)
        self.assertIn("&lt;b&gt;fix it&lt;/b&gt;", page)
        self.assertIn("at stake, not savings", page)
        self.assertIn("correction", page)


    def test_report_defaults_to_reports_directory_outside_the_project(self):
        with tempfile.TemporaryDirectory() as reports, tempfile.TemporaryDirectory() as project:
            target = os.path.join(reports, "nested")
            stdout = io.StringIO()
            previous = os.getcwd()
            os.chdir(project)
            try:
                with mock.patch.object(vr, "REPORTS_DIRECTORY", target), \
                        mock.patch.object(sys, "argv", ["visual_review.py", FIXTURE]), \
                        contextlib.redirect_stdout(stdout):
                    vr.main()
            finally:
                os.chdir(previous)
            expected = os.path.join(target, "session.html")
            self.assertTrue(os.path.isfile(expected))
            self.assertEqual(stdout.getvalue().strip(), os.path.abspath(expected))
            self.assertEqual(os.listdir(project), [])

    def test_explicit_output_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as reports, tempfile.TemporaryDirectory() as other:
            output = os.path.join(other, "custom.html")
            with mock.patch.object(vr, "REPORTS_DIRECTORY", reports), \
                    mock.patch.object(sys, "argv", ["visual_review.py", FIXTURE, "--output", output]), \
                    contextlib.redirect_stdout(io.StringIO()):
                vr.main()
            self.assertTrue(os.path.isfile(output))
            self.assertEqual(os.listdir(reports), [])


if __name__ == "__main__":
    unittest.main()
