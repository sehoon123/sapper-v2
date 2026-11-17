from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid


class PiError(RuntimeError):
    pass


class Lifecycle(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPING = "stopping"
    DEAD = "dead"


@dataclass(frozen=True)
class ExecutionSettings:
    backend: str = "claude"
    model: str | None = None
    provider: str | None = None
    executable: str = "pi"


EXTENSION = Path(__file__).resolve().parents[1] / "pi" / "extension.ts"
MAX_RECORD = 4 * 1024 * 1024


def private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise PiError(f"runtime directory cannot be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PiError(f"runtime directory must be owned by you and mode 0700: {path}")
    return path.resolve()


class PiSession:
    def __init__(self, settings: ExecutionSettings, cwd: Path, session_dir: Path,
                 *, session_file: str | None = None, worker_id: str | None = None,
                 store_path: Path | None = None, append_system: str | None = None, timeout: float = 30,
                 on_spawn=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise PiError("Pi timeout must be positive and finite")
        if not EXTENSION.is_file():
            raise PiError("required Sapper Pi extension is missing")
        self.generation = uuid.uuid4().hex
        self.state = Lifecycle.STARTING
        self.responses = {}
        self.pending = set()
        self.buffer = bytearray()
        self.final = None
        self.result = None
        self.error = None
        self.settled = None
        self.deadline = time.monotonic() + timeout
        session_dir = private_dir(session_dir)
        argv = [settings.executable, "--mode", "rpc", "-e", str(EXTENSION),
                "--session-dir", str(session_dir)]
        if session_file:
            if not Path(session_file).is_file():
                raise PiError("recorded exact session file is missing")
            argv += ["--session", str(Path(session_file).resolve())]
        env = {**os.environ,
               "SAPPER_STORE": str((store_path or cwd / ".sapper").resolve()),
               "SAPPER_PI_APPEND_SYSTEM": append_system or ""}
        if worker_id:
            env["SAPPER_WORKER_ID"] = worker_id
        else:
            env.pop("SAPPER_WORKER_ID", None)
        self.selector = selectors.DefaultSelector()
        try:
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
        except (OSError, ValueError, TypeError) as exc:
            self.selector.close()
            raise PiError(f"Pi startup failed: {exc}") from exc
        try:
            if on_spawn:
                on_spawn(self.proc.pid)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                os.set_blocking(stream.fileno(), False)
            self.selector.register(self.proc.stdout, selectors.EVENT_READ, "stdout")
            self.selector.register(self.proc.stderr, selectors.EVENT_READ, "stderr")
            if settings.model:
                models = self.request("get_available_models").get("models", [])
                if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
                    raise PiError("Pi returned invalid models")
                matches = [m for m in models if (m.get("id") == settings.model or
                    f"{m.get('provider')}/{m.get('id')}" == settings.model) and
                    (not settings.provider or m.get("provider") == settings.provider)]
                if len(matches) != 1:
                    raise PiError("Pi model must match exactly one available provider/model; no fallback")
                self.request("set_model", provider=matches[0]["provider"], modelId=matches[0]["id"])
            elif settings.provider:
                raise PiError("--provider requires --model")
            state = self.request("get_state")
            self.model = state.get("model")
            if not isinstance(self.model, dict) or not all(isinstance(self.model.get(k), str) and self.model[k] for k in ("id", "provider")):
                raise PiError("Pi has no selected model")
            self.session_file = state.get("sessionFile")
            if not isinstance(self.session_file, str) or not self.session_file or Path(self.session_file).resolve().parent != session_dir:
                raise PiError("Pi session escaped its private session directory")
            if session_file and Path(self.session_file).resolve() != Path(session_file).resolve():
                raise PiError("Pi did not reopen the recorded exact session")
            self.state = Lifecycle.IDLE
        except Exception:
            self.close()
            raise

    def send(self, kind: str, **fields) -> str:
        if self.state in (Lifecycle.STOPPING, Lifecycle.DEAD):
            raise PiError("Pi input is closed")
        request_id = uuid.uuid4().hex
        self.pending.add(request_id)
        self._write(dict(type=kind, id=request_id, **fields))
        return request_id

    def _write(self, record):
        data = memoryview((json.dumps(record, ensure_ascii=False) + "\n").encode())
        if len(data) > MAX_RECORD:
            raise PiError("Pi command exceeds record limit")
        while data:
            if time.monotonic() >= self.deadline:
                raise PiError("Pi input deadline exceeded")
            try:
                count = os.write(self.proc.stdin.fileno(), data)
                data = data[count:]
            except BlockingIOError:
                self.pump(.01)
            except (BrokenPipeError, OSError) as exc:
                raise PiError("Pi exited while writing input") from exc

    def wait_response(self, request_id):
        while request_id not in self.responses:
            self.pump(.05)
            self._check_deadline()
        reply = self.responses.pop(request_id)
        self.pending.discard(request_id)
        if not reply.get("success"):
            raise PiError(f"Pi {reply.get('command')} rejected the request")
        data = reply.get("data") or {}
        if not isinstance(data, dict):
            raise PiError("Pi returned invalid response data")
        return data

    def request(self, kind, **fields):
        return self.wait_response(self.send(kind, **fields))

    def _check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise PiError("Pi deadline exceeded")
        if self.proc.poll() is not None:
            raise PiError(f"Pi exited {self.proc.returncode}")

    def pump(self, wait=0):
        for key, _ in self.selector.select(wait):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                if key.data == "stdout" and self.buffer:
                    raise PiError("Pi exited with an incomplete protocol record")
                continue
            if key.data == "stderr":
                continue
            self.buffer.extend(chunk)
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if len(line) > MAX_RECORD:
                    raise PiError("Pi record exceeds limit")
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise PiError("Pi emitted malformed JSONL") from exc
                if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                    raise PiError("Pi emitted an invalid protocol record")
                self._record(record)
            if len(self.buffer) > MAX_RECORD:
                raise PiError("Pi record exceeds limit")

    def _record(self, record):
        kind = record["type"]
        if kind == "response":
            rid = record.get("id")
            if not isinstance(rid, str) or rid not in self.pending or rid in self.responses:
                raise PiError("Pi returned an uncorrelated response")
            self.responses[rid] = record
        elif kind == "extension_error":
            raise PiError("Pi extension failed; refusing model results")
        elif kind == "extension_ui_request" and record.get("method") in ("select", "confirm", "input", "editor"):
            self._write(dict(type="extension_ui_response", id=record["id"], cancelled=True))
            raise PiError(f"Pi extension interaction {record['method']!r} is unsupported by Sapper RPC; cancelled, not approved")
        elif self.state == Lifecycle.RUNNING:
            if kind == "message_end":
                message = record.get("message")
                if not isinstance(message, dict):
                    raise PiError("Pi returned an invalid message")
                if message.get("role") == "assistant":
                    self.final = message
            elif kind == "agent_settled":
                self.settled = record

    def begin(self, prompt: str, timeout: float):
        if not math.isfinite(timeout) or timeout <= 0:
            raise PiError("Pi deadline exceeded or invalid timeout")
        if not isinstance(prompt, str) or not prompt.strip():
            raise PiError("Pi prompt cannot be empty")
        if self.state not in (Lifecycle.IDLE, Lifecycle.COMPLETED):
            raise PiError(f"Pi cannot accept a business run while {self.state.value}")
        self.deadline = time.monotonic() + timeout
        self.final = self.result = self.error = self.settled = None
        self.state = Lifecycle.RUNNING
        try:
            disposition = self.request("prompt", message=prompt).get("disposition")
            if disposition != "started":
                raise PiError(f"Pi business prompt was {disposition!r}, not started")
        except PiError as exc:
            self.state, self.error = Lifecycle.FAILED, str(exc)
            raise

    def poll_result(self):
        if self.state != Lifecycle.RUNNING:
            return self.result
        try:
            self.pump(0)
            if self.settled is None:
                self._check_deadline()
                return None
            if self.settled.get("aborted") or not self.final or self.final.get("stopReason") != "stop":
                raise PiError(f"Pi run did not finish successfully ({(self.final or {}).get('stopReason', 'incomplete')})")
            content = self.final.get("content", [])
            if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
                raise PiError("Pi returned invalid assistant content")
            text = "".join(b["text"] for b in content if b.get("type") == "text" and isinstance(b.get("text"), str))
            if not text.strip():
                raise PiError("Pi returned no final assistant text")
            self.result = {"result": text, "session_file": self.session_file}
            self.state = Lifecycle.COMPLETED
            return self.result
        except PiError as exc:
            self.state, self.error = Lifecycle.FAILED, str(exc)
            raise

    def run(self, prompt, timeout):
        self.begin(prompt, timeout)
        while self.poll_result() is None:
            self.pump(.05)
        return self.result

    def close(self):
        if self.state == Lifecycle.DEAD:
            return
        self.deadline = time.monotonic() + 1
        try:
            if self.proc.poll() is None:
                self.request("clear_queue")
                self.request("abort")
        except (PiError, OSError, ValueError):
            pass
        self.state = Lifecycle.STOPPING
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # The unreaped child owns this process group, so its PID cannot be reused.
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=2)
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()
        self.selector.close()
        self.state = Lifecycle.DEAD
