import hermetic  # Block live connections and discard inherited credentials.
import json
import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pco_seat_audit.cli import InputError, audit, main

ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / "examples/plan.json").read_text())
USAGE = json.loads((ROOT / "examples/usage.json").read_text())
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


class AuditTests(unittest.TestCase):
    def test_same_rule_covers_each_product_and_unused_publishing(self):
        results = audit(PLAN, USAGE, 90, now=NOW)
        self.assertEqual([x["product"] for x in results], ["check_ins", "registrations", "giving", "services", "publishing"])
        by_name = {x["product"]: x for x in results}
        self.assertEqual(by_name["check_ins"]["used"], 2)
        self.assertEqual(by_name["check_ins"]["finding"], "LOW USAGE")
        self.assertEqual(by_name["check_ins"]["possible_monthly_saving"], 20)
        self.assertEqual(by_name["giving"]["finding"], "PAID BUT UNUSED")
        self.assertEqual(by_name["publishing"]["finding"], "OK")

    def test_short_windows_filter_rows(self):
        result = audit(PLAN, USAGE, 30, now=NOW)
        self.assertEqual(result[0]["used"], 2)

    def test_unknown_product_and_bad_sizes_are_clear(self):
        for value, expected in [("mystery", "unknown product"), (0, "positive number"), (-2, "positive number")]:
            plan = json.loads(json.dumps(PLAN))
            plan["products"][0]["product"] = value if isinstance(value, str) else "check_ins"
            if not isinstance(value, str):
                plan["products"][0]["limit"] = value
            with self.assertRaisesRegex(InputError, expected):
                audit(plan, USAGE, 90, now=NOW)

    def test_missing_product_rows_and_short_rows_fail_cleanly(self):
        with self.assertRaisesRegex(InputError, "missing product rows"):
            audit(PLAN, {}, 90, now=NOW)
        usage = json.loads(json.dumps(USAGE))
        usage["check_ins"] = [{}]
        with self.assertRaisesRegex(InputError, "missing or malformed"):
            audit(PLAN, usage, 90, now=NOW)

    def test_output_cannot_overwrite_an_input(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = Path(folder) / "plan.json"
            usage = Path(folder) / "usage.json"
            plan.write_text(json.dumps(PLAN))
            usage.write_text(json.dumps(USAGE))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(["--plan", str(plan), "--usage", str(usage), "--output", str(plan)])
            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue().strip(), "error: output path must differ from every input path")

    def test_malformed_input_file_is_one_line_error(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "bad.json"
            bad.write_text("{")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(["--plan", str(bad), "--usage", str(ROOT / "examples/usage.json"), "--output", str(Path(folder) / "out")])
            self.assertEqual(code, 2)
            self.assertTrue(stderr.getvalue().startswith("error: invalid JSON in "))
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
