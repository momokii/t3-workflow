# Read-only verification threads — what this machine can actually do

Question: a verifier thread must run with read-only tools (no file edits, no shell writes), on a chosen
provider and model, and the parent must be able to read its final reply. `t3 thread start` has no
`runtimeMode` flag. What is really available on **this** machine?

Method: read-only inspection of `t3` v0.1.9 (help output, embedded source), the running server, its
HTTP surface, and `~/.t3/userdata/statev2.sqlite` opened with `?mode=ro`. No thread was started, no
ticket was touched, nothing was written to T3's database, and `~/.local/state/t3-workflow/reports/` was
not opened. Badges: `[measured]` = a command produced this output here, `[code]` = quoted from the
TypeScript embedded in the binary, `[schema]` = quoted from SQLite, `[inference]` = my reasoning.

**Answer in one line.** T3 0.1.9 has no read-only *thread*. `read-only` is a **credential-level access
value** — an MCP client access level and an auth scope — and a read-only credential is structurally
forbidden from creating a thread. The thread-launch parameter is a four-value enum with no read-only
member, and the CLI cannot set it at all. What can be enforced from outside is **evidence integrity**,
not tool restriction.

---

## 1. What was measured

### 1.1 The runtime-mode enum has four values, none of them read-only

`[code]` — `strings -a ~/.t3/runtime/versions/0.1.9/t3 | sed -n '1104017,1104029p'`:

```
	RuntimeMode$1 = Literals$3([
		"approval-required",
		"auto-accept-edits",
		"auto",
		"full-access"
	]);
	DEFAULT_RUNTIME_MODE$1 = "full-access";
	RUNTIME_MODE_RANK = {
		"approval-required": 0,
		...
```

The provider adapters declare the same four values (`type RuntimeMode = "approval-required" |
"auto-accept-edits" | "auto" | "full-access"`, `[code]` at `strings` line 1398181). There is no
`"read-only"` and no `"readonly"` anywhere in the runtime-mode literals.

`[code]` The one place a *sandbox* is named read-only is a separate type — the sandbox-policy kind,
`Literals$3(["read-only", "workspace-write", "danger-full-access"])` at `strings` lines 1104012-1104016 — and it
is derived from the runtime mode, not set alongside it (see §1.8).

### 1.2 Every CLI flag that could name a mode, approval, sandbox or tool policy

`[measured]` — the `--help` of every subcommand, grepped for `--(mode|approval|sandbox|tool|permission|policy|isolat…)`:

```
== t3  ==                    --mode choice   Runtime mode. `desktop` keeps loopback defaults …  (choices: web, desktop)
== t3 serve ==               --mode choice   … (choices: web, desktop)
== t3 thread start ==        (no mode/approval/sandbox/tool/permission flag)
== t3 thread send ==         (none)
== t3 project add ==         (none)
== t3 app | browser | theme | status | doctor | trace | up | connect | auth ==  (none)
== t3 environment link ==    --access choice  What this environment's agents may do there: read-only, or
                             the broadest runtime mode. (choices: read-only, approval-required,
                             auto-accept-edits, auto, full-access)
```

`t3 --mode` / `t3 serve --mode` is `web | desktop` — how the *server* runs, not a thread. The single
`read-only` on the whole CLI belongs to `--access`, and `[measured]` its own help says what it is for:

```
$ t3 environment link --help
DESCRIPTION
  Link another T3 Code environment, so agents here can work there. It shows up there under
  Settings → Connections, where it can be revoked.
FLAGS
  --pairing-code string    A pairing code from the other environment (`t3 pair` there).
  --access choice          What this environment's agents may do there: read-only, or the broadest
                           runtime mode. (choices: read-only, approval-required, auto-accept-edits,
                           auto, full-access)
```

That flag governs what **agents arriving from another environment** may do, and it is applied when this
environment acts as an MCP **client** towards the other one. It has nothing to do with a thread this
watcher starts locally.

`thread start`'s complete flag list, verbatim `[measured]`: `--base-dir`, `--title`, `--provider`,
`--model`, `--worktree`, `--branch`, `--wait`, `--json`. Nothing about modes. `thread send`: `--base-dir`,
`--wait`, `--json`. `thread show`: `--base-dir`, `--messages`, `--json`.

**Plainly: no flag on this CLI influences a thread's runtime mode, interaction mode, approval policy or
tool permissions.** `[measured]`

### 1.3 There is no `t3 mcp` subcommand

`[measured]`:

```
$ t3 mcp
ERROR
  Unknown command "mcp". Use "t3 --help" for commands or an explicit path such as "t3 ./my-project"
  for a new directory.
exit=1
$ t3 --help | grep -i mcp
(no match)
```

`t3 --help` lists: `help serve app pair auth environment browser project thread up status service update
install uninstall theme trace doctor policy triage connect`. No MCP entry.

### 1.4 Two hidden (unlisted) commands exist, and they are not a way in

`[code]` The binary registers commands with `unlisted`, which suppresses them from help. The two that
mention MCP (`strings` lines 1225525–1225529):

```
acpMcpBridgeCommand = make$241("acp-mcp-bridge").pipe(
    withDescription$1("Bridge T3 Code's MCP endpoint to stdio for ACP agents."), unlisted, …)
… = make$241("acp-mcp-call", …).pipe(
    withDescription$1("Call one T3 Code MCP tool from an ACP agent terminal."), unlisted, …)
```

`[code]` Both refuse to run without credentials the server injects into the *spawned agent's* environment
(`strings` line 1099679–1099683):

```js
async function runAcpMcpCliFastPath(command, args) {
	const endpoint = process.env.T3_ACP_MCP_ENDPOINT;
	…
	process.stderr.write(`${command} requires T3_ACP_MCP_ENDPOINT and T3_ACP_MCP_AUTHORIZATION.\n`);
```

Those variables are set per agent launch (`strings` line 1256923–1256957). An external process does not
have them, and `[code]` the tokens behind them are ephemeral (§1.6). The other unlisted commands are
`start`, `__service-launcher`, `__ssh-helper`, `__preflight`, `native-speech-worker`,
`claude-history-worker`, `connect` (a stub), and a deprecated `service update` — none of them touch a
thread's mode. `[measured]`/`[code]`

### 1.5 An MCP endpoint *is* listening — but it is per-thread, in-memory, and closed to thread creation

`[measured]` A server is running: pid 2153 `t3 serve`, from `~/.t3/userdata/server-runtime.json`:

```
{"version":1,"pid":2153,"host":"0.0.0.0","privateNetworkOnly":true,"port":3773,
 "origin":"http://127.0.0.1:3773", "serverVersion":"0.1.9","managedPolicy":null, …}
```

`[measured]` The MCP route is mounted at `/mcp` (`[code]` `path: "/mcp"`, `strings` line 1495371). A
handshake without a credential:

```
$ curl -i -X POST http://127.0.0.1:3773/mcp \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",
         "capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer resource_metadata="http://127.0.0.1:3773/.well-known/oauth-protected-resource/mcp"
{"error":"invalid_mcp_credential","message":"A valid T3 Code MCP credential is required."}
```

`[measured]` `/api/mcp` and `/mcp/http` are 404 — `/mcp` is the endpoint.

**Transport**: HTTP POST, JSON-RPC, `accept` must include both `application/json` and
`text/event-stream`; `[code]` other methods get 405, a wrong `content-type` gets 415, a bad `accept` 406,
and replies come back as SSE (`addMcpHttpRoutes`, `strings` line 1487623). The handshake uses
`mcp-protocol-version: 2025-06-18` and, after initialize, `mcp-session-id`; `[measured]` the pi bridge on
disk shows exactly that sequence (`~/.t3/caches/pi-t3-mcp-extension.ts`, `createMcpClient`).

**Who can get a credential.** Three issuers, none usable by a stdlib watcher:

| Issuer | Scope/limit | Evidence |
|---|---|---|
| `McpSessionRegistry.issue` | 32 random bytes, SHA-256 hashed, stored in an **in-memory `Map`**, bound to one `threadId` + `providerSessionId` + `providerInstanceId`, with a liveness window | `[code]` `const state = yield* make$248({ records: new Map() })`, `strings` 1411436; `issue` at 1411441 |
| OAuth client flow | `t3 pair` / `t3 auth pairing create` / `t3 auth session issue` mint bearer tokens; `/mcp` advertises OAuth metadata | `[measured]` below |
| Linked environment | `t3 environment link --access …`, pairing code from `t3 pair` | `[measured]` §1.2 |

`[measured]` the OAuth metadata the 401 points at:

```
$ curl -sS http://127.0.0.1:3773/.well-known/oauth-protected-resource/mcp
{"resource":"http://127.0.0.1:3773/mcp","authorization_servers":["http://127.0.0.1:3773"],
 "scopes_supported":["orchestration:read","orchestration:operate"],
 "bearer_methods_supported":["header"],"resource_name":"T3 Code"}
```

So an external process *can* authenticate — `[measured]` `t3 auth session issue --scope … --ttl …` exists
and offers `orchestration:read`, `orchestration:operate`, `settings:write`, `filesystem:read/write`,
`source-control:write`, … and `t3 pair` mints the same. But `[code]` the thread tools refuse a client that
is not an agent inside T3 (`strings` line 1411405):

```js
threadCallerRequired = (operation) => new OrchestratorMcpFailure({
    code: "thread_credential_required",
    message: `${operation} acts as the calling T3 thread, so it needs an agent running inside T3 Code.
              This MCP client signed in from outside a thread.`
});
requireThreadScope = (scope, operation) => scope.thread === void 0 ? fail$8(threadCallerRequired(operation)) : …
```

`thread_credential_required` is also a documented `delegate_task` failure code (`strings` lines 1126323,
1127710). **An external MCP client can read, but cannot launch a thread.** `[code]`

### 1.6 "read-only" is a client access level that *forbids* what we need

`[code]` — `strings -a … | sed -n '1104236,1104249p'`:

```js
AuthMcpClientAccess = Literals$3(["read-only","approval-required","auto-accept-edits","auto","full-access"]);
mcpClientScopes = (access) => access === "read-only" ? [AuthOrchestrationReadScope] : access === "full-access" ? [
    AuthOrchestrationReadScope, AuthOrchestrationOperateScope, AuthSettingsWriteScope, AuthSourceControlWriteScope
] : [AuthOrchestrationReadScope, AuthOrchestrationOperateScope];
```

and `[code]` `strings` line 1411349:

```js
clientModeCeiling = (client) => {
    const approved = {
        runtimeMode: client === void 0 || client.access === "read-only" ? "approval-required" : client.access,
        interactionMode: "default"
    };
    …
```

Consequences, all `[code]`:

- `read-only` maps to **`orchestration:read` only**. `orchestration:operate` is what every thread-mutating
  RPC requires (`CLIENT_GUARDED_RPC_SCOPES`, `strings` line 1129998, and `legacyParents` mapping
  `settings:write`, `source-control:write`, `filesystem:write` … up to `orchestration:operate`,
  `strings` 1104153–1104162).
- A read-only MCP client's *runtime mode ceiling* is `approval-required`, and `[code]` the mode such a
  session is shown is literally the string `"read-only"` when it lacks the operate scope
  (`strings` line 1222668):

```js
access: session.scopes.includes("orchestration:operate") ? session.runtimeModeCeiling ?? "approval-required" : "read-only",
```

That is the only place `"read-only"` names a session's capability. It is a *label on a read-only client's
session*, not a mode a thread can run in.

So the enum that would have to carry "read-only" — `RuntimeMode$1` — does not, and the access value that
does carry it cannot create a thread. **These two facts are the whole answer.** `[code]`

### 1.7 The thread-launch tool accepts `runtimeMode`, with four values, and no CLI reaches it

`[code]` — `t3_thread_launch`, `strings` line 1489544ff:

```js
ThreadLaunchTool = make$22("t3_thread_launch", {
    …shared$3,
    parameters: Struct({
        environmentId: …, projectId: optional(ProjectId), scratch: optional(Boolean),
        title: TrimmedNonEmptyString,
        modelSelection: optional(ModelSelection),
        runtimeMode: optional$11(RuntimeMode$1),          // ← approval-required | auto-accept-edits | auto | full-access
        interactionMode: optional$11(ProviderInteractionMode),
        workspaceStrategy: optional(…), message: optional(String.check(isMaxLength(12e4))),
        attachments: …, clientRequestId: …, delegatedFrom: optional(…)
    }),
    success: Struct({ threadId, link?, projectId, modelSelection, runId, status }),
    …
}).annotate(Destructive, true).annotate(OpenWorld, true);
```

`delegate_task` is the same shape (`[code]` `strings` 1125816): `runtimeMode:
optional(Union([Literal("inherit"), RuntimeMode$1]))`, described as *"Omit unless the user asks for a
supervised child. Narrow modes pause for human approval, not read-only. Above yours is rejected."* — the
tool's own description confirms "narrow" means **more prompts**, not fewer permissions.

`[measured]` the CLI does not send it. `t3 thread start --help` has no such flag, and `[code]` the
embedded CLI source calls the tool with only title/message/workspace/model (`strings` line 1443113), as
`docs/independent-verification.md:66-115` already documents.

`[inference]` Post-hoc switching is also out of reach: `[code]` the orchestration command
`thread.runtime-mode.set` exists as a command type — `{type, commandId, threadId, runtimeMode:
RuntimeMode$1}` (`strings` line 1113578), dispatched at 1419912 — but no CLI subcommand and no MCP tool
name exposes it; the only writer is the desktop UI over the WS API. Note the setter takes the same
four-value enum, so even it has no read-only option.

### 1.8 What a "narrow" mode actually does, per provider — and why it would break this watcher

For the provider this machine runs (settings.json: `pi` enabled with `launchArgs: "--approve"`; `codex`
and `claudeAgent` disabled — `[measured]`), the read-only story is a **per-tool confirm dialog**, not a
denial. `[code]` the T3 extension injected into every pi session (`~/.t3/caches/pi-t3-mcp-extension.ts`,
`strings` lines 1398180 and 1398461):

```ts
const READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls"]);
pi.on("tool_call", async (event, ctx) => {
  const mode = runtimeMode();                 // T3_PI_RUNTIME_MODE
  if (mode === "full-access") return;
  if ((event.toolName === "tool_search" ? hasBuiltinToolSearch() : READ_ONLY_TOOLS.has(event.toolName))
      || isReadOnlyMcpTool(event.toolName)) return;
  if (mode === "auto-accept-edits" && FILE_CHANGE_TOOLS.has(event.toolName)) return;
  const approved = await ctx.ui.confirm(`Allow ${event.toolName}?`, toolInputSummary(event.input), …);
  if (!approved) return { block: true, reason: `${event.toolName} was declined in T3 Code.` };
});
```

So under `approval-required` every `write`, `edit` and `bash` call becomes a pending approval. In T3 that
is a `waiting` run with `pendingRequestCount > 0`, i.e. `docs/independent-verification.md:566` row **8**
(`verify-needs-input` → `blocked`), not a secure refusal. **A headless watcher that forced
`approval-required` would stop working, not become safe.** `[code]`/`[inference]`

The providers that *do* have a hard read-only sandbox are not this one:

- Claude: `[code]` `CLAUDE_READ_ONLY_ALLOWED_TOOLS = ["Read","Glob","Grep"]` (`strings` 1324920) applied
  only when `sandboxPolicy.type === "readOnly"` (`strings` 1323972, 1324000), with
  `CLAUDE_READ_ONLY_T3_MCP_ALLOWED_TOOLS` narrowing the T3 tools to 14 read-only ones (`strings` 1325369).
  `claudeAgent` is `enabled: false` here. `[measured]`
- Codex: `[code]` `codexRuntimeModeTurnDefaults("approval-required")` →
  `{approvalPolicy:"untrusted", approvalsReviewer:"user", sandboxPolicy:{type:"readOnly"}}`
  (`strings` 1355206–1355217). `codex` is `enabled: false` here. `[measured]`
- ACP agents: `[code]` `acpOperationDisposition` denies mutations when the mode is `approval-required`
  and no sandbox policy is set (`strings` 1255603–1255623).

`[inference]` Even those paths are not selectable from the CLI: `sandboxPolicy` is a turn-level field
(`[code]` `V2TurnStartParams__SandboxPolicy`, `strings` 1353488) with no CLI or thread-launch surface.

### 1.9 The database: one runtime mode in use, and no tool-permission column anywhere

`[schema]` — `select runtime_mode, count(*) from orchestration_v2_projection_threads group by 1` on
`file:~/.t3/userdata/statev2.sqlite?mode=ro`:

```
('full-access', 281)
```

`projection_threads.runtime_mode` returned no rows (that legacy projection is empty). `[schema]`

`[schema]` `pragma table_info(orchestration_v2_projection_threads)`:

```
(0, 'thread_id', 'TEXT', 0, None, 1)
(1, 'project_id', 'TEXT', 1, None, 0)
(2, 'title', 'TEXT', 1, None, 0)
(3, 'default_provider', 'TEXT', 1, None, 0)
(4, 'runtime_mode', 'TEXT', 1, None, 0)
(5, 'interaction_mode', 'TEXT', 1, None, 0)
(6, 'active_provider_thread_id', 'TEXT', 0, None, 0)
(7, 'created_at', 'TEXT', 1, None, 0)
(8, 'updated_at', 'TEXT', 1, None, 0)
(9, 'archived_at', 'TEXT', 0, None, 0)
(10, 'deleted_at', 'TEXT', 0, None, 0)
(11, 'payload_json', 'TEXT', 1, None, 0)
(12, 'provider_instance_id', 'TEXT', 0, None, 0)
```

`[schema]` `provider_session_runtime` and `projection_thread_sessions` carry the same pair with a default:
`(3, 'runtime_mode', 'TEXT', 1, "'full-access'", 0)` and `(8, 'runtime_mode', 'TEXT', 1,
"'full-access'", 0)`. Measured values: `provider_session_runtime` → `('full-access', 39)`;
`projection_thread_sessions` → no rows. `[schema]` No row anywhere records a mode other than
`full-access`.

**Is there any table or column that records the tools a thread was allowed to use? No.** `[schema]`
Querying every column in the database whose name matches `tool|permission|sandbox|allow|policy` returns
exactly one row:

```
('mcp_app_model_context', 'tool', 'TEXT')
```

which is a UI-model-context key, unrelated to permissions. The tool allowlists are computed in the
provider adapter from the mode/sandbox at launch time (§1.8) and are never persisted.

`[measured]` The mode is, however, **visible from outside**: `t3 thread show --json` returns it.

```
$ t3 thread show --json --messages 0 'mcp:72b322c1-fc37-4640-9e92-c75484955db9'
{'threadId': 'mcp:72b322c1-…', 'status': 'running', 'runtimeMode': 'full-access',
 'interactionMode': 'default', 'providerInstanceId': 'pi', 'model': 'opencode-go/deepseek-v4.1-flash',
 'worktreePath': None, 'branch': None, 'runCount': 14}
```

`worktreePath` and `branch` are in that payload — the watcher can *observe* isolation and mode, and
therefore refuse a verdict produced under the wrong ones.

### 1.10 Concurrency: this repository moved while I was reading it

`[measured]` Two reads of the same tree, ten minutes apart:

```
23:0x  $ git status --short
        M board/server.py
        M board/static/index.html
        M tests/test_board.py
        M tests/test_catalog.py
        M tickets/catalog.py
       ?? board/static/guide.html
       $ git worktree list
       …/t3-workflow  a24c8f1 [main]

23:16  $ git status --short          → (empty)
       $ git rev-parse HEAD          → 123afbc29ff1fadf6b06ca7f5f76ae1f512a9ea7
       $ git log --oneline -3        → 123afbc feat(board): instant dialogs, a guide page and a cached catalogue
```

`[measured]` `git reflog` shows that commit landed at 23:14:49 while this research was running, and the
project's live thread `mcp:72b322c1-…` was `status: running, runCount: 14`. The repo has one worktree
(the root) and a remote `origin https://github.com/momokii/t3-workflow.git`.

`[inference]` This is the exact hazard the fingerprint gate in §3 exists for, observed rather than
assumed: a verification conducted against "the working tree" can start and finish on different trees.

---

## 2. What exists and what does not

| Capability | Status | Evidence |
|---|---|---|
| Start a thread with a chosen provider + model from the CLI | **exists** | `thread start --provider/--model` `[measured]` |
| Read the final reply of that thread | **exists** | `--wait --json` → `{status, reply, error}`; `thread show --json --messages N` `[measured]` |
| Start a thread in a separate git worktree | **exists** | `thread start --worktree <branch\|tag\|commit>` `[measured]` |
| Observe a thread's `runtimeMode`, `interactionMode`, `worktreePath` | **exists** | `thread show --json` `[measured]` |
| Set `runtimeMode` for a new thread from the CLI | **does not exist** | no flag; embedded CLI source sends only title/message/workspace/model `[measured]`/`[code]` |
| Set `runtimeMode: "read-only"` | **does not exist** — no such enum value | `RuntimeMode$1` has four members `[code]` |
| A runtime mode that denies writes | **does not exist** — `approval-required` asks, per tool | pi extension `tool_call` gate `[code]` |
| Set an approval policy or sandbox policy per thread | **does not exist** at CLI/thread-launch level; `sandboxPolicy` is turn-level (WS API) | `V2TurnStartParams__SandboxPolicy` `[code]` |
| A `t3 mcp` server command (stdio for external processes) | **does not exist** | `Unknown command "mcp"` `[measured]` |
| An MCP HTTP endpoint reachable from an external process | **exists** at `http://127.0.0.1:3773/mcp`, bearer-only | 401 + OAuth metadata `[measured]` |
| An external MCP client creating a thread | **refused by design** | `thread_credential_required` `[code]` |
| A read-only *credential* | **exists** (`access: read-only`, `--access read-only`, `orchestration:read`) and it cannot create a thread | `mcpClientScopes`, `clientModeCeiling` `[code]`; `t3 environment link --help` `[measured]` |
| A per-thread tool allowlist stored in the database | **does not exist** | column census `[schema]` |
| Changing a thread's mode after launch | no CLI, no MCP tool; only the orchestration command behind the UI | `thread.runtime-mode.set` `[code]` |

---

## 3. Recommended approach for a stdlib-only watcher

### 3.1 The honest premise

`[inference]` T3 0.1.9 offers **no mechanism, at any layer a Python script can reach, that restricts what
a verification thread's tools may do.** The four runtime modes control *how many prompts* a run emits and
*which sandbox a provider adapter installs* — and the provider in use here (pi) installs none, asking
instead. Any design that assumes a read-only verifier is assuming a feature that is not in this version.

The reachable goal is therefore different, and it is the one the repo actually needs: **make the verdict
trustworthy**, not make the verifier harmless. Two enforceable properties, both purely host-side:

1. **The tree the verifier judged is the tree that gets released** (tree identity).
2. **The project folder was not written to while verification ran** (fingerprint gate).

Anything the verifier does inside a throwaway worktree is then irrelevant, and anything it does outside it
is caught by (2) and refuses the verdict.

### 3.2 What can be enforced from outside — the four gates

All four are `subprocess` + `sqlite3` + `json` from the standard library, and all four are already the
shape the watcher uses (`watcher.py:104-122` `_git`, `:46-67` read-only DB access). `[code]`

| Gate | Question | Mechanism | Strength |
|---|---|---|---|
| **G1 tree identity at proof time** | did the tree the tests ran on change before commit? | `sha256(git status --porcelain=v1 -z --untracked-files=all ‖ git diff --binary)` recorded with the proof; recomputed before commit | real: catches any edit, including untracked |
| **G2 committed tree is the judged tree** | is the verifier looking at exactly what will be pushed? | commit first, then `git rev-parse HEAD^{tree}` → launch verifier with `--worktree <sha>`; `thread show --json` must report a non-null `worktreePath` | real: a worktree at a commit contains exactly that commit |
| **G3 project folder unchanged during verification** | did the verifier (or the dev thread) write to the real checkout? | recompute G1's fingerprint after the verdict; **refuse** on any difference | real for the project folder; does not cover `$HOME`, `/tmp`, other repos |
| **G4 the run is the run** | is this verdict from *our* fresh verification thread, on the configured profile? | `verify_thread_id` equals the one this stage created; `runCount == 1`; final message `runId == verify_run_id`; `thread show --json` `runtimeMode`/`providerInstanceId`/`model` equal the config | real; `runtimeMode` is observable, not settable, so it is a *detector* — assert it, never assume it |

`[measured]` G1's fingerprint command, run here on a clean tree:

```
{"head":   [0, "2d4b28490fc46550", 41],
 "status_z":[0, "e3b0c44298fc1c14", 0],
 "diff":    [0, "e3b0c44298fc1c14", 0],
 "staged":  [0, "e3b0c44298fc1c14", 0]}
```

`[inference]` Include `--untracked-files=all` in the status hash: the dev thread's new files are untracked
until `git add -A`, and a verifier creating a file in the project folder is exactly the case G3 must see.

### 3.3 Decision table

Options for the verification stage as it can be built today. "Damage bound" = the worst a wrong or
malicious verdict can do to released artifacts.

| # | Option | Restricts the verifier's tools? | Enforced by | stdlib-only | Damage bound | Cost | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Verifier in the project folder, no isolation (default `thread start`) | no | nothing | yes | can rewrite the release tree; G1/G3 catch it only if recomputed | none | **reject** — §3 row 3 would release a tree no test ran on |
| 2 | Ask T3 for `approval-required` (if a future CLI exposed it) | no — it *asks*; the run waits | nothing; ends in `waiting` | yes | none (it never finishes) | a human must answer | **reject** — breaks unattended running; §1.8 |
| 3 | Verifier in T3's worktree at a committed sha (`--worktree <sha>`) | no | `git worktree` semantics + G2 | yes | edits land in a copy; a shell can still reach the real folder | one commit moved earlier | **adopt**, with G3 |
| 4 | Option 3 + G3 fingerprint gate on the project folder | no | host-side hash compare | yes | release refused if the real folder changed | one extra hash per step | **adopt — recommended** |
| 5 | Option 4, but register a throwaway clone as its own T3 project (`t3 project add`) and verify there | no | physical separation of the project folder | yes (but writes T3 project state — allowed by invariant 9 via the CLI) | damage confined to a clone | project churn, per-project model defaults, cleanup | **hold** — more moving parts for the same enforceable property; revisit only if G3 proves noisy |
| 6 | Do not use a model for the mechanical checks: run `test_cmd`, `git diff --check`, the linter in the watcher and decide from exit codes | n/a | exit codes | yes | n/a | none | **adopt as the floor** — invariant 5 already says the test stage decides from exit codes; the model verifier adds judgement on top, never replaces this |

### 3.4 Fit against this repo's rules

Assessed against `AGENTS.md` invariants and the policy table in `docs/independent-verification.md:550-580`.

| Rule | Effect on the recommendation |
|---|---|
| Invariant 1 — status changes go through the store, `force` is recorded (`AGENTS.md:18-19`) | All four gates only *refuse* a move; a refusal is a normal `review → blocked` move, never `force`. Unchanged. |
| Invariant 2 — one ticket, one thread, claim failures reported (`AGENTS.md:20-21`) | Options 3–4 add a second thread per ticket. It must be recorded as `verify_thread_id`, not as `thread_id`, so invariant 2 still means one *dev* thread. |
| Invariant 3 — the profile comes from config, unknown profile blocks (`AGENTS.md:22-23`) | The verifier profile is a second named profile in the same config; G4 asserts the thread actually ran it. No model is invented. Matches §3 row 12. |
| Invariant 4 — approvals are one-shot (`AGENTS.md:24-25`) | Unaffected. Note §1.8: a mode that asks for approvals would create *T3* approvals, which this watcher cannot answer (see `bridge/t3_bridge.py:11-13`). Another reason to stay `full-access` and gate on evidence. |
| Invariant 5 — test stage decides from exit codes (`AGENTS.md:26`) | Option 6 keeps this as the floor; G1 proves the exit code still describes the committed tree. Strengthens, does not weaken. |
| Invariant 6 — reports are evidence, regenerated (`AGENTS.md:27-28`) | The fingerprint and `verify_tree` belong in the report, so a report can be re-checked later. |
| Invariant 7 — release pushes only a ticket branch, `[PASS]` required (`AGENTS.md:29-30`) | Moving `git add -A && commit` from `release()` (`watcher.py:296-298`) into the verification stage is compatible **only if** the commit is made on `ticket/T-xxxx-…` and the push stays in `release()`. The release precondition gains: `verify_tree == HEAD^{tree}`. |
| Invariant 8 — board writes need the token (`AGENTS.md:31-32`) | Unaffected. New refusal reasons are read-only GETs on the board. |
| Invariant 9 — T3 owns projects and threads; DB read-only (`AGENTS.md:33-34`) | Options 3–4 add no writes to T3's state at all — which is why they win over option 5. |
| §3 row 1 (`:559`) — start the verifier, store `verify_thread_id`/`verify_run_id` | Unchanged, plus `--worktree <sha>` and the fingerprint. |
| §3 row 3 (`:561`) — ACCEPT → `release()` | Now additionally requires G2 and G3 to pass; a failed gate is not ACCEPT. |
| §3 row 7 (`:565`) — verifier `failed/cancelled` → `blocked` | Unchanged. |
| §3 row 8 (`:566`) — `waiting` → `blocked` | This is the row that any forced narrow mode would land in. Keeping `full-access` avoids tripping it by design. |
| §3 row 9 (`:567`) — wall-clock cap → `blocked` | Still needed: with no tool restriction, a verifier can also loop. |

### 3.5 Concrete shape (proposal, `[inference]`)

Minimum additions to what exists:

- Config: `verification: {profile: "<name>", maxAttempts: 2, timeoutMinutes: 45, isolation:
  "worktree", requireUnchangedTree: true}`. `profile` resolves through the existing
  `tickets/catalog.py` path, so an unknown profile blocks (invariant 3).
- Store: `verify_thread_id`, `verify_run_id`, `verify_attempt`, `verify_tree`, `verify_fingerprint`,
  `verify_started_at`. Additive migration, same `MIGRATIONS` list (`tickets/store.py:85-93`).
- Watcher step, in `review`:
  1. remeasure the fingerprint; if it differs from the proof-time value → `blocked` (G1).
  2. commit on `ticket/T-xxxx-…`; record `verify_tree = HEAD^{tree}` (G2).
  3. `t3 thread start <project> - --provider … --model … --title … --worktree <verify_tree> --json`.
  4. store thread/run id; next step reads `t3 thread show --json --messages N`.
  5. on a terminal status, remeasure the project folder fingerprint; any difference → `blocked` (G3).
  6. require `worktreePath != null`, `runCount == 1`, mode/provider/model as configured (G4).
  7. only then parse `^VERDICT: (ACCEPT|REJECT)$` from the last assistant message of `verify_run_id`,
     and only then let `release()` push.

`[inference]` Step 2 restates §2C option 2 of `docs/independent-verification.md:456-459`; the new part is
that the fingerprint makes the "verifier saw the released tree" claim checkable instead of hopeful.

### 3.6 Residual risk, stated plainly

- A worktree is **not** a sandbox. `[code]` In `full-access` (§1.8) the pi extension returns before any
  tool gate, so a verifier with a shell can `cd` to the project folder, write to `$HOME`, or push. Only G3
  — a hash of the project folder — notices the first. Nothing notices the second. `[inference]`
- G3 detects a change but cannot attribute it. §1.10 shows a concurrent T3 thread committing to this very
  repo mid-research; a busy project will trip G3 for reasons that are not the verifier's fault. The
  correct response is still `blocked` with the diff of fingerprints in the note, so a human can tell which.
- If the owner wants real containment, it has to come from outside T3: a separate user, a container, or a
  filesystem-level read-only mount of the project folder. `[inference]` That is out of scope for a
  stdlib-only watcher on this host, and should be recorded as a known limitation rather than implied by
  naming the stage "read-only".
- The cheapest honest improvement while waiting: option 6 as the deciding gate, and the model verifier as
  an advisory second opinion whose *absence* blocks but whose *presence* never releases on its own.

---

## 4. Sources

### Commands (all read-only; no thread started, no ticket touched)

| Command | Purpose |
|---|---|
| `t3 --version` → `t3 v0.1.9` | version |
| `t3 --help`, `t3 thread --help`, `t3 thread start\|send\|show\|list --help` | full CLI surface |
| `t3 project add --help`, `t3 policy show`, `t3 environment link --help`, `t3 auth session issue --help`, `t3 auth pairing create --help`, `t3 pair --help` | every flag naming a mode, access or scope |
| `for c in …; do t3 $c --help \| grep -iE '^\s+--.*(mode\|approval\|sandbox\|tool\|permission\|policy\|isolat)'; done` | the §1.2 census |
| `t3 mcp` / `t3 help mcp` / `t3 --help \| grep -i mcp` | absence of an MCP subcommand |
| `curl -i -X POST http://127.0.0.1:3773/mcp …` | 401 + `www-authenticate` |
| `curl -sS http://127.0.0.1:3773/.well-known/oauth-protected-resource/mcp` | transport, scopes |
| `cat ~/.t3/userdata/server-runtime.json`, `settings.json`, `~/.t3/service.env` | live server, enabled providers |
| `ss -ltnp` / `ps -eo pid,etime,cmd` | pid 2153 `t3 serve` on :3773 |
| `python3` + `sqlite3` on `file:~/.t3/userdata/statev2.sqlite?mode=ro` | `runtime_mode` census, `pragma table_info`, tool-column census |
| `t3 thread show --json --messages 0 'mcp:72b322c1-…'` | `runtimeMode`/`worktreePath` visibility |
| `git status --short`, `git rev-parse HEAD`, `git log`, `git reflog`, `git worktree list`, `git remote -v` | §1.10 concurrency evidence, worktree state |
| `python3` sha256 over `git status --porcelain=v1 -z --untracked-files=all`, `git diff --binary`, `git diff --cached --binary` | G1 fingerprint |

### Binary, by `strings` line number

Line numbers are from `strings -a ~/.t3/runtime/versions/0.1.9/t3` (binary is `not stripped`) and are
stable for this build only.

- `1104012-1104016` sandbox-policy kind literals `read-only | workspace-write | danger-full-access`
- `1104017-1104029` `RuntimeMode$1`, `DEFAULT_RUNTIME_MODE$1`, `RUNTIME_MODE_RANK`
- `1104098-1104100` `orchestration:read`, `orchestration:operate`, `settings:write`
- `1104153-1104162` `legacyParents` folding write scopes up to `orchestration:operate`
- `1113578`, `1419912` `thread.runtime-mode.set` — the only runtime-mode setter, same four-value enum
- `1104236-1104249` `AuthMcpClientAccess`, `mcpClientScopes`
- `1125816` `delegate_task` `runtimeMode` description ("Narrow modes pause for human approval, not read-only")
- `1126323`, `1127710` `thread_credential_required` as a documented failure code
- `1129998-1130040` `CLIENT_GUARDED_RPC_SCOPES`
- `1222668`, `1222677` a read-only session's mode label
- `1255603-1255623` ACP `acpOperationDisposition` / `acpReadDisposition`
- `1323972-1324005` `sandboxPolicyKindForClaudeRuntimePolicy`, `readOnlyTools`
- `1324920`, `1325369` `CLAUDE_READ_ONLY_ALLOWED_TOOLS`, `CLAUDE_READ_ONLY_T3_MCP_ALLOWED_TOOLS`
- `1355206-1355232` `codexRuntimeModeTurnDefaults` (approval-required → `sandboxPolicy: readOnly`)
- `1398166`, `1398180-1398181` `PI_FILE_CHANGE_TOOLS`, pi extension `READ_ONLY_TOOLS` + `RuntimeMode`
- `1398461-1398476` pi `tool_call` gate
- `1411349` `clientModeCeiling`
- `1411403-1411410` `requireMcpCapability`, `threadCallerRequired`
- `1411426-1411500` `McpSessionRegistry` (`issue`/`resolve`), in-memory token `Map`
- `1487623-1487680` `addMcpHttpRoutes` (405/415/406/SSE)
- `1489544-1489565` `t3_thread_launch` schema, including `runtimeMode`
- `1495371` `path: "/mcp"`
- `1099679-1099707` `runAcpMcpCliFastPath` credential requirement
- `1443113` CLI `thread start` → `tools.call("t3_thread_launch", …)`

### Files on this machine

- `~/.t3/caches/pi-t3-mcp-extension.ts` — the pi bridge: `tool_call` gate, MCP client handshake
  (`mcp-protocol-version`, `mcp-session-id`).
- `~/.t3/userdata/server-runtime.json`, `~/.t3/userdata/settings.json`, `~/.t3/service.env` — measured above.
- `~/.t3/userdata/secrets/mcp-oauth-signing-key.bin` — exists (`[measured]` `find`), not opened.
- `~/.t3/worktrees/` — exists, holds `video-maker/` only; no worktree for this repo.

### Repository files, by line

- `AGENTS.md:18-34` — invariants 1–9 (quoted individually in §3.4).
- `docs/independent-verification.md:66-115` (CLI ↔ tool payload), `:169-210` (schema), `:439-467` (the
  three worktree options), `:468-500` (verdict channel), `:550-580` (policy decision table, rows 1/3/4/7/8/9),
  `:757-801` (source conventions this document follows).
- `watcher.py:46-67` (read-only DB access), `:104-122` (`_git`), `:205-207` (no `--worktree` today),
  `:256-318` (`release()` and its commit step), `:350-391` (budget sweep, `next_review`).
- `tickets/store.py:71-81` (approvals table), `:85-93` (`MIGRATIONS`), `:263-278` (`write_report`),
  `:296-306` (`attach_proof`), `:307-314` (`set_branch`).
- `tickets/catalog.py:122-178` (profile resolution — the only legal source of a provider/model).
- `bridge/t3_bridge.py:11-13` — "T3 has no CLI for answering pending approval requests", the measured
  constraint behind §3.4 invariant 4 and §1.8.
