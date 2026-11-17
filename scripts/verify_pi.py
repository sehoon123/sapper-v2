#!/usr/bin/env python3
"""Exercise installed Pi and Sapper against a deterministic localhost provider."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from pi_rpc import ExecutionSettings, PiSession
from pi_workers import pid_exists


class Provider(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        assert self.path == "/v1/chat/completions", self.path
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(payload)
        messages = payload["messages"]
        users = [m for m in messages if m["role"] == "user"]
        # Pi appends graph context as a user message after the actual business prompt.
        prompt = next((json.dumps(m["content"], ensure_ascii=False) for m in reversed(users)
                       if "<sapper-graph-data>" not in json.dumps(m["content"])), "")
        tools = [m for m in messages if m["role"] == "tool"]
        tool = None
        if "VERIFY_DENY" in prompt or "VERIFY_ALLOW" in prompt:
            marker = "denied.txt" if "VERIFY_DENY" in prompt else "allowed.txt"
            if not tools:
                tool = dict(id="call_local", type="function", function=dict(name="write",
                    arguments=json.dumps(dict(path=marker, content="benign fixture"))))
                text = ""
            else:
                text = "DENIED" if "denied" in json.dumps(tools[-1]).lower() else "WROTE"
        elif "VERIFY_GRAPH_CLI" in prompt:
            if not tools:
                target = re.search(r"\bi[0-9]+\b", prompt).group()
                tool = dict(id="call_graph", type="function", function=dict(name="bash",
                    arguments=json.dumps(dict(command=f"sapper claim {target} && sapper done {target} --fact 'Local CLI observation'"))))
                text = ""
            else:
                text = "GRAPH_CLI_DONE" if "claimed" in json.dumps(tools[-1]) and "done" in json.dumps(tools[-1]) else "GRAPH_CLI_FAILED"
        elif "VERIFY_CLI" in prompt:
            if not tools:
                tool = dict(id="call_cli", type="function", function=dict(name="bash",
                    arguments=json.dumps(dict(command="sapper --version"))))
                text = ""
            else:
                text = "CLI_FOUND" if "sapper 1.0.0" in json.dumps(tools[-1]) else "CLI_MISSING"
        elif "VERIFY_CANCEL" in prompt:
            tool = dict(id="call_cancel", type="function", function=dict(name="bash",
                arguments=json.dumps(dict(command="python3 -c 'import os,time; open(\"helper.pid\",\"w\").write(str(os.getpid())); time.sleep(60)'"))))
            text = ""
        elif "VERIFY_HOLD" in prompt:
            time.sleep(2)
            text = "HELD_RESULT"
        elif "performing a **brainstorm**" in prompt:
            text = json.dumps(dict(accepted=True, data=dict(options=[dict(title="Local option",
                description="Inspect a benign local file", **{"from": ["f001"]})])))
        elif "performing a **blindspot analysis**" in prompt:
            text = json.dumps(dict(accepted=True, data=dict(blindspots=[dict(area="local",
                risk="low", question="Is the local fixture deterministic?", suggestion="Run the check")])))
        elif "You are the verifier" in prompt:
            text = json.dumps(dict(accepted=True, data=dict(verified=True, issues="")))
        elif "You are an executor" in prompt:
            text = json.dumps(dict(accepted=True, data=dict(title="Local result", description="Observed the deterministic localhost fixture", kind="note")))
        elif "You are the commander" in prompt:
            text = json.dumps(dict(accepted=True, data={}))
        elif "VERIFY_CONTEXT" in prompt:
            text = "CONTEXT_OK" if "LOCAL_GRAPH_SENTINEL" in json.dumps(messages) else "CONTEXT_MISSING"
        else:
            text = "LOCAL_FINAL 한글\u2028line\u2029paragraph"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        delta = {"role": "assistant", "tool_calls": [{"index": 0, **tool}]} if tool else {"role": "assistant", "content": text}
        records = [dict(id="local", object="chat.completion.chunk", created=1, model="local",
            choices=[dict(index=0, delta=delta, finish_reason=None)]),
            dict(id="local", object="chat.completion.chunk", created=1, model="local",
            choices=[dict(index=0, delta={}, finish_reason="tool_calls" if tool else "stop")],
            usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120))]
        try:
            for record in records:
                self.wfile.write(("data: " + json.dumps(record, ensure_ascii=False) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def wait_for(predicate, label, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(f"timed out waiting for {label}")


def verify(executable):
    Provider.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="sp-real-", dir="/tmp") as temp:
            base = Path(temp).resolve()
            cwd = base / "project with spaces"
            cwd.mkdir()
            config = base / "home/.pi/agent"
            config.mkdir(parents=True)
            (config / "models.json").write_text(json.dumps(dict(providers={"fixture": dict(
                baseUrl=f"http://127.0.0.1:{server.server_port}/v1", api="openai-completions",
                apiKey="local-test-only", models=[dict(id="local", reasoning=False, contextWindow=32000, maxTokens=4096)])})))
            (config / "settings.json").write_text(json.dumps(dict(defaultProvider="fixture", defaultModel="local",
                retry={"enabled": False}, compaction={"enabled": False})))
            env = dict(PATH=os.environ["PATH"], HOME=str(base / "home"),
                PI_CODING_AGENT_DIR=str(config), PI_OFFLINE="1", PI_SKIP_VERSION_CHECK="1",
                PI_TELEMETRY="0", PYTHONDONTWRITEBYTECODE="1", LANG="en_US.UTF-8")
            with patch.dict(os.environ, env, clear=True):
                version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True).stdout.strip()
                assert version == "1.4.0", f"This check targets Pi 1.4.0, found {version}"

                def cli(*args, ok=True):
                    proc = subprocess.run([sys.executable, str(ROOT / "bin/sapper"), *args],
                        cwd=cwd, env=os.environ.copy(), capture_output=True, text=True, timeout=45)
                    assert (proc.returncode == 0) == ok, (args, proc.returncode, proc.stdout, proc.stderr)
                    return proc.stdout

                def worker(*args, ok=True):
                    raw = cli("worker", *args, ok=ok)
                    return json.loads(raw) if ok else raw

                (cwd / ".pi").mkdir()
                (cwd / ".pi/settings.json").write_text('{"quietStartup":true}')
                (cwd / ".claude").mkdir()
                (cwd / ".claude/settings.json").write_text('{"unrelated":true}')
                (cwd / "AGENTS.md").write_text("Unrelated project instructions")
                cli("setup", "--backend", "pi", "--goal", "LOCAL_GRAPH_SENTINEL")
                cli("setup", "--backend", "pi", "--goal", "LOCAL_GRAPH_SENTINEL")
                cli("fact", "benign seed")
                settings = ExecutionSettings("pi", "local", "fixture", "readonly", executable)
                session = PiSession(settings, cwd, base / "readonly")
                try:
                    assert session.run("VERIFY_CONTEXT", 15)["result"] == "CONTEXT_OK"
                    start = len(Provider.requests)
                    assert session.run("VERIFY_DENY", 15)["result"] == "DENIED"
                    assert not (cwd / "denied.txt").exists()
                    assert len(Provider.requests) >= start + 2, "real tool call did not reach another provider turn"
                    events = session.request("get_messages")["messages"]
                    assert any(m.get("role") == "toolResult" and m.get("isError") for m in events)
                    assert Path(session.session_file).is_file()
                    readonly_session = session.session_file
                finally:
                    session.close()
                assert session.proc.poll() == 0
                assert "LOCAL_GRAPH_SENTINEL" in Path(readonly_session).read_text()
                print("PASS real Pi graph context, readonly denial, final settlement, session persistence, EOF cleanup")

                autonomous = ExecutionSettings("pi", "local", "fixture", "autonomous", executable)
                session = PiSession(autonomous, cwd, base / "autonomous")
                try:
                    assert session.run("VERIFY_ALLOW", 15)["result"] == "WROTE"
                    assert (cwd / "allowed.txt").read_text() == "benign fixture"
                finally:
                    session.close()
                session = PiSession(autonomous, cwd, base / "cli-discovery")
                try:
                    assert session.run("VERIFY_CLI", 15)["result"] == "CLI_FOUND"
                finally:
                    session.close()
                session = PiSession(autonomous, cwd, base / "cancellation")
                try:
                    session.begin("VERIFY_CANCEL", 15)
                    helper_file = cwd / "helper.pid"
                    helper_pid = int(wait_for(lambda: helper_file.read_text().strip() if helper_file.exists() else None,
                        "real Pi shell helper startup"))
                    assert pid_exists(helper_pid)
                finally:
                    session.close()
                wait_for(lambda: not pid_exists(helper_pid), "real Pi aborted shell helper cleanup")
                assert session.proc.poll() == 0
                print("PASS explicit autonomous tool effects, bundled CLI discovery, and running shell cancellation cleanup")

                opts = ["--backend", "pi", "--pi-bin", executable, "--model", "fixture/local", "--timeout", "20"]
                assert "reason" in cli("dispatch", "reason", *opts)
                assert "reason" in cli("dispatch", "reason", "--backend", "pi", "--pi-bin", executable, "--timeout", "20")
                assert "blindspot" in cli("dispatch", "blindspot", *opts)
                assert "brainstorm" in cli("dispatch", "brainstorm", *opts)
                iid = cli("intent", "--from", "f001", "Observe the local deterministic fixture").strip()
                assert "explore" in cli("dispatch", iid, "--claim", *opts)
                assert "VERIFIED" in cli("dispatch", "verify", "--intent", iid, *opts)
                cli("auto", "--max-steps", "1", *opts)
                cli("dispatch", "reason", "--backend", "pi", "--pi-bin", executable, "--model", "no-such-local-model", ok=False)
                print("PASS public Pi reason/explore/verify/blindspot/brainstorm/auto and unknown-model refusal")

                package = subprocess.run([executable, "--mode", "rpc", "--offline", "--no-approve",
                    "--no-extensions", "--no-skills", "--no-mcp", "--no-context-files", "--no-themes",
                    "--no-prompt-templates", "--no-session", "--session-dir", str(base / "package"), "-e", str(ROOT)],
                    input='{"type":"get_commands","id":"commands"}\n', capture_output=True, text=True,
                    cwd=cwd, timeout=20)
                assert package.returncode == 0, package.stderr
                records = [json.loads(line) for line in package.stdout.split("\n") if line]
                commands = next(r["data"]["commands"] for r in records if r.get("id") == "commands")
                assert any(c["name"] == "sapper-policy" for c in commands), commands
                assert any(c["name"] == "skill:sapper-setup" for c in commands), commands
                project_package = subprocess.run([executable, "--mode", "rpc", "--offline", "--approve",
                    "--no-mcp", "--no-context-files", "--no-themes", "--no-prompt-templates", "--no-session",
                    "--session-dir", str(base / "project-package")],
                    input='{"type":"get_commands","id":"commands"}\n', capture_output=True, text=True,
                    cwd=cwd, timeout=20)
                assert project_package.returncode == 0, project_package.stderr
                project_records = [json.loads(line) for line in project_package.stdout.split("\n") if line]
                project_commands = next(r["data"]["commands"] for r in project_records if r.get("id") == "commands")
                assert any(c["name"] == "sapper-policy" for c in project_commands)
                assert any(c["name"] == "skill:sapper-setup" for c in project_commands)
                print("PASS actual Pi explicit and project package extension and skill loading")

                runtime = cwd / ".sapper-runtime"
                pids = []
                server_proc = None
                worker("up", "--max-workers", "2", "--seconds", "180")
                try:
                    first = worker("spawn", "--pi-bin", executable, "--model", "fixture/local")
                    second = worker("spawn", "--pi-bin", executable, "--model", "fixture/local", "--pi-policy", "autonomous")
                    pids.extend([first["pid"], second["pid"]])
                    assert first["id"] != second["id"]
                    worker("spawn", "--pi-bin", executable, ok=False)
                    worker("message", first["id"], "WORKER_FIRST")
                    def completed(wid):
                        record = worker("peek", wid)
                        assert record["status"] not in ("failed", "dead"), record
                        return record if record["status"] == "completed" else None
                    done = wait_for(lambda: completed(first["id"]), "worker final result")
                    assert done["result"] == "LOCAL_FINAL 한글\u2028line\u2029paragraph"
                    assert Path(done["session_file"]).is_file()
                    worker("resume", first["id"], ok=False)
                    worker("stop", first["id"])
                    resumed = worker("resume", first["id"])
                    pids.append(resumed["pid"])
                    assert resumed["session_file"] == done["session_file"]
                    assert resumed["generation"] != done["generation"]
                    start = len(Provider.requests)
                    worker("message", first["id"], "WORKER_SECOND")
                    wait_for(lambda: completed(first["id"]), "resumed worker result")
                    assert any("WORKER_FIRST" in json.dumps(p["messages"]) and "WORKER_SECOND" in json.dumps(p["messages"])
                               for p in Provider.requests[start:]), "exact-session conversation was not restored"
                    for record in (first, second):
                        target = cli("intent", "--from", "f001", "Observe local fixture with durable owner").strip()
                        worker("message", record["id"], "--intent", target)
                        wait_for(lambda: completed(record["id"]), "owned intent result")
                        intent = json.loads((cwd / ".sapper/intents" / f"{target}.json").read_text())
                        assert intent["worker"] == record["id"] and intent["to"]
                    cli_target = cli("intent", "--from", "f001", "Record benign model-issued CLI ownership").strip()
                    worker("message", second["id"], f"VERIFY_GRAPH_CLI {cli_target}")
                    cli_done = wait_for(lambda: completed(second["id"]), "model-issued claim and done")
                    assert cli_done["result"] == "GRAPH_CLI_DONE", cli_done
                    cli_intent = json.loads((cwd / ".sapper/intents" / f"{cli_target}.json").read_text())
                    assert cli_intent["worker"] == second["id"] and cli_intent["to"]
                    before = worker("list")
                    assert len(worker("board")["workers"]) == 2
                    worker("down")
                    assert not (runtime / "rpc.sock").exists()
                    assert all(not pid_exists(pid) for pid in pids), "Pi child survived supervisor shutdown"
                    worker("up", "--seconds", "999", "--max-workers", "9")
                    after = worker("list")
                    assert after["deadline"] == before["deadline"] and after["cap"] == 2
                    restart = worker("resume", first["id"])
                    pids.append(restart["pid"])
                    worker("down")
                    print("PASS public workers, same-model graph ownership, cap, resume, persisted deadline, up/down cleanup")

                    server_proc = subprocess.Popen([sys.executable, str(ROOT / "bin/sapper"), "worker", "serve"],
                        cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    wait_for(lambda: (runtime / "rpc.sock").exists(), "foreground supervisor")
                    restored = worker("resume", first["id"])
                    pids.append(restored["pid"])
                    worker("message", first["id"], "VERIFY_HOLD")
                    server_proc.kill()
                    server_proc.wait(timeout=5)
                    wait_for(lambda: not pid_exists(restored["pid"]), "orphan Pi EOF shutdown")
                    worker("up")
                    interrupted = worker("peek", first["id"])
                    assert interrupted["status"] == "interrupted"
                    count = len(Provider.requests)
                    final = worker("resume", first["id"])
                    pids.append(final["pid"])
                    assert final["session_file"] == restored["session_file"]
                    assert final["mission"] is None and final["status"] == "idle"
                    assert len(Provider.requests) == count, "interrupted mission was replayed"
                    print("PASS supervisor crash/restart, prior-child exit, interrupted status, exact resume without replay")
                finally:
                    if server_proc is not None:
                        if server_proc.poll() is None:
                            server_proc.terminate()
                            server_proc.wait(timeout=10)
                        server_proc.stderr.close()
                    if (runtime / "rpc.sock").exists():
                        worker("down")
                assert all(not pid_exists(pid) for pid in pids), "owned child process remains"
                for _ in range(2):
                    cli("teardown", "--backend", "pi")
                assert (cwd / ".sapper/project.json").is_file()
                assert json.loads((cwd / ".pi/settings.json").read_text()) == {"quietStartup": True}
                assert (cwd / ".claude/settings.json").read_text() == '{"unrelated":true}'
                assert (cwd / "AGENTS.md").read_text() == "Unrelated project instructions"
                assert not (runtime / "rpc.sock").exists()
                assert len(Provider.requests) > 15
                print(f"PASS cleanup and repeatable teardown; {len(Provider.requests)} localhost provider requests, no external models")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi-bin", default=os.environ.get("SAPPER_PI_BIN", "pi"))
    args = parser.parse_args()
    executable = shutil.which(args.pi_bin)
    if not executable:
        parser.error("installed Pi executable not found")
    verify(executable)


if __name__ == "__main__":
    main()
