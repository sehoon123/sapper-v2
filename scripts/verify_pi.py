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
from pi_rpc import ExecutionSettings, PiError, PiSession
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
        prompt_index = next((i for i in range(len(messages) - 1, -1, -1)
                             if messages[i]["role"] == "user" and
                             "<sapper-graph-data>" not in json.dumps(messages[i]["content"])), -1)
        prompt = json.dumps(messages[prompt_index]["content"], ensure_ascii=False)
        tools = [m for m in messages[prompt_index + 1:] if m["role"] == "tool"]
        tool = None
        if "PENTEST_EXECUTOR_FIXTURE" in prompt and "You are an executor" in prompt:
            text = json.dumps(dict(accepted=True, data=dict(title="Bounded defense",
                description="Synthetic tenant boundary enforced", kind="assessment",
                doc="PENTEST_EVIDENCE_SNAPSHOT: authenticated owner receives object; reader in another tenant receives no object; enforcement log rejects before read; no state changed.",
                data=dict(outcome="validated_defense", asset="fixture /objects/1",
                    conditions="build 1; local fixture time; authenticated owner A tenant X and reader B tenant Y",
                    limitations="Only this synthetic object and these identities",
                    method="Compare owner and cross-tenant reads", expected="Only owner reads object",
                    observed="Owner receives object, cross-tenant reader gets no object; enforcement log confirms boundary",
                    comparison="Owner succeeds before and after reader request",
                    evidence="PENTEST_EVIDENCE_SNAPSHOT in doc", basis="dynamic"))))
        elif "VERIFY_NATIVE_" in prompt and ("You are" not in prompt or "You are an executor" in prompt):
            tag = re.search(r"VERIFY_NATIVE_(DIRECT|DISPATCH|WORKER)", prompt).group(1).lower()
            steps = [
                ("bash", dict(command=f"printf shell-ok > {tag}-shell.txt && sapper --version")),
                ("write", dict(path=f"{tag}-write.txt", content="before edit")),
                ("edit", dict(path=f"{tag}-write.txt", oldText="before edit", newText="after edit")),
                ("fixture_tool", {}),
            ]
            if len(tools) < len(steps):
                name, arguments = steps[len(tools)]
                tool = dict(id=f"call_native_{len(tools)}", type="function",
                            function=dict(name=name, arguments=json.dumps(arguments)))
                text = ""
            else:
                text = json.dumps(dict(accepted=True, data=dict(title="Native result",
                    description="Observed native shell, write, edit and custom tool effects", kind="note")))
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
            caveat = "PENTEST_EXECUTOR_FIXTURE auto check" in prompt
            text = json.dumps(dict(accepted=True, data=dict(verified=not caveat,
                issues="PENTEST_VERIFICATION_CAVEAT: baseline identity needs operator review" if caveat else "")))
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
                # Isolation and native trust are fixture configuration, never launcher overrides.
                (config / "trust.json").write_text(json.dumps({str(cwd): True}))
                (config / "AGENTS.md").write_text("GLOBAL_CONTEXT_SENTINEL")
                (cwd / ".pi/APPEND_SYSTEM.md").write_text("NATIVE_APPEND_SENTINEL")
                (config / "extensions").mkdir()
                (config / "extensions/fixture.ts").write_text("""
import { Type } from "@earendil-works/pi-ai";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
export default function(pi) {
  pi.registerTool({name: "fixture_tool", label: "Fixture", description: "Benign fixture tool",
    parameters: Type.Object({}),
    async execute(_id, _params, _signal, _update, ctx) {
      writeFileSync(join(ctx.cwd, "custom-tool.txt"), "custom-ok");
      return {content: [{type: "text", text: "CUSTOM_TOOL_OK"}], details: {}};
    }});
  pi.on("before_agent_start", async (event, ctx) => {
    if (event.prompt?.includes("VERIFY_DIALOG")) await ctx.ui.confirm("Fixture permission", "Allow?");
  });
}
""")
                (cwd / ".pi/extensions").mkdir()
                (cwd / ".pi/extensions/project.ts").write_text("""
export default function(pi) {
  pi.on("before_agent_start", event => {
    event.systemPromptOptions.appendSystemPrompt += "\\nPROJECT_EXTENSION_SENTINEL";
  });
}
""")
                (config / "skills/fixture").mkdir(parents=True)
                (config / "skills/fixture/SKILL.md").write_text(
                    "---\nname: fixture\ndescription: USER_SKILL_SENTINEL\n---\nLocal fixture skill\n")
                (config / "prompts").mkdir()
                (config / "prompts/fixture.md").write_text("USER_TEMPLATE_SENTINEL")
                settings = ExecutionSettings("pi", "local", "fixture", executable)

                def native_effects(tag):
                    assert (cwd / f"{tag}-shell.txt").read_text() == "shell-ok"
                    assert (cwd / f"{tag}-write.txt").read_text() == "after edit"
                    assert (cwd / "custom-tool.txt").read_text() == "custom-ok"
                    (cwd / "custom-tool.txt").unlink()

                session = PiSession(settings, cwd, base / "native", append_system="SAPPER_APPEND_SENTINEL")
                try:
                    assert session.run("VERIFY_CONTEXT", 15)["result"] == "CONTEXT_OK"
                    assert sum("<sapper-graph-data>" in json.dumps(m) for m in Provider.requests[-1]["messages"]) == 1
                    start = len(Provider.requests)
                    session.run("VERIFY_NATIVE_DIRECT", 15)
                    native_effects("direct")
                    assert len(Provider.requests) == start + 5
                    messages = Provider.requests[start]["messages"]
                    for sentinel in ("GLOBAL_CONTEXT_SENTINEL", "Unrelated project instructions",
                                     "NATIVE_APPEND_SENTINEL", "SAPPER_APPEND_SENTINEL",
                                     "PROJECT_EXTENSION_SENTINEL", "USER_SKILL_SENTINEL"):
                        assert sentinel in json.dumps(messages), sentinel
                    commands = session.request("get_commands")["commands"]
                    names = {c["name"] for c in commands}
                    assert {"skill:sapper-setup", "skill:fixture", "fixture"} <= names, names
                    events = session.request("get_messages")["messages"]
                    assert any(m.get("role") == "toolResult" and "sapper 1.0.0" in json.dumps(m) for m in events)
                    assert Path(session.session_file).is_file()
                    native_session = session.session_file
                finally:
                    session.close()
                assert session.proc.poll() == 0
                assert "LOCAL_GRAPH_SENTINEL" in Path(native_session).read_text()
                print("PASS default bash/write/edit/custom tool effects, native context/append/skills/templates, graph deduplication, settlement, persistence")

                original_settings = (config / "settings.json").read_text()
                selected = json.loads(original_settings)
                selected["defaultTools"] = ["read"]
                (config / "settings.json").write_text(json.dumps(selected))
                session = PiSession(settings, cwd, base / "user-tools")
                try:
                    session.run("VERIFY_TOOL_SELECTION", 15)
                    names = {t["function"]["name"] for t in Provider.requests[-1]["tools"]}
                    assert "read" in names and not names.intersection({"bash", "edit", "write"}), names
                finally:
                    session.close()
                    (config / "settings.json").write_text(original_settings)
                (config / "trust.json").write_text(json.dumps({str(cwd): False}))
                session = PiSession(settings, cwd, base / "untrusted-project")
                try:
                    session.run("VERIFY_PROJECT_TRUST", 15)
                    assert "PROJECT_EXTENSION_SENTINEL" not in json.dumps(Provider.requests[-1]["messages"])
                    assert "GLOBAL_CONTEXT_SENTINEL" in json.dumps(Provider.requests[-1]["messages"])
                finally:
                    session.close()
                    (config / "trust.json").write_text(json.dumps({str(cwd): True}))
                session = PiSession(settings, cwd, base / "dialog")
                try:
                    try:
                        session.run("VERIFY_DIALOG", 15)
                        raise AssertionError("native confirmation silently accepted")
                    except PiError as exc:
                        assert "confirm" in str(exc) and "unsupported" in str(exc), exc
                finally:
                    session.close()
                print("PASS user defaultTools, native saved distrust, and visible unsupported extension dialog")

                session = PiSession(settings, cwd, base / "cli-discovery")
                try:
                    assert session.run("VERIFY_CLI", 15)["result"] == "CLI_FOUND"
                finally:
                    session.close()
                session = PiSession(settings, cwd, base / "cancellation")
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
                print("PASS bundled CLI discovery and running shell cancellation cleanup")

                opts = ["--backend", "pi", "--pi-bin", executable, "--model", "fixture/local", "--timeout", "20"]
                assert "reason" in cli("dispatch", "reason", *opts)
                assert "reason" in cli("dispatch", "reason", "--backend", "pi", "--pi-bin", executable, "--timeout", "20")
                assert "blindspot" in cli("dispatch", "blindspot", *opts)
                assert "brainstorm" in cli("dispatch", "brainstorm", *opts)
                iid = cli("intent", "--from", "f001", "VERIFY_NATIVE_DISPATCH Observe the local deterministic fixture").strip()
                assert "explore" in cli("dispatch", iid, "--claim", "--skip-permissions", *opts)
                native_effects("dispatch")
                assert "NATIVE_APPEND_SENTINEL" in json.dumps(Provider.requests[-1]["messages"])
                assert "VERIFIED" in cli("dispatch", "verify", "--intent", iid, *opts)
                cli("auto", "--max-steps", "1", *opts)
                cli("dispatch", "reason", "--backend", "pi", "--pi-bin", executable, "--model", "no-such-local-model", ok=False)
                print("PASS public Pi reason/explore/verify/blindspot/brainstorm/auto and unknown-model refusal")

                package = subprocess.run([executable, "--mode", "rpc", "--no-session",
                    "--session-dir", str(base / "package"), "-e", str(ROOT)],
                    input='{"type":"get_commands","id":"commands"}\n', capture_output=True, text=True,
                    cwd=cwd, timeout=20)
                assert package.returncode == 0, package.stderr
                records = [json.loads(line) for line in package.stdout.split("\n") if line]
                commands = next(r["data"]["commands"] for r in records if r.get("id") == "commands")
                assert any(c["name"] == "skill:sapper-setup" for c in commands), commands
                print("PASS actual Pi explicit and project package skill loading")

                runtime = cwd / ".sapper-runtime"
                pids = []
                server_proc = None
                worker("up", "--max-workers", "2", "--seconds", "180")
                try:
                    first = worker("spawn", "--pi-bin", executable, "--model", "fixture/local")
                    second = worker("spawn", "--pi-bin", executable, "--model", "fixture/local")
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
                    worker("message", second["id"], "VERIFY_NATIVE_WORKER")
                    wait_for(lambda: completed(second["id"]), "native worker effects")
                    native_effects("worker")
                    before = worker("list")
                    assert len(worker("board")["workers"]) == 2
                    worker("down")
                    assert not (runtime / "rpc.sock").exists()
                    assert all(not pid_exists(pid) for pid in pids), "Pi child survived supervisor shutdown"
                    registry_path = runtime / "workers.json"
                    registry = json.loads(registry_path.read_text())
                    registry["workers"][first["id"]]["settings"]["policy"] = "readonly"
                    registry_path.write_text(json.dumps(registry))
                    worker("up", "--seconds", "999", "--max-workers", "9")
                    assert "policy" not in json.loads(registry_path.read_text())["workers"][first["id"]]["settings"]
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
                context = dict(objective="PENTEST_CONTEXT_SENTINEL bounded local assessment",
                    scope="Synthetic API /objects/1 and org.example.app only",
                    exclusions="All external services", environment="fixture build 1; no mobile device",
                    window="Local verification run", identities="owner A tenant X; reader B tenant Y",
                    access="Synthetic authenticated sessions", methods="Local deterministic fixture only",
                    constraints="No live target traffic; redact all evidence")
                (cwd / "engagement.json").write_text(json.dumps(context))
                cli("setup", "--backend", "pi", "--profile", "pentest", "--engagement", "engagement.json")
                project = json.loads((cwd / ".sapper/project.json").read_text())
                engagement = project["engagement"]
                cli("setup", "--backend", "pi", "--profile", "pentest", "--engagement", "engagement.json")
                assert json.loads((cwd / ".sapper/project.json").read_text()) == project

                def assessment_request():
                    messages = Provider.requests[-1]["messages"]
                    system = "\n".join(json.dumps(m["content"]) for m in messages if m["role"] == "system")
                    assert "Sapper pentest mode" in system, system
                    assert "NATIVE_APPEND_SENTINEL" in system
                    transmitted = json.dumps(messages)
                    assert "PENTEST_CONTEXT_SENTINEL" in transmitted
                    for unwanted in ("Capture flags immediately", "harvest credentials/loot", "flag submitted", "arena challenges"):
                        assert unwanted not in transmitted, unwanted
                    names = {t["function"]["name"] for t in Provider.requests[-1]["tools"]}
                    assert {"bash", "write", "edit", "fixture_tool"} <= names, names

                native = subprocess.run([executable, "--print", "--no-session", "--model", "fixture/local",
                    "PENTEST_NATIVE_CONTEXT"], cwd=cwd, capture_output=True, text=True, timeout=30)
                assert native.returncode == 0, native.stderr
                assessment_request()
                for role in ("reason", "blindspot", "brainstorm"):
                    cli("dispatch", role, *opts)
                    assessment_request()
                target = cli("intent", "--from", engagement, "PENTEST_EXECUTOR_FIXTURE check tenant boundary").strip()
                cli("dispatch", target, "--claim", *opts)
                assessment_request()
                cli("dispatch", "verify", "--intent", target, *opts)
                assessment_request()
                assert "PENTEST_EVIDENCE_SNAPSHOT" in json.dumps(Provider.requests[-1]["messages"])
                custom = cwd / "custom-templates/pentest"
                custom.mkdir(parents=True)
                (custom / "verify.md").write_text("You are the verifier. Review {intent_description} against {result_description}.")
                cli("dispatch", "verify", "--intent", target, "--templates", str(custom.parent), *opts)
                assessment_request()
                assert "PENTEST_EVIDENCE_SNAPSHOT" in json.dumps(Provider.requests[-1]["messages"])
                auto_target = cli("intent", "--from", engagement, "PENTEST_EXECUTOR_FIXTURE auto check").strip()
                cli("claim", auto_target, "--worker", "pi-fixture-auto")
                with patch.dict(os.environ, SAPPER_WORKER_ID="pi-fixture-auto"):
                    cli("auto", "--max-steps", "1", "--max-explore", "1", "--verify", "--templates", str(custom.parent), *opts)
                assessment_request()
                assert "PENTEST_EVIDENCE_SNAPSHOT" in json.dumps(Provider.requests[-1]["messages"])
                assert json.loads((cwd / ".sapper/intents" / f"{auto_target}.json").read_text())["to"]
                worker("up")
                try:
                    managed = worker("spawn", "--pi-bin", executable, "--model", "fixture/local")
                    worker("message", managed["id"], "PENTEST_FREE_TEXT_CONTEXT")
                    wait_for(lambda: completed(managed["id"]), "assessment free-text worker")
                    assessment_request()
                    worker_target = cli("intent", "--from", engagement, "PENTEST_EXECUTOR_FIXTURE worker check").strip()
                    worker("message", managed["id"], "--intent", worker_target)
                    wait_for(lambda: completed(managed["id"]), "assessment intent worker")
                    assessment_request()
                    assert json.loads((cwd / ".sapper/intents" / f"{worker_target}.json").read_text())["worker"] == managed["id"]
                finally:
                    worker("down")
                gap = cli("intent", "--from", engagement, "Mobile runtime prerequisites").strip()
                cli("done", gap, "--fact", "No mobile device", "-k", "assessment", "-d", "outcome=blocked",
                    "-d", "asset=org.example.app", "-d", "conditions=no device/build supplied",
                    "-d", "limitations=no dynamic coverage", "-d", "reason=missing authorized build/device",
                    "-d", "next_step=obtain prerequisites and retest")
                cli("intent", "--from", engagement, "Unexecuted business workflow")
                cli("validate")
                report = json.loads(cli("report", "--format", "json"))
                assert report["outcome_counts"]["vulnerability"] == 0
                assert report["outcome_counts"]["validated_defense"] == 3
                assert report["outcome_counts"]["blocked"] == 1
                assert report["outcome_counts"]["not_tested"] >= 1
                assert "PENTEST_EVIDENCE_SNAPSHOT" in json.dumps(report)
                assert any(h["creator"] == "blindspot" for h in report["advisories"])
                assert any(h["creator"] == "verify" and "PENTEST_VERIFICATION_CAVEAT" in h["content"]
                           for h in report["advisories"])
                text_report = cli("report")
                assert "vulnerability: 0" in text_report
                assert "PENTEST_VERIFICATION_CAVEAT" in text_report
                print("PASS pentest native Pi, all dispatch roles, auto, free-text/intent workers, complete verifier evidence and zero-vulnerability report")
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
