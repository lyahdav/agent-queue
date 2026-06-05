# agent-queue

`agentq` is a local multi-project agent queue runner backed by one Google
Sheets spreadsheet. It generalizes the single-project spreadsheet workflow:
one `agentq watch` command starts one serial worker per enabled project, while
different projects can run in parallel.

Each task gets a fresh agent context by launching a new CLI process, initially
`codex exec`.

## Spreadsheet Layout

Create a `Projects` tab with these headers:

```text
Project ID,Enabled,Sheet Name,Repo Path,Default Branch,Agent,Command File,Verify Command,Poll Seconds
```

Keep the columns in this order. The Apps Script reads project rows by column
position and returns the internal field names expected by `agentq`.

Project columns:

| Column | Description |
| --- | --- |
| Project ID | Unique project key used by commands such as `agentq worker --project <project-id>`. If `Sheet Name` is blank, this is also used as the task tab name. |
| Enabled | Controls whether workers can claim new tasks. Truthy values are `1`, `true`, `yes`, `y`, `on`, and `enabled` (case-insensitive). Any other value, including `0`, `false`, `no`, `off`, or blank, is treated as disabled. |
| Sheet Name | Name of the task tab for this project. Defaults to `Project ID` when blank. |
| Repo Path | Local filesystem path to the git repository for this project. |
| Default Branch | Branch the worker must be on before claiming tasks. Defaults to `main` when blank. |
| Agent | Agent runner to use. Currently supported value: `codex`. Blank also defaults to `codex`; other values are reserved for future runners. |
| Command File | Path, relative to `Repo Path`, to the instruction file included in the implementation prompt. Defaults to `agents/commands/tdd.md` when blank. |
| Verify Command | Shell command run from `Repo Path` after the agent commits its work. This is required for implementation tasks; failures trigger the fix loop. Example: `python3 -m pytest`. |
| Poll Seconds | How often a forever worker checks for another task when none is available. Defaults to `30`; values below `5` are raised to `5`; invalid values default to `30`. |

Create one task tab per project. Each task tab uses these headers:

```text
ID,Status,Task,Commit SHAs,Redo Reason,Claimed At,Updated At,Last Error
```

Supported actionable statuses:

- `READY`: implement the task.
- `REDO`: implement again, with `redoReason` and prior commit SHAs as context.
- `PLAN`: produce an implementation plan only.

In-flight and review statuses:

- `IN PROGRESS`
- `PLAN IN PROGRESS`
- `PLAN REVIEW`
- `VERIFY`
- `FAILED`
- `DONE`

## Apps Script

Paste `apps-script/agent-queue-apps-script.js` into the spreadsheet's Apps
Script project and deploy it as a web app.

Configure the URL locally:

```bash
mkdir -p ~/.agent-queue
printf 'AGENTQ_WEB_APP_URL="https://script.google.com/macros/s/.../exec"\n' > ~/.agent-queue/config.env
```

`TODO_WEB_APP_URL` is also accepted for compatibility.

## Usage

From this repo:

```bash
python3 -m agentq status
python3 -m agentq watch
python3 -m agentq worker --project my-project
python3 -m agentq add --project my-project --task "Add a test for the parser"
python3 -m agentq attach --project my-project
```

For convenience, you can add this directory to your `PATH` and use `bin/agentq`.

## Output Model

`agentq watch` prints only concise worker events. Raw agent output is written to
per-run logs under:

```text
~/.agent-queue/runs/<projectId>/<taskId>-<timestamp>/
```

Each run contains:

- `metadata.json`
- `events.jsonl`
- `output.log`
- `agent.log`
- `verify.log`
- `fix-<n>.log`

Use `agentq attach --project <projectId>` or `agentq attach --run <runId>` to
follow one task's `output.log` without intermingled output from other projects.

## Safety Defaults

- One worker per project.
- One local lock per project/repo.
- A worker checks that the project is enabled before every new claim.
- If a project is disabled mid-task, the worker finishes the current task and
  exits before claiming another.
- A worker skips claiming new tasks if the repo has uncommitted changes.
- A worker verifies it is on the configured default branch before claiming.
