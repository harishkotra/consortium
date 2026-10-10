"""Unit checks for the parts that must not be wrong: schema, scope enforcement, aggregation.

    .venv/bin/python -m unittest discover -s tests -t . -v

These cover the deterministic layers only. The three end-to-end demos (which involve the model)
are in scripts/verify.py.
"""

from __future__ import annotations

import statistics
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config as config_mod
from core import contract as contract_mod
from core import dataset
from core.events import LOG_PATH, emit, read_all
from core.negotiation import SchemaError, validate_message

from buyer_agent import normalise_plan

COLUMNS = set(dataset.columns())


def scope(columns, purpose="aov by region", days=30):
    return {"columns": list(columns), "purpose": purpose, "retention_days": days}


class TestSchema(unittest.TestCase):
    def test_valid_buyer_proposal(self):
        message = validate_message(
            {"from": "buyer", "intent": "propose", "scope": scope(["region", "order_value"]),
             "rationale": "Needed to answer the question."},
            expect_from="buyer",
            known_columns=COLUMNS,
        )
        self.assertEqual(message["scope"]["columns"], ["region", "order_value"])

    def test_wrong_sender_is_rejected(self):
        with self.assertRaises(SchemaError):
            validate_message({"from": "vendor", "intent": "propose", "scope": scope(["region"]),
                              "rationale": "x"}, expect_from="buyer", known_columns=COLUMNS)

    def test_invented_column_is_rejected(self):
        with self.assertRaises(SchemaError):
            validate_message({"from": "buyer", "intent": "propose",
                              "scope": scope(["average_order"]), "rationale": "x"},
                             known_columns=COLUMNS)

    def test_extra_key_is_rejected(self):
        with self.assertRaises(SchemaError):
            validate_message({"from": "buyer", "intent": "propose", "scope": scope(["region"]),
                              "rationale": "x", "urgency": "now"}, known_columns=COLUMNS)

    def test_non_integer_retention_is_rejected(self):
        for bad in ("30", 30.5, True, None):
            with self.assertRaises(SchemaError):
                validate_message({"from": "buyer", "intent": "propose",
                                  "scope": scope(["region"], days=bad), "rationale": "x"})

    def test_retention_is_capped_and_duplicates_dropped(self):
        message = validate_message({"from": "vendor", "intent": "counter",
                                    "scope": scope(["region", "region", "order_value"], days=9999),
                                    "rationale": "x"})
        self.assertEqual(message["scope"]["retention_days"], contract_mod.MAX_RETENTION_DAYS)
        self.assertEqual(message["scope"]["columns"], ["region", "order_value"])

    def test_reject_may_carry_no_columns(self):
        message = validate_message({"from": "vendor", "intent": "reject",
                                    "scope": scope([], purpose="pii only", days=1),
                                    "rationale": "Personal data is never granted."})
        self.assertEqual(message["scope"]["columns"], [])


class TestScopeEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._original = contract_mod.CONTRACT_PATH
        contract_mod.CONTRACT_PATH = Path(self.tmp.name) / "contract.json"
        contract_mod.clear()

    def tearDown(self):
        contract_mod.CONTRACT_PATH = self._original
        contract_mod.clear()
        self.tmp.cleanup()

    def test_no_contract_refuses_everything(self):
        allowed, reason, _ = contract_mod.evaluate({"region"}, COLUMNS)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("NO_CONTRACT"))

    def test_granted_columns_pass_and_denied_name_the_column(self):
        contract_mod.publish(columns=["region", "order_value", "order_date"], purpose="aov",
                             retention_days=30)
        allowed, reason, contract = contract_mod.evaluate({"region", "order_value"}, COLUMNS)
        self.assertTrue(allowed, reason)
        self.assertEqual(contract["scope"]["columns"], ["order_date", "order_value", "region"])

        allowed, reason, _ = contract_mod.evaluate({"region", "customer_email"}, COLUMNS)
        self.assertFalse(allowed)
        self.assertIn("SCOPE_DENIED", reason)
        self.assertIn("customer_email", reason)
        self.assertIn("['order_date', 'order_value', 'region']", reason)

    def test_expired_contract_refuses(self):
        contract_mod.publish(columns=["region"], purpose="aov", retention_days=30)
        stored = contract_mod.current()
        stored["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        contract_mod.CONTRACT_PATH.write_text(__import__("json").dumps(stored))
        allowed, reason, _ = contract_mod.evaluate({"region"}, COLUMNS)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("CONTRACT_EXPIRED"), reason)

    def test_unknown_column_is_not_a_scope_question(self):
        contract_mod.publish(columns=["region"], purpose="aov", retention_days=30)
        allowed, reason, _ = contract_mod.evaluate({"salary"}, COLUMNS)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("UNKNOWN_COLUMN"))


class TestAggregation(unittest.TestCase):
    def test_q3_mean_by_region_matches_direct_computation(self):
        rows = dataset._rows()
        q3 = [r for r in rows if "2025-07-01" <= str(r["order_date"]) <= "2025-09-30"]
        result = dataset.run_query({"order_date": {"gte": "2025-07-01", "lte": "2025-09-30"}},
                                   "region", {"function": "mean", "column": "order_value"})
        self.assertEqual(result["rows_matched"], len(q3))
        for entry in result["results"]:
            expected = statistics.fmean([r["order_value"] for r in q3 if r["region"] == entry["group"]])
            self.assertAlmostEqual(entry["mean(order_value)"], round(expected, 4), places=4)

    def test_dataset_meets_the_spec_floor(self):
        rows = dataset._rows()
        self.assertGreaterEqual(len(rows), 500)
        self.assertGreaterEqual(len(COLUMNS), 6)
        self.assertTrue({"customer_name", "customer_email"} <= COLUMNS)
        self.assertTrue(all(isinstance(r["order_value"], float) for r in rows))

    def test_count_star_and_ungrouped_total(self):
        result = dataset.run_query({"channel": "online"}, None, {"function": "count", "column": "*"})
        online = [r for r in dataset._rows() if r["channel"] == "online"]
        self.assertEqual(result["results"][0]["n_rows"], len(online))
        self.assertEqual(result["results"][0]["count(*)"], len(online))


class TestPlanNormalisation(unittest.TestCase):
    def test_stringified_filters_and_null_group(self):
        plan = normalise_plan({"filters": '{"channel": "online"}', "group_by": "None",
                               "aggregate": {"function": "MEAN", "column": "order_value"}})
        self.assertEqual(plan, {"filters": {"channel": "online"}, "group_by": None,
                                "aggregate": {"function": "mean", "column": "order_value"}})

    def test_bad_shape_raises(self):
        for bad in ({"filters": [], "group_by": None, "aggregate": {"function": "mean", "column": "x"}},
                    {"filters": {}, "group_by": 3, "aggregate": {"function": "mean", "column": "x"}},
                    {"filters": {}, "group_by": None, "aggregate": {"function": "median", "column": "x"}},
                    {"filters": {}, "group_by": None, "aggregate": {"function": "mean", "column": " "}}):
            with self.assertRaises(SchemaError):
                normalise_plan(bad)


class TestConfigRoundTrip(unittest.TestCase):
    def test_special_characters_survive_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            original = config_mod.ENV_PATH
            config_mod.ENV_PATH = path
            try:
                cfg = config_mod.Config(api_key='sk-abc "quoted" $value #hash', model="m/1",
                                        base_url="http://127.0.0.1:9/v1")
                config_mod.save(cfg)
                reloaded = config_mod.load()
                self.assertEqual(reloaded.api_key, 'sk-abc "quoted" $value #hash')
                self.assertEqual(reloaded.model, "m/1")
                self.assertEqual(reloaded.base_url, "http://127.0.0.1:9/v1")
                self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
            finally:
                config_mod.ENV_PATH = original

    def test_defaults_match_the_spec(self):
        self.assertEqual(config_mod.DEFAULT_BASE_URL, "https://api.particle.ai/v1")
        self.assertEqual(config_mod.DEFAULT_MODEL, "deepseek-v4.1-flash")
        self.assertIn("LM Studio", config_mod.PRESETS)
        self.assertIn("Ollama", config_mod.PRESETS)
        self.assertIn("Gemini", config_mod.PRESETS)


class TestEventLog(unittest.TestCase):
    def test_sequence_is_monotonic_and_readable(self):
        from core import events as events_mod

        with tempfile.TemporaryDirectory() as tmp:
            original = events_mod.LOG_PATH
            events_mod.LOG_PATH = Path(tmp) / "events.jsonl"
            try:
                self.assertEqual(read_all(), [])
                first = emit("run", "unittest", "probe")
                again = emit("run", "unittest", "probe", seq_note="second")
                self.assertEqual(first["seq"], 1)
                self.assertEqual(again["seq"], 2)
                stored = read_all()
                self.assertEqual([e["seq"] for e in stored], [1, 2])
                self.assertEqual(stored[-1]["kind"], "probe")
                self.assertEqual(stored[-1]["seq"], again["seq"])
            finally:
                events_mod.LOG_PATH = original

    def test_concurrent_writers_do_not_reuse_a_sequence_number(self):
        from concurrent.futures import ThreadPoolExecutor

        from core import events as events_mod

        with tempfile.TemporaryDirectory() as tmp:
            original = events_mod.LOG_PATH
            events_mod.LOG_PATH = Path(tmp) / "events.jsonl"
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda i: emit("run", "unittest", f"probe-{i}"), range(40)))
                seqs = [e["seq"] for e in read_all()]
                self.assertEqual(sorted(seqs), list(range(1, 41)))
            finally:
                events_mod.LOG_PATH = original


if __name__ == "__main__":
    unittest.main()
