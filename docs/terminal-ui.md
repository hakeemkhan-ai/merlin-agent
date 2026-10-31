# Using Merlin's terminal interface

Merlin now has a full-screen terminal interface with conversations, recent sessions,
live progress, source previews, and publishing reviews. Repository work still runs
in E2B, and closing the interface leaves tasks running.

## Try it without any setup

Open a terminal in the Merlin project folder and run:

```sh
uv sync --locked --dev
uv run merlin ui --demo
```

This demonstration uses synthetic data entirely on your laptop. It needs no API
server, worker, credentials, Docker, or provider credit. Its example approvals
change only the demonstration's in-memory state. Press **Ctrl+Q** to exit.

## Connect to your real project

Keep the Merlin API and worker running in their existing terminals. After updating
Merlin, restart an idle API and worker so they load the new code:

```sh
# API terminal, after stopping the old process with Ctrl+C
uv run merlin serve

# Worker terminal, after stopping the old process with Ctrl+C
uv run merlin-worker
```

Do not stop the worker during a task just to update the interface. Wait for it to
finish, or deliberately cancel the task and wait for cancellation to complete.
No database migration is needed for the interface.

In another terminal:

```sh
uv run merlin
```

If you have already used `merlin login`, Merlin uses that saved connection.
Otherwise, enter the **Merlin server URL** and **owner token** in the connection
screen. For your local server, use `http://127.0.0.1:8000`.
Gemini/E2B keys and GitHub App credentials stay in the server's configuration.

Type a greeting or question and press Enter to chat. No repository, GitHub App, or
E2B key is needed for ordinary conversation; the worker needs a Gemini key.
Merlin keeps earlier messages in the same session as conversational context.

For coding, choose **Choose repository**, select your repository, type a task,
and press Enter. Selecting a repository starts a new session. Greetings and general
questions still receive direct answers; repository workspaces start when tools are needed.
Running the terminal locally still uses the API and worker. Repository tools run in
E2B rather than your terminal's current directory.

You can also choose the repository when opening Merlin:

```sh
uv run merlin ui --repo OWNER/REPOSITORY
```

## Everyday controls

| Control | Action |
|---|---|
| Enter | Send the message; queue a follow-up if work is already active. |
| Ctrl+J / Alt+Enter | Add a newline. Shift+Enter also works in supporting terminals. |
| Ctrl+P | Search all available commands. |
| Ctrl+N | Start a new conversation. |
| Ctrl+B | Toggle the sidebar, or open session selection on a narrow terminal. |
| Ctrl+Q / Ctrl+C | Exit and detach; remote work continues. |
| Escape | Close an overlay. |
| Tab | Move focus, or accept a visible `@file` suggestion. |

Pasting multiple lines does not send them. Drafts survive session switching while
the interface is open; typing the next draft while a submission is pending also
preserves it. Unsent drafts are not saved to disk on exit. Changing servers clears
the previous connection's drafts and task selection.

Use the **Tasks** button to select which task the **Review**, **Cancel**, and usage
actions apply to. A queued follow-up does not interrupt the current model call.
Earlier messages load in pages; **Back to latest** returns to current work.

## Commands inside the conversation

| Command | Action |
|---|---|
| `/new`, `/sessions`, `/repo` | Start or switch work. |
| `/model` | Set a model ID for future tasks. |
| `/details` | Expand tool output and load recorded activity for the selected task. |
| `/usage` | Show recorded cost estimates, reservations, calls, and sandbox usage. |
| `/approvals` | Inspect pending publishing proposals in this conversation. |
| `/files` | Browse source files from a recovery checkpoint. |
| `/cancel` | Request cancellation of the selected task and its children. |
| `/resume` | Resume a paused task after any uncertain operations have been resolved. |
| `/resolve` | Inspect/acknowledge an uncertain operation using a required note. |
| `/reconnect`, `/connect` | Restore the stream or change connection settings. |
| `/theme`, `/help`, `/quit` | Change theme, read help, or detach. |

The server supplies the default model. Manually entered IDs are labeled as
unverified: the interface does not assume your account has access to them.
Cost figures are service estimates and can differ from provider billing.

## Files and publishing

Type `@READ` to see matching checkpoint paths, then Tab to insert a reference.
Before the first checkpoint, type the path manually. A reference tells the agent
which file to inspect; it does not upload local laptop files or insert hidden
file contents into your prompt.

Source previews display the checkpoint identity and timestamp. Each page contains
at most 200 lines and 64 KiB. Binary files and clipped lines are identified.
The file picker lists the first 2,000 matching paths; `@` suggestions search by path.

Publishing proposals appear under **Review**. Read the repository, branch, message,
and proposed files. Use **Details** for complete metadata, **Choose file** for other
files, and **Next** for additional preview pages. Diffs use the exact base commit;
if it cannot be read, the screen explicitly shows proposed content instead.

Choose **Approve this operation**, **Reject**, or **Request changes** deliberately.
Changing a proposal requires fresh approval. Tool output and model text cannot
approve operations. Uncertain external mutations are never automatically retried.

## Reconnect and scripting

```sh
uv run merlin ui --session SESSION_ID
uv run merlin ui --task TASK_ID
uv run merlin chat --plain --repo OWNER/REPOSITORY
uv run merlin attach TASK_ID --json
```

The interface retries interrupted event reads three times, then exposes
`/reconnect`. A submission with an unknown outcome retains its submission ID:
retry the unchanged message to retrieve the same task.
This also applies to request timeouts and server/gateway failures, which can occur
after the server has already accepted the task.

Existing command-line commands and JSON output remain available. Redirected output
never opens the full-screen interface. Use a terminal of at least 80 columns and
24 rows; the sidebar appears at 110 columns. Dark, light, and monochrome themes
are available; `NO_COLOR` selects monochrome initially.

## Troubleshooting

- **Server update required:** the running API predates the new read endpoints;
  restart the idle API and worker with this installation.
- **API connected but work stays queued:** API connectivity does not establish
  worker or provider readiness. Check the worker terminal and `merlin doctor`.
- **Owner authentication failed:** use `/connect` with the matching owner token.
- **No repositories:** check the GitHub App installation using `merlin github repos`.
- **No file suggestions:** wait for a checkpoint; failed/missing snapshots cannot
  provide a source preview.
- **Styling or terminal limitations:** choose monochrome or use `chat --plain`.

UI preferences are stored beside the existing client configuration in `ui.json`
(mode 0600). `MERLIN_UI_CONFIG` overrides that path. Conversation history stays
on the service; preference files contain no conversation drafts or provider keys.

## Contributor verification

Default tests use synthetic providers, actual terminal widgets, and SQLite.
Run `uv run pytest tests/test_tui.py tests/test_terminal_client.py tests/test_terminal_views.py`.
Generate synthetic SVG captures with:

```sh
uv run python scripts/capture_terminal.py
```

Captures are written to ignored `.merlin/reports/tui/`. No live model, sandbox,
GitHub publication, or deployment is exercised by these checks.
