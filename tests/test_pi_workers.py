import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import threading
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import sapper
from pi_rpc import PiError, PiSession
from pi_workers import Supervisor, receive, request

FIXTURE = ROOT / "tests/fixtures/fake_pi.py"


class PiWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sp-", dir="/tmp")
        self.cwd = Path(self.tmp.name).resolve()
        self.store = sapper.Store(self.cwd / ".sapper")
        self.store.init("test", "local only")
        self.seed = self.store.add_fact("seed", "test", None)
        self.supervisor = Supervisor(self.cwd / ".sapper-runtime", 2, 60)

    def tearDown(self):
        for wid in list(self.supervisor.sessions):
            self.supervisor.stop(self.supervisor.workers[wid])
        self.supervisor.lock.close()
        self.tmp.cleanup()

    def spawn(self):
        return self.supervisor.handle(dict(action="spawn", cwd=str(self.cwd), settings=dict(
            backend="pi", executable=str(FIXTURE))))

    def settle(self, wid):
        end = time.monotonic() + 3
        while self.supervisor.workers[wid].status == "running" and time.monotonic() < end:
            self.supervisor.tick()
            time.sleep(.01)
        return self.supervisor.workers[wid]

    def test_same_model_workers_claim_distinct_intents_and_cli_identity(self):
        first, second = self.spawn(), self.spawn()
        self.assertNotEqual(first["id"], second["id"])
        for worker in (first, second):
            intent = self.store.add_intent([self.seed["id"]], "observe", "test")
            env = {**os.environ, "SAPPER_WORKER_ID": worker["id"]}
            cmd = [sys.executable, str(ROOT / "lib/sapper.py"), "--store", str(self.store.root)]
            result = subprocess.run(cmd + ["claim", intent["id"]], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.store.get_intent(intent["id"])["worker"], worker["id"])
            wrong = {**env, "SAPPER_WORKER_ID": "not-owner"}
            self.assertNotEqual(subprocess.run(cmd + ["done", intent["id"], "--fact", "bad"], env=wrong, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(cmd + ["done", intent["id"], "--fact", "observed"], env=env, capture_output=True).returncode, 0)

    def test_worker_message_result_stop_resume_exact_session_and_cap(self):
        worker = self.spawn()
        other = self.spawn()
        with self.assertRaisesRegex(PiError, "cap"):
            self.spawn()
        self.supervisor.handle(dict(action="message", id=worker["id"], message="hello"))
        with self.assertRaisesRegex(PiError, "idle or completed"):
            self.supervisor.handle(dict(action="message", id=worker["id"], message="duplicate"))
        result = self.settle(worker["id"])
        self.assertEqual(result.status, "completed")
        self.assertIn("final", result.result)
        old_session, generation = result.session_file, result.generation
        with self.assertRaisesRegex(PiError, "still alive"):
            self.supervisor.handle(dict(action="resume", id=result.id))
        self.supervisor.handle(dict(action="stop", id=result.id))
        resumed = self.supervisor.handle(dict(action="resume", id=result.id))
        self.assertEqual(resumed["session_file"], old_session)
        self.assertNotEqual(resumed["generation"], generation)
        self.assertIsNone(resumed["mission"])

    def test_intent_result_validated_before_graph_write(self):
        with patch.dict(os.environ, FAKE_PI_RESULT=json.dumps({"description": "local result"})):
            worker = self.spawn()
        intent = self.store.add_intent([self.seed["id"]], "observe", "test")
        self.supervisor.handle(dict(action="message", id=worker["id"], intent=intent["id"]))
        result = self.settle(worker["id"])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(self.store.get_intent(intent["id"])["worker"], worker["id"])
        self.assertIsNotNone(self.store.get_intent(intent["id"])["to"])

    def test_deadline_releases_only_owned_claims_and_persists(self):
        with patch.dict(os.environ, FAKE_PI_SCENARIO="timeout"):
            worker = self.spawn()
        intent = self.store.add_intent([self.seed["id"]], "observe", "test")
        other = self.store.add_intent([self.seed["id"]], "other", "test")
        self.store.claim_intent(other["id"], "unrelated")
        self.supervisor.handle(dict(action="message", id=worker["id"], intent=intent["id"]))
        self.supervisor.deadline = time.time() - 1
        self.supervisor.tick()
        self.assertEqual(self.supervisor.workers[worker["id"]].status, "interrupted")
        self.assertFalse(self.store.get_intent(intent["id"]).get("worker"))
        self.assertEqual(self.store.get_intent(other["id"])["worker"], "unrelated")
        deadline = self.supervisor.deadline
        self.supervisor.lock.close()
        self.supervisor = Supervisor(self.cwd / ".sapper-runtime", 8, 999)
        self.assertEqual(self.supervisor.deadline, deadline)
        self.assertEqual(self.supervisor.cap, 2)
        with self.assertRaisesRegex(PiError, "deadline"):
            self.spawn()

    def test_no_duplicate_supervisor_or_stale_generation(self):
        with self.assertRaisesRegex(PiError, "already owns"):
            Supervisor(self.cwd / ".sapper-runtime", 2, 60)
        worker = self.spawn()
        self.supervisor.handle(dict(action="message", id=worker["id"], message="hello"))
        self.supervisor.workers[worker["id"]].generation = "new-generation"
        result = self.settle(worker["id"])
        self.assertEqual(result.status, "failed")
        self.assertIn("stale", result.error)

    def test_nonfinite_deadlines_and_invalid_caps_are_rejected(self):
        for seconds in (float("nan"), float("inf"), 0):
            with self.assertRaises(PiError):
                Supervisor(self.cwd / "invalid", 2, seconds)
        for cap in (0, 33):
            with self.assertRaises(PiError):
                Supervisor(self.cwd / "invalid", cap, 60)
        runtime = self.cwd / "invalid"
        runtime.mkdir(mode=0o700)
        (runtime / "workers.json").write_text('{"deadline":NaN}')
        with self.assertRaisesRegex(PiError, "persisted"):
            Supervisor(runtime, 2, 60)

    def test_registry_worker_identity_cannot_escape_runtime(self):
        worker = self.spawn()
        worker["id"] = "../escape"
        runtime = self.cwd / "invalid"
        runtime.mkdir(mode=0o700)
        (runtime / "workers.json").write_text(json.dumps({"workers": {"../escape": worker}}))
        with self.assertRaisesRegex(PiError, "identity"):
            Supervisor(runtime, 2, 60)
        self.assertFalse((self.cwd / "escape").exists())

    def test_invalid_intent_payload_creates_no_fact_and_releases_claim(self):
        worker = self.spawn()
        intent = self.store.add_intent([self.seed["id"]], "observe", "test")
        before = self.store.list_facts()
        self.supervisor.handle(dict(action="message", id=worker["id"], intent=intent["id"]))
        result = self.settle(worker["id"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.store.list_facts(), before)
        self.assertFalse(self.store.get_intent(intent["id"]).get("worker"))

    def test_resumed_start_crash_retains_unresolved_ownership(self):
        self.supervisor.cap = 1
        worker = self.spawn()
        self.supervisor.handle(dict(action="message", id=worker["id"], message="persist session"))
        self.settle(worker["id"])
        stopped = self.supervisor.handle(dict(action="stop", id=worker["id"]))
        session_path = Path(stopped["session_file"])
        before = session_path.read_bytes()
        live = PiSession.__new__(PiSession)

        def crash_before_pid_recorded(*args, **kwargs):
            def crash(_pid):
                raise SystemExit("supervisor crashed before recording the child PID")
            kwargs["on_spawn"] = crash
            PiSession.__init__(live, *args, **kwargs)

        try:
            with patch("pi_workers.PiSession", side_effect=crash_before_pid_recorded):
                with self.assertRaises(SystemExit):
                    self.supervisor.handle(dict(action="resume", id=worker["id"]))
            self.assertIsNone(live.proc.poll())
            intent = self.store.add_intent([self.seed["id"]], "interrupted", "test")
            self.store.claim_intent(intent["id"], worker["id"])
            saved = json.loads((self.supervisor.runtime / "workers.json").read_text())
            self.assertIsNone(saved["workers"][worker["id"]]["pid"])
            self.assertEqual(saved["workers"][worker["id"]]["session_file"], str(session_path))
            for _ in range(2):
                self.supervisor.lock.close()
                self.supervisor = Supervisor(self.cwd / ".sapper-runtime", 1, 60)
                for action in ("resume", "stop"):
                    with self.assertRaisesRegex(PiError, "ambiguous startup ownership"):
                        self.supervisor.handle(dict(action=action, id=worker["id"]))
                with self.assertRaisesRegex(PiError, "cap"):
                    self.spawn()
                self.assertEqual(self.store.get_intent(intent["id"])["worker"], worker["id"])
                self.assertEqual(session_path.read_bytes(), before)
        finally:
            live.close()

    def test_failed_start_does_not_reserve_capacity(self):
        self.supervisor.cap = 1
        with self.assertRaisesRegex(PiError, "startup failed"):
            self.supervisor.handle(dict(action="spawn", cwd=str(self.cwd), settings=dict(
                backend="pi", executable=str(self.cwd / "missing-pi"))))
        self.assertEqual(self.spawn()["status"], "idle")

    def test_legacy_starting_record_without_pid_blocks_recovery(self):
        worker = self.spawn()
        self.supervisor.handle(dict(action="message", id=worker["id"], message="persist session"))
        self.settle(worker["id"])
        self.supervisor.handle(dict(action="stop", id=worker["id"]))
        self.supervisor.workers[worker["id"]].status = "starting"
        self.supervisor.save()
        self.supervisor.lock.close()
        self.supervisor = Supervisor(self.cwd / ".sapper-runtime", 2, 60)
        with self.assertRaisesRegex(PiError, "ambiguous startup ownership"):
            self.supervisor.handle(dict(action="resume", id=worker["id"]))

    def test_shutdown_during_mission_save_sends_no_prompt(self):
        worker = self.spawn()
        session = self.supervisor.sessions[worker["id"]][1]
        intent = self.store.add_intent([self.seed["id"]], "observe", "test")
        save = self.supervisor.save

        def save_then_shutdown():
            save()
            self.supervisor.stopping = True

        with patch.object(self.supervisor, "save", side_effect=save_then_shutdown), patch.object(session, "begin", wraps=session.begin) as begin:
            with self.assertRaisesRegex(PiError, "stopping"):
                self.supervisor.handle(dict(action="message", id=worker["id"], intent=intent["id"]))
            begin.assert_not_called()
        self.assertFalse(self.store.get_intent(intent["id"]).get("worker"))
        self.assertEqual(self.supervisor.workers[worker["id"]].status, "failed")

    def test_stopping_rejects_direct_message_before_mutation(self):
        worker = self.spawn()
        intent = self.store.add_intent([self.seed["id"]], "observe", "test")
        before = self.supervisor.handle(dict(action="peek", id=worker["id"]))
        self.supervisor.stopping = True
        with self.assertRaisesRegex(PiError, "stopping"):
            self.supervisor.message(self.supervisor.workers[worker["id"]], dict(intent=intent["id"]))
        self.assertEqual(self.supervisor.handle(dict(action="peek", id=worker["id"])), before)
        self.assertFalse(self.store.get_intent(intent["id"]).get("worker"))

    def test_partial_message_across_shutdown_signal_is_rejected(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                worker = self.spawn()
                session = self.supervisor.sessions[worker["id"]][1]
                intent = self.store.add_intent([self.seed["id"]], "observe", "test")
                interrupted = threading.Event()
                replies = queue.Queue()
                runtime = self.supervisor.runtime

                def shutdown(_signum, _frame):
                    self.supervisor.stopping = True
                    interrupted.set()

                def receive_across_signal(conn):
                    self.assertTrue(conn.recv(1, socket.MSG_PEEK))
                    signal.raise_signal(signum)
                    return receive(conn)

                def client():
                    try:
                        until = time.monotonic() + 3
                        while not (runtime / "rpc.sock").exists():
                            if time.monotonic() >= until:
                                raise TimeoutError("supervisor socket did not appear")
                            time.sleep(.01)
                        with socket.socket(socket.AF_UNIX) as conn:
                            conn.settimeout(3)
                            conn.connect(str(runtime / "rpc.sock"))
                            conn.sendall(json.dumps(dict(action="message", id=worker["id"], intent=intent["id"])).encode())
                            if not interrupted.wait(3):
                                raise TimeoutError("shutdown signal not delivered")
                            conn.sendall(b"\n")
                            replies.put(receive(conn))
                    except Exception as exc:
                        replies.put(exc)

                previous = signal.signal(signum, shutdown)
                thread = threading.Thread(target=client)
                try:
                    with patch("pi_workers.receive", side_effect=receive_across_signal), patch.object(session, "begin", wraps=session.begin) as begin:
                        thread.start()
                        self.supervisor.serve()
                        thread.join(timeout=4)
                        self.assertFalse(thread.is_alive())
                        reply = replies.get(timeout=1)
                        self.assertIsInstance(reply, dict)
                        self.assertFalse(reply["ok"], reply)
                        self.assertIn("stopping", reply["error"])
                        begin.assert_not_called()
                        self.assertFalse(self.store.get_intent(intent["id"]).get("worker"))
                finally:
                    signal.signal(signum, previous)
                    thread.join(timeout=4)
                    self.supervisor.lock.close()
                    self.supervisor = Supervisor(runtime, 2, 60)

    def test_restarted_supervisor_refuses_live_prior_pid(self):
        worker = self.spawn()
        self.supervisor.handle(dict(action="message", id=worker["id"], message="hello"))
        self.settle(worker["id"])
        interrupted_intent = self.store.add_intent([self.seed["id"]], "interrupted", "test")
        self.store.claim_intent(interrupted_intent["id"], worker["id"])
        live = self.supervisor.sessions.pop(worker["id"])[1]
        self.supervisor.lock.close()
        self.supervisor = Supervisor(self.cwd / ".sapper-runtime", 2, 60)
        try:
            with self.assertRaisesRegex(PiError, "still alive"):
                self.supervisor.handle(dict(action="resume", id=worker["id"]))
        finally:
            live.close()
        result = self.supervisor.handle(dict(action="resume", id=worker["id"]))
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["mission"])
        self.assertFalse(self.store.get_intent(interrupted_intent["id"]).get("worker"))
