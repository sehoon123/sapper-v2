#!/usr/bin/env python3
"""Deterministic RPC peer. Scenarios are selected by FAKE_PI_SCENARIO."""
import json
import os
import sys
import time
from pathlib import Path

scenario = os.environ.get("FAKE_PI_SCENARIO", "ok")
if os.environ.get("FAKE_PI_CAPTURE"):
    Path(os.environ["FAKE_PI_CAPTURE"]).write_text(json.dumps(dict(argv=sys.argv, env=dict(os.environ))))
session = str(Path.cwd() / "fake-session.jsonl")
if "--session-dir" in sys.argv:
    session = str(Path(sys.argv[sys.argv.index("--session-dir") + 1]) / "fake-session.jsonl")
if "--session" in sys.argv:
    session = sys.argv[sys.argv.index("--session") + 1]


def emit(value):
    raw = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    if scenario == "split":
        for byte in raw:
            os.write(1, bytes([byte]))
    else:
        os.write(1, raw)


def reply(cmd, data=None):
    emit(dict(type="response", id=cmd["id"], command=cmd["type"], success=True, data=data or {}))


def assistant(text, reason="stop"):
    emit(dict(type="message_end", message=dict(role="assistant", stopReason=reason,
         content=[dict(type="text", text=text)])))


pending = None
for line in sys.stdin.buffer:
    cmd = json.loads(line)
    kind = cmd["type"]
    if kind == "get_state":
        if scenario == "startup-dialog":
            emit(dict(type="extension_ui_request", id="dialog", method=os.environ["FAKE_PI_DIALOG"]))
        reply(cmd, dict(sessionFile=session, model=dict(id="local", provider="fixture")))
    elif kind == "get_commands":
        reply(cmd, dict(commands=[]))
    elif kind == "get_available_models":
        reply(cmd, dict(models=[dict(id="local", provider="fixture")]))
    elif kind == "reverse":
        if pending:
            reply(cmd, dict(value=cmd["id"]))
            reply(pending, dict(value=pending["id"]))
            pending = None
        else:
            pending = cmd
    elif kind == "freeze":
        reply(cmd)
        time.sleep(30)
    elif kind == "prompt":
        if scenario == "invalid-utf8":
            os.write(1, b'\xff\n')
            continue
        if scenario == "oversized":
            os.write(1, b'x' * (5 * 1024 * 1024))
            continue
        if scenario == "incomplete":
            os.write(1, b'{"type":')
            sys.exit(0)
        if scenario == "invalid-message":
            reply(cmd, dict(disposition="started"))
            emit(dict(type="message_end", message=[]))
            continue
        if scenario == "exit":
            sys.exit(9)
        if scenario == "malformed":
            os.write(1, b"not-json\n")
            continue
        reply(cmd, dict(disposition=scenario if scenario in ("handled", "queued") else "started"))
        if scenario in ("handled", "queued", "timeout"):
            continue
        if scenario == "stderr":
            os.write(2, b"x" * 300000)
        if scenario == "dialog":
            emit(dict(type="extension_ui_request", id="dialog", method=os.environ.get("FAKE_PI_DIALOG", "confirm")))
            continue
        if scenario == "retry":
            assistant("stale", "error")
            emit(dict(type="agent_end", willRetry=True))
            time.sleep(.02)
        reason = scenario if scenario in ("error", "aborted", "length", "deferred", "pending", "toolUse") else "stop"
        assistant(os.environ.get("FAKE_PI_RESULT", "final 한글\u2028line\u2029paragraph"), reason)
        emit(dict(type="agent_end", willRetry=False))
        emit(dict(type="agent_settled", aborted=scenario == "aborted"))
        Path(session).parent.mkdir(parents=True, exist_ok=True)
        Path(session).write_text('{"type":"session"}\n')
    elif kind == "extension_ui_response":
        if os.environ.get("FAKE_PI_DIALOG_CAPTURE"):
            Path(os.environ["FAKE_PI_DIALOG_CAPTURE"]).write_text(json.dumps(cmd))
        assistant("cancelled" if cmd.get("cancelled") else "approved")
        emit(dict(type="agent_settled", aborted=False))
    else:
        reply(cmd)
