# Use Sapper from Pi

English | [한국어](pi.ko.md)

Sapper keeps its Python graph and Claude backend. The Pi backend supports one-shot dispatch, auto, and persistent RPC workers without tmux or benchmark credentials. Arena operations still require separate arena configuration and authorization.

## Install and configure

Use Python 3.10 or newer, Node.js 22.19 or newer, and `@earendil-works/pi-coding-agent` 1.4.0. The RPC lifecycle requires `agent_settled`. macOS and Linux are supported. Windows is not supported because the supervisor uses Unix sockets and `flock`.

Clone the Pi branch and add its CLI to this terminal's PATH:

```sh
git clone --branch feat/pi-rpc https://github.com/sehoon123/sapper-v2.git
cd sapper-v2
export PATH="$PWD/bin:$PATH"
sapper --version
```

The PATH change lasts for this terminal session. From this checkout, you can also use `./bin/sapper` directly, or explicitly install the CLI with `bash install.sh --cli-only`. This opt-in installer creates a user symlink. The Pi package itself never runs install scripts.

To load the Pi package for one session, run `pi -e /absolute/path/to/sapper-v2`. For a persistent project setup, run these commands from your project with the bundled CLI's absolute path in place of `sapper` if needed:

```sh
sapper setup --backend pi --goal "Document the local example project"
sapper status
sapper context
pi
```

Setup adds the package path to this project's `.pi/settings.json`. It does not change user-level Pi settings, credentials, Claude settings, or AGENTS.md. The extension adds the bundled `bin` directory to its process's PATH, so model-issued `sapper` commands need no global CLI install. Project trust is your decision. Review resources in Pi's normal trust flow and save the decision if managed RPC runs should load project resources. Sapper neither forces approval nor distrust; RPC has no startup trust prompt, so unknown project resources may remain unloaded under Pi's native rules.

Global package installation is optional and explicit with `pi install /absolute/path/to/sapper-v2`. The extension supplies graph context and bundled CLI discovery, not a tool policy. Do not install globally if you only want it in one project.

Configure your provider using Pi's `/login`, its supported provider environment variable, or your own `models.json`. Sapper does not configure authentication or rotate Pi keys. `--model` accepts an exact model ID or `provider/id`. Use `--provider` to disambiguate a bare ID. Unknown or ambiguous models fail rather than falling back. Omitting `--model` uses Pi's selected default, never Claude's `sonnet` default or `SAPPER_MODEL`.

## Dispatch a step

```sh
sapper dispatch reason --backend pi --model provider/model-id
sapper dispatch brainstorm --backend pi --max-options 4
sapper dispatch blindspot --backend pi
sapper dispatch i001 --backend pi --claim
sapper dispatch verify --intent i001 --backend pi
sapper auto --backend pi --max-steps 2 --max-explore 1
```

Replace the model and intent IDs with configured models and existing graph intents. Dispatch validates the existing reason, explore, verify, blindspot, and brainstorm payloads before graph changes. Model startup or provider failures make Pi auto fail visibly. Pi handles its own provider retries.

`SAPPER_PI_BIN` or `--pi-bin` selects the Pi executable. `SAPPER_BIN` retains its legacy Claude meaning. One-shot sessions are stored in private `.sapper-runtime/dispatch-*` directories for inspection.

## Native Pi execution and user controls

Sapper adds no tool allowlist or shell hook. Ordinary Pi has shell and file access; bash, write, edit, and custom tools work without a Sapper opt-in, subject to your native tool selection and permission extensions. Managed runs inherit Pi resources: extensions, skills, prompt templates, themes, context/instructions (including `APPEND_SYSTEM.md`), MCP, provider environment, and native project-trust decisions. The bundled extension adds graph context and CLI discovery; an explore prompt supplements rather than replaces your native prompt resources. Sapper does not force offline mode or override Pi telemetry/version settings.

The shared `--skip-permissions` flag is accepted as a **no-op for Pi**: Pi has no equivalent core per-tool approval flag. It does not approve project trust or bypass user permission extensions. Claude still receives `--dangerously-skip-permissions`. The former `--pi-policy`, `SAPPER_PI_POLICY`, and policy command are removed, not alternative modes; remove the obsolete flag from scripts.

There is no interactive approval-forwarding bridge in this RPC client. If a loaded extension requests `select`, `confirm`, `input`, or `editor`, Sapper sends a cancellation and **fails the run visibly** rather than treating a cancelled interaction as success. This also applies during startup. Notifications may be undisplayed; native custom TUI features are unsupported/degraded by Pi RPC itself. Use interactive Pi or configure the extension's own supported noninteractive behavior when interaction is needed; Sapper does not silently disable extensions or approve dialogs.

Neither Sapper nor Pi is an OS sandbox. Ordinary tools and user resources can access available credentials, network services, and files, or launch processes. Use your normal user controls and, where appropriate, a dedicated OS user, container, or VM with suitable mounts/network access. Graph payload validation, intent confirmation, ownership, and explicit budgets still apply; graph context remains bounded and labeled as untrusted data.

## Run persistent workers

Run supervisor commands from one campaign directory. Each worker has its own session and stable ID, even when workers share a model and graph.

```sh
sapper worker up --max-workers 4 --seconds 3600
sapper worker spawn --cwd . --model provider/model-id
sapper worker list
sapper worker board
```

Copy the returned `id`, such as `w-0123456789abcdef`, into the following commands:

```sh
sapper worker message w-0123456789abcdef "Summarize the local README without changing files"
sapper worker peek w-0123456789abcdef
sapper worker message w-0123456789abcdef --intent i001
sapper worker stop w-0123456789abcdef
sapper worker resume w-0123456789abcdef
sapper worker down
```

Message accepts a new business run only while idle or completed. It does not queue, steer, or interrupt an active mission. `peek` reports status, final result, error, session path, mission identity, and timestamps as JSON. A free-text message returns text. `--intent` claims the worker's project-local graph intent, renders the explore template, validates the final JSON payload, and concludes the intent atomically. Only confirmed intents can run.

The current user Pi can act as commander by calling these commands when its native user configuration permits shell use. No unattended strategic commander is implemented. An unattended caller can use this same worker mechanism rather than launching a second unmanaged Pi.

For foreground service management, run `sapper worker serve --max-workers 4 --seconds 3600`. `up` starts that process in the background and waits for readiness. `down` stops new input, clears queues, aborts, closes worker pipes, and waits for the socket to disappear. SIGTERM and SIGINT request the same cleanup. Failed workers are not automatically restarted. The crash restart budget is zero; explicit `resume` is required.

## Recover a campaign

The campaign owns `.sapper-runtime`, which must be mode 0700 and owned by your user. Its socket is mode 0600. Registry and session files stay separate from `.sapper`. Do not share this directory with other users. Sessions may contain sensitive model input and output. Runtime directories are ignored by Git.

The first supervisor start persists an absolute deadline and your positive-integer global worker cap; there is no extra 32-worker ceiling. Restarting with a different `--seconds` or `--max-workers` does not extend or change them. The deadline stops idle workers too. Use a separate campaign directory for a new budget.

Existing worker registries are migrated under the supervisor lock: only obsolete `settings.policy` is removed, while unrelated fields, sessions, generations, claims, caps, and deadlines are preserved (apart from normal restart status transitions). Unknown execution settings fail without rewriting the registry. No session deletion or reset is needed.

After supervisor death, start it again with `worker up`. Old stdio cannot be reattached. Workers become interrupted and must be resumed explicitly. Resume requires the exact recorded session file and proof that the prior PID is absent. Any live PID, including a reused PID, blocks recovery conservatively. Sapper never signals a prior supervisor's PID. A crash before the child PID was recorded leaves the worker `unresolved`, even when a resumed worker already has a session file. This state blocks resume, stop, claim release, and reuse of its worker capacity across restarts. Inspect the owned runtime and processes rather than guessing which session to open. Sapper cannot automatically prove that an unrecorded child has exited.

After the prior process is gone, resume releases only that worker's open graph claims and starts a new generation. It does not replay an interrupted mission. Inspect the graph and side effects, then issue a new message deliberately. Completed text, failed work, interrupted work, idle sessions, and dead processes remain distinguishable in the registry.

A worker stopped before its first persisted turn may have no session file and cannot resume. Spawn a new worker instead. Unix socket path limits require a campaign path shorter than about 75 bytes on macOS. The CLI rejects longer paths.

## Remove project integration

```sh
sapper teardown --backend pi
```

Teardown removes only package entries that Sapper setup added. Repeating setup or teardown is safe. Existing package registrations, unrelated settings, Claude integration, and AGENTS.md remain intact. Graph data and runtime sessions remain. `--purge` explicitly deletes the graph, so stop workers and back up data first.

## Legacy boundary

Pi is not supported by `bench`, `sweep`, `command`, or the tmux `fleet` commands. `--backend pi` fails before these commands start work. Their Claude behavior is unchanged. The legacy `docker/commander` scripts reject `SAPPER_BACKEND=pi` before creating files or contacting services. Use `worker` and the small Pi image instead. No benchmark configuration is needed unless you explicitly use arena operations. These are missing backends, not permission restrictions. Pi workers require explicit orchestration; no unattended strategic commander or running `steer`/`follow_up` bridge is implemented. Exact model selection has no Claude-style alias/fallback/key rotation. Pi auto failures are fatal, not silently retried by Sapper. Exact owned sessions, no arbitrary session switching/forking, stale-generation rejection, conservative orphan recovery, and bounded protocol/context sizes remain lifecycle/data invariants, not tool restrictions.

## Run the small container

Build only the Pi runtime, not the Kali toolchain:

```sh
docker build -f docker/pi/Dockerfile -t sapper-pi .
docker run --rm --init -v "$PWD:/work" sapper-pi status
```

The image pins Pi 1.4.0 and Node 22.19.0. It runs as user `node`, UID 1000. The mounted project must be writable by that user for graph changes. Mount only the required files. Supply provider configuration separately through scoped environment variables or a dedicated Pi config volume. Do not mount your entire home. The default container command runs `worker serve` in the foreground. The container does not require benchmark variables or publish network ports.

## Verify without external models

From the checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_pi.py
git diff --check
```

The integration command uses the installed Pi executable and a deterministic localhost OpenAI-compatible provider. Temporary homes, settings, model configuration, and sessions isolate it from host credentials. It checks default bash/write/edit and custom-tool effects through public dispatch and workers, CLI discovery, native resources and append prompts, selected tool configuration and saved project trust, visible dialog failure, graph context, final results, old-registry migration, exact-session resume, worker ownership, caps/deadlines, crash recovery, and cleanup. Isolation and trust configuration exist only in the disposable fixtures, not in the production launcher. It does not contact external models or assessment targets.
