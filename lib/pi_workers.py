from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import json
import math
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

from pi_rpc import ExecutionSettings, Lifecycle, PiError, PiSession, private_dir
from pi_setup import atomic_json

MAX_REQUEST = 65536


@dataclass
class Worker:
    id: str
    generation: str
    cwd: str
    store: str
    settings: dict
    status: str
    session_file: str | None
    pid: int | None
    mission: dict | None
    created_at: float
    updated_at: float
    result: str | None = None
    error: str | None = None


def pid_exists(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def receive(conn):
    data = bytearray()
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise PiError("worker connection closed before a complete record")
        data.extend(chunk)
        if len(data) > MAX_REQUEST:
            raise PiError("worker request exceeds limit")
    value = json.loads(data.partition(b"\n")[0])
    if not isinstance(value, dict):
        raise PiError("worker request must be an object")
    return value


def request(runtime, command, timeout=40):
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(str(runtime / "rpc.sock"))
            conn.sendall((json.dumps(command) + "\n").encode())
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = conn.recv(65536)
                if not chunk:
                    raise PiError("supervisor closed the connection")
                chunks.extend(chunk)
                if len(chunks) > 8 * 1024 * 1024:
                    raise PiError("worker response exceeds limit")
            reply = json.loads(chunks.partition(b"\n")[0])
        except OSError as exc:
            raise PiError(f"supervisor unavailable: {exc}") from exc
    if not reply.get("ok"):
        raise PiError(reply.get("error", "worker operation failed"))
    return reply["data"]


class Supervisor:
    def __init__(self, runtime: Path, cap: int, seconds: float):
        if cap < 1 or cap > 32 or not math.isfinite(seconds) or seconds <= 0:
            raise PiError("worker cap must be 1..32 and lifetime must be positive")
        self.runtime = private_dir(runtime)
        if len(os.fsencode(self.runtime / "rpc.sock")) >= 100:
            raise PiError("campaign path too long for a portable Unix socket; use a shorter path")
        self.lock = (self.runtime / "owner.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise PiError("supervisor already owns this campaign") from exc
        self.sessions = {}
        self.stopping = False
        self.socket = None
        try:
            path = self.runtime / "workers.json"
            saved = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(saved, dict) or not isinstance(saved.get("workers", {}), dict):
                raise PiError("invalid worker registry")
            self.cap = saved.get("cap", cap)
            self.deadline = saved.get("deadline", time.time() + seconds)
            if not isinstance(self.cap, int) or not 1 <= self.cap <= 32 or not isinstance(self.deadline, (int, float)) or not math.isfinite(self.deadline):
                raise PiError("invalid persisted worker cap or deadline")
            self.workers = {k: Worker(**v) for k, v in saved.get("workers", {}).items()}
            for worker_id, worker in self.workers.items():
                if worker.id != worker_id or re.fullmatch(r"w-[0-9a-f]{16}", worker_id) is None:
                    raise PiError("invalid persisted worker identity")
                if worker.pid is not None and (not isinstance(worker.pid, int) or worker.pid <= 0):
                    raise PiError("invalid persisted worker PID")
                if worker.status == "starting" and worker.pid is None:
                    worker.status = "unresolved"
                if worker.pid is not None or worker.status in ("starting", "running", "idle", "completed"):
                    worker.status = "interrupted"
                    worker.error = "supervisor restarted; prior process must be gone before resume"
            self.save()
        except Exception:
            self.lock.close()
            raise

    def save(self):
        atomic_json(self.runtime / "workers.json", dict(cap=self.cap, deadline=self.deadline,
            owner_pid=os.getpid(), workers={k: asdict(v) for k, v in self.workers.items()}))

    def release_claims(self, worker):
        import sapper
        store = sapper.Store(Path(worker.store))
        if store.exists():
            for intent in store.list_intents():
                if intent.get("worker") == worker.id and intent.get("to") is None:
                    store.release_intent(intent["id"], worker.id)

    def check_capacity(self):
        if self.stopping or time.time() >= self.deadline:
            raise PiError("campaign deadline reached or supervisor is stopping")
        occupied = sum(1 for w in self.workers.values() if w.id in self.sessions or pid_exists(w.pid)
                       or w.status == "unresolved" or (w.status == "interrupted" and w.session_file is None))
        if occupied >= self.cap:
            raise PiError("global worker cap reached")

    def start(self, worker):
        self.check_capacity()
        worker.generation = uuid.uuid4().hex
        worker.status, worker.error, worker.pid = "unresolved", None, None
        worker.updated_at = time.time()
        self.save()

        def spawned(pid):
            worker.status, worker.pid = "starting", pid
            self.save()

        try:
            session = PiSession(ExecutionSettings(**worker.settings), Path(worker.cwd),
                private_dir(self.runtime / worker.id), session_file=worker.session_file,
                worker_id=worker.id, store_path=Path(worker.store), timeout=min(30, self.deadline - time.time()), on_spawn=spawned)
            worker.session_file = session.session_file
            worker.settings["model"] = session.model["id"]
            worker.settings["provider"] = session.model["provider"]
            worker.status = "idle"
            self.sessions[worker.id] = (worker.generation, session)
            self.save()
        except Exception as exc:
            worker.status, worker.error = "failed", str(exc)
            if not pid_exists(worker.pid):
                worker.pid = None
            self.save()
            raise
        return asdict(worker)

    def stop(self, worker, status="stopped"):
        owned = self.sessions.pop(worker.id, None)
        if owned:
            owned[1].close()
            worker.pid = None
        elif pid_exists(worker.pid):
            raise PiError("cannot signal a process from a prior supervisor; wait for its confirmed exit")
        elif worker.status == "unresolved" or (worker.status in ("starting", "interrupted") and worker.session_file is None):
            raise PiError("ambiguous startup ownership; inspect the campaign before recovery")
        self.release_claims(worker)
        worker.status, worker.updated_at = status, time.time()
        worker.mission = None
        self.save()
        return asdict(worker)

    def message(self, worker, data):
        import sapper
        if self.stopping:
            raise PiError("supervisor is stopping")
        owned = self.sessions.get(worker.id)
        if not owned or worker.status not in ("idle", "completed"):
            raise PiError("worker must be idle or completed; resume stopped workers first")
        if time.time() >= self.deadline:
            raise PiError("campaign deadline reached")
        text = data.get("message", "")
        target = data.get("intent")
        if not isinstance(text, str) or len(text) > 48000:
            raise PiError("message must be a string under 48000 characters")
        if target is not None and not isinstance(target, str):
            raise PiError("intent must be an ID string")
        store = sapper.Store(Path(worker.store))
        if target:
            intent = store.get_intent(target)
            if intent["to"] is not None:
                raise PiError("intent already concluded")
            if intent.get("requires_confirmation") and not intent.get("confirmed_at"):
                raise PiError("intent requires confirmation")
            text = sapper.render_intent_prompt(store,
                sapper._load_template(Path(__file__).resolve().parents[1] / "templates", "explore.md"), intent)
        if not text.strip():
            raise PiError("provide a message or --intent")
        session = owned[1]
        worker.mission = dict(id=uuid.uuid4().hex, generation=worker.generation, intent=target)
        worker.status, worker.result, worker.error = "running", None, None
        worker.updated_at = time.time()
        self.save()
        try:
            if target:
                store.claim_intent(target, worker.id)
            if self.stopping:
                raise PiError("supervisor is stopping")
            session.begin(text, max(.01, self.deadline - time.time()))
        except Exception as exc:
            worker.error = str(exc)
            self.stop(worker, "failed")
            raise
        return asdict(worker)

    def tick(self):
        import sapper
        for worker_id, (generation, session) in list(self.sessions.items()):
            worker = self.workers[worker_id]
            if time.time() >= self.deadline:
                worker.error = "campaign deadline reached"
                self.stop(worker, "interrupted" if worker.status == "running" else "stopped")
                continue
            try:
                if worker.status != "running":
                    session.pump(0)
                    if session.proc.poll() is not None:
                        raise PiError("Pi process exited while idle")
                    continue
                result = session.poll_result()
                if result is None:
                    continue
                mission = worker.mission
                if generation != worker.generation or not mission or mission["generation"] != generation:
                    raise PiError("stale worker generation result rejected")
                if mission["intent"]:
                    store = sapper.Store(Path(worker.store))
                    payload = sapper.extract_json_object(result["result"])
                    title, desc, doc, kind, fields = sapper._parse_explore_payload(payload)
                    intent = store.get_intent(mission["intent"])
                    if intent["to"] is None:
                        store.conclude_with_new_fact(intent["id"], desc, worker.id,
                            title=title, doc=doc, kind=kind, data=fields)
                    elif intent.get("worker") != worker.id:
                        raise PiError("intent was concluded by another worker")
                worker.result, worker.status = result["result"], "completed"
                worker.updated_at = time.time()
                self.save()
            except (PiError, sapper.FGError) as exc:
                worker.error = str(exc)
                self.stop(worker, "dead" if session.proc.poll() is not None else "failed")

    def handle(self, data):
        action = data.get("action")
        if action in ("list", "board", "ping"):
            return dict(deadline=self.deadline, cap=self.cap, workers=[asdict(w) for w in self.workers.values()])
        if action == "down":
            self.stopping = True
            return {"stopping": True, "cap": self.cap}
        if action == "spawn":
            self.check_capacity()
            cwd = Path(data.get("cwd", "")).resolve()
            if not cwd.is_dir():
                raise PiError("worker cwd must exist")
            settings = ExecutionSettings(**data.get("settings", {}))
            if settings.backend != "pi":
                raise PiError("managed workers require the Pi backend")
            worker_id = "w-" + uuid.uuid4().hex[:16]
            worker = Worker(worker_id, "", str(cwd), str(cwd / ".sapper"), asdict(settings),
                "new", None, None, None, time.time(), time.time())
            self.workers[worker_id] = worker
            return self.start(worker)
        worker_id = data.get("id")
        if not isinstance(worker_id, str) or worker_id not in self.workers:
            raise PiError("unknown worker ID")
        worker = self.workers[worker_id]
        if action == "peek":
            return asdict(worker)
        if action == "stop":
            return self.stop(worker)
        if action == "resume":
            if worker.status == "unresolved":
                raise PiError("ambiguous startup ownership; inspect the campaign before recovery")
            if worker.id in self.sessions or pid_exists(worker.pid):
                raise PiError("prior worker process is still alive; refusing duplicate session ownership")
            if not worker.session_file or not Path(worker.session_file).is_file():
                raise PiError("exact recorded session missing; cannot resume")
            expected_dir = self.runtime / worker.id
            if Path(worker.session_file).resolve().parent != expected_dir:
                raise PiError("session ownership mismatch")
            self.release_claims(worker)
            worker.mission = None
            worker.pid = None
            return self.start(worker)
        if action == "message":
            return self.message(worker, data)
        raise PiError("unknown worker action")

    def serve(self):
        import sapper
        path = self.runtime / "rpc.sock"
        path.unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX)
        try:
            self.socket.bind(str(path))
            os.chmod(path, 0o600)
            self.socket.listen(8)
            self.socket.settimeout(.05)
            while not self.stopping:
                self.tick()
                try:
                    conn, _ = self.socket.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(2)
                    try:
                        data = receive(conn)
                        if self.stopping:
                            raise PiError("supervisor is stopping")
                        reply = dict(ok=True, data=self.handle(data))
                    except (PiError, sapper.FGError, ValueError, TypeError, OSError) as exc:
                        reply = dict(ok=False, error=str(exc))
                    try:
                        conn.sendall((json.dumps(reply) + "\n").encode())
                    except OSError:
                        pass
        finally:
            for worker_id in list(self.sessions):
                self.stop(self.workers[worker_id], "interrupted" if self.workers[worker_id].status == "running" else "stopped")
            self.socket.close()
            path.unlink(missing_ok=True)
            self.lock.close()


def cli(args):
    import sapper
    runtime = Path.cwd() / ".sapper-runtime"
    try:
        if args.action == "serve":
            supervisor = Supervisor(runtime, args.max_workers, args.seconds)
            def shutdown(_signum, _frame):
                supervisor.stopping = True
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            supervisor.serve()
            return 0
        if args.action == "up":
            runtime = private_dir(runtime)
            try:
                result = request(runtime, {"action": "ping"}, timeout=1)
            except PiError:
                command = [sys.executable, str(Path(__file__).with_name("sapper.py")), "worker", "serve",
                    "--max-workers", str(args.max_workers), "--seconds", str(args.seconds)]
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                until = time.monotonic() + 5
                while True:
                    try:
                        result = request(runtime, {"action": "ping"}, timeout=.2)
                        break
                    except PiError:
                        if proc.poll() is not None or time.monotonic() >= until:
                            if proc.poll() is None:
                                proc.terminate()
                            proc.wait(timeout=5)
                            raise PiError("supervisor failed to start; run worker serve for diagnostics")
                        time.sleep(.05)
            print(json.dumps(result, indent=2))
            return 0
        data = dict(action=args.action, id=args.id)
        if args.action == "spawn":
            data.update(cwd=str(Path(args.cwd).resolve()), settings=asdict(sapper.resolve_execution(args)))
        elif args.action == "message":
            data.update(message=args.message or "", intent=args.intent)
        result = request(runtime, data)
        if args.action == "down":
            until = time.monotonic() + 5 + 4 * result["cap"]
            while (runtime / "rpc.sock").exists():
                if time.monotonic() >= until:
                    raise PiError("supervisor shutdown did not complete within its cleanup budget")
                time.sleep(.05)
        print(json.dumps(result, indent=2))
        return 0
    except (PiError, OSError, ValueError) as exc:
        raise sapper.FGError(str(exc)) from exc
