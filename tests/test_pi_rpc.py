import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import sapper
from pi_rpc import ExecutionSettings, Lifecycle, PiError, PiSession

FIXTURE = ROOT / "tests/fixtures/fake_pi.py"


class PiRpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.settings = ExecutionSettings("pi", executable=str(FIXTURE))

    def tearDown(self):
        self.tmp.cleanup()

    def session(self):
        return PiSession(self.settings, self.cwd, self.cwd / "sessions", timeout=3)

    def test_final_result_after_retry_split_unicode_and_stderr_pressure(self):
        for scenario in ("ok", "split", "retry", "stderr", "dialog"):
            with self.subTest(scenario=scenario), patch.dict(os.environ, FAKE_PI_SCENARIO=scenario):
                session = self.session()
                try:
                    result = session.run("benign", 3)["result"]
                    self.assertEqual(result, "cancelled" if scenario == "dialog" else "final 한글\u2028line\u2029paragraph")
                    self.assertEqual(session.state, Lifecycle.COMPLETED)
                finally:
                    session.close()
                self.assertIsNotNone(session.proc.poll())

    def test_failed_outcomes_are_visible_and_cleaned_up(self):
        for scenario in ("handled", "queued", "error", "aborted", "length", "deferred", "pending", "toolUse", "exit", "malformed", "timeout", "invalid-utf8", "invalid-message", "oversized", "incomplete"):
            with self.subTest(scenario=scenario), patch.dict(os.environ, FAKE_PI_SCENARIO=scenario):
                session = self.session()
                started = time.monotonic()
                try:
                    with self.assertRaises(PiError):
                        session.run("benign", .2)
                finally:
                    session.close()
                self.assertLess(time.monotonic() - started, 4)
                self.assertIsNotNone(session.proc.poll())

    def test_responses_correlate_by_id_not_arrival_order(self):
        session = self.session()
        try:
            first, second = session.send("reverse"), session.send("reverse")
            self.assertEqual(session.wait_response(first), {"value": first})
            self.assertEqual(session.wait_response(second), {"value": second})
        finally:
            session.close()

    def test_input_backpressure_obeys_deadline_and_kills_unresponsive_child(self):
        session = self.session()
        try:
            session.request("freeze")
            session.deadline = time.monotonic() + .2
            with self.assertRaisesRegex(PiError, "deadline"):
                session.send("prompt", message="x" * (1024 * 1024))
        finally:
            session.close()
        self.assertIsNotNone(session.proc.poll())

    def test_startup_missing_policy_and_unknown_model_fail(self):
        with patch.dict(os.environ, FAKE_PI_SCENARIO="missing-policy"):
            with self.assertRaisesRegex(PiError, "policy"):
                self.session()
        self.settings = ExecutionSettings("pi", model="not-real", executable=str(FIXTURE))
        with self.assertRaisesRegex(PiError, "no fallback"):
            self.session()

    def test_cli_defaults_and_unsupported_paths(self):
        parser = sapper.build_parser()
        self.assertEqual(sapper.resolve_execution(parser.parse_args(["dispatch", "reason"])).model, "sonnet")
        self.assertIsNone(sapper.resolve_execution(parser.parse_args(["dispatch", "reason", "--backend", "pi"])).model)
        self.assertEqual(sapper._worker_id(None), sapper._worker_id("pi"))
        with self.assertRaisesRegex(sapper.FGError, "empty"):
            sapper.resolve_execution(parser.parse_args(["dispatch", "reason", "--model", ""]))
        with contextlib.redirect_stderr(io.StringIO()):
            for command in (["bench"], ["sweep"], ["command"], ["fleet", "up"]):
                self.assertEqual(sapper.main(command + ["--backend", "pi"]), 1)
            self.assertEqual(sapper.main(["dispatch", "reason", "--backend", "pi", "--skip-permissions"]), 1)

    def test_failed_pi_explore_releases_its_claim_without_a_fact(self):
        store = sapper.Store(self.cwd / ".sapper")
        store.init("test", "benign")
        seed = store.add_fact("seed", "test", None)
        intent = store.add_intent([seed["id"]], "observe", "test")
        with patch.dict(os.environ, FAKE_PI_SCENARIO="handled"), contextlib.redirect_stderr(io.StringIO()), patch("sapper.Path.cwd", return_value=self.cwd):
            code = sapper.main(["--store", str(store.root), "dispatch", intent["id"], "--claim", "--backend", "pi", "--pi-bin", str(FIXTURE)])
        self.assertEqual(code, 1)
        self.assertFalse(store.get_intent(intent["id"]).get("worker"))
        self.assertEqual(len(store.list_facts()), 2)

    def test_auto_pi_failure_returns_nonzero_without_sleep_or_graph_change(self):
        store = sapper.Store(self.cwd / ".sapper")
        store.init("test", "benign")
        with patch.dict(os.environ, FAKE_PI_SCENARIO="handled"), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), patch("sapper.Path.cwd", return_value=self.cwd):
            code = sapper.main(["--store", str(store.root), "auto", "--backend", "pi", "--pi-bin", str(FIXTURE)])
        self.assertEqual(code, 1)
        self.assertEqual(store.list_intents(), [])
