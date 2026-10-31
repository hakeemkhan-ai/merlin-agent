# Merlin architecture and task flow

These diagrams describe the current implementation. PostgreSQL is the production
store; local development can use SQLite. The database also holds the task queue.

## ASCII overview

```text
  You
   |
   v
Terminal UI / CLI  <--- persisted replies and progress ---+
   |                                                    |
   v                                                    |
FastAPI  <----------> PostgreSQL / SQLite <---------------+
                         | tasks, history, leases,
                         | approvals, budgets, events
                         v
                   Worker / agent loop
                         |
                         v
                      Gemini <-------------------------+
                         |                             |
                    Needs tools?                       |
                     /       \                         |
                   No         Yes (repository selected)|
                   |           |                       |
                   v           v                       |
              Chat reply  Create/restore E2B workspace  |
                   |           |                       |
                   |      Validate permissions         |
                   |        /              \           |
                   |       v                v          |
                   |   E2B tools       GitHub adapter   |
                   |   files, Bash,    scoped access,   |
                   |   LSP, browser    publishing review|
                   |        \              /           |
                   |         v            v            |
                   |       Save tool results ---------+
                   |       and coding checkpoints
                   v
          Store final reply and stream it to the terminal
```

The terminal can run locally while connecting to a local or hosted API. Ordinary
chat uses Gemini without creating an E2B workspace or accessing GitHub. Without a
selected repository, tools are omitted and unexpected tool calls are refused.

## Architecture

```mermaid
flowchart LR
    UI["Terminal UI / CLI"]

    subgraph Hosted["Hosted Merlin service"]
        Edge["Caddy<br/>HTTPS"]
        API["FastAPI<br/>authentication, task controls, event streaming"]
        DB[("PostgreSQL<br/>tasks, history, events, leases, approvals, budgets")]
        Worker["Worker / agent loop<br/>context, tool dispatch, recovery"]
        Files[("Persistent artifacts<br/>checkpoints, screenshots, large outputs")]
        Broker["Trusted GitHub adapter<br/>scoped credentials and publishing checks"]
    end

    Gemini["Gemini<br/>answers and proposed tool calls"]
    Sandbox["E2B workspace per task<br/>repository, Bash, tests, file tools, LSP, Chromium"]
    GitHub["GitHub<br/>repositories, branches, PRs, issues"]

    UI <-->|"Requests and live events"| Edge
    Edge <--> API
    API <-->|"Submit tasks / read state"| DB
    Worker <-->|"Claim work / record progress"| DB
    Worker <-->|"Build context / receive proposals"| Gemini
    Worker <-->|"Execute repository tools / receive results"| Sandbox
    Worker <-->|"Save / restore checkpoints"| Files
    API -->|"Read previews and downloads"| Files
    Worker -->|"Authorized GitHub operations"| Broker
    API -->|"GitHub connection and review previews"| Broker
    Broker <--> GitHub
    Sandbox -->|"Read-only repository clone"| GitHub
```

The API and worker are separate processes. The GitHub adapter and context/tool
components run within those processes. Repository commands execute inside E2B.
GitHub write credentials remain with the trusted adapter.

## How a task runs

```mermaid
flowchart TD
    Prompt["You submit a message; repository optional"]
    Queue["API authenticates, validates and stores a queued task"]
    Claim["Worker claims a lease"]
    Workspace["Prepare E2B workspace if not active<br/>clone repository or restore checkpoint"]
    Context["Build context<br/>message, conversation, available repository instructions<br/>check budget"]
    Model["Ask Gemini and save its response"]
    Calls{"Tool calls requested?"}
    Approval{"Publishing approval needed?"}
    Review{"Owner reviews the exact proposal"}
    Execute["Validate and dispatch tool<br/>E2B, GitHub adapter or child-task executor"]
    Certain{"Outcome certain?"}
    Record["Save tool result<br/>checkpoint mutations"]
    Paused["Pause for owner inspection or review"]
    Finish["Save answer and any active workspace checkpoint<br/>mark task succeeded"]

    Prompt --> Queue --> Claim --> Context --> Model --> Calls
    Calls -->|"No"| Finish
    Calls -->|"Yes; repository selected"| Workspace
    Workspace --> Approval
    Approval -->|"No; already permitted or not publishing"| Execute
    Approval -->|"Yes"| Review
    Review -->|"Approve"| Execute
    Review -->|"Request changes"| Queue
    Review -->|"Reject"| Paused
    Execute --> Certain
    Certain -->|"Yes; success or known failure"| Record
    Record --> Context
    Certain -->|"No; effect may have happened"| Paused
    Paused -->|"Owner resolves or resumes when allowed"| Queue
```

1. **Submit:** the terminal sends an authenticated request. A submission ID prevents
   retries from creating duplicate tasks.
2. **Prepare:** the worker claims durable ownership and assembles conversational
   context. It starts an isolated workspace only when repository tools are requested.
   Coding follow-ups inherit the latest successful coding checkpoint, even after chat turns.
3. **Work:** Gemini receives the task context and proposes actions. Merlin checks
   permissions and executes the tools. Results return to Gemini for the next turn.
4. **Publish:** GitHub writes use task-scoped permission or explicit approval of
   the prepared operation. Repository text and model output cannot grant permission.
5. **Finish:** the final answer, events and any workspace checkpoint are stored. The API streams
   persisted progress to the terminal throughout the task.

Closing the terminal detaches; hosted work continues. Cancellation is an explicit
request that also stops child tasks. After a worker crash, a replacement claims
the expired lease and restores a checkpoint into a fresh workspace. Completed
operations reuse their saved results; uncertain mutations require inspection and
explicit resolution before work continues.

Optional child tasks receive separate E2B workspaces, share the parent's budget,
and cannot publish. The parent checks for file conflicts before integrating their
changes.

Implementation references: [composition root](../src/merlin/application.py),
[worker loop](../src/merlin/worker/__init__.py),
[durable store](../src/merlin/persistence/store.py), and
[tool authorization](../src/merlin/agent/__init__.py).
