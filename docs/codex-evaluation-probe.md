# Codex evaluation probe

An offline harness that sends an explicitly provided market context to the
official Codex CLI under a ChatGPT login and gets back a locally validated,
ORBIT-shaped assessment.

**Its result is a test artifact.** It is not workflow evidence. It carries no
worker claim, no lease and no supersession, it never reaches the risk engine or
an executor, and nothing in the runtime can import it.

Nothing in this document authorises a real CLI invocation. Every test in the
suite drives a fake process.

## Why this is not a `ReasoningProvider`

`src/reasoning/provider.py` promises three things the Codex CLI cannot deliver:

| Promise in the port | Why Codex 0.153.4 cannot keep it |
|---|---|
| `max_output_tokens` bounds generation | No output-token key exists anywhere in `core/config.schema.json` |
| No tool, filesystem or credential surface | `apply_patch` is registered whenever the model catalog sets `apply_patch_tool_type`; no config disables it |
| A bounded, typed provider failure | `request_max_retries` and `stream_max_retries` cannot be overridden for the built-in `openai` provider: `merge_configured_model_providers` uses `entry(key).or_insert(...)`, so a same-named entry is silently dropped |

The reasoning contract was therefore left exactly as it is, and the harness
promises less rather than the port promising something untrue. The decisive
choice is that `EvaluationRequest` has **no** output-token field at all. A field
that is only logged and then ignored is worse than a field that does not exist.

## What the harness does promise

Three things, each enforced locally:

1. **At most one `codex exec` start.** No resume, no restart after any outcome.
   A second call to `evaluate` is refused, not retried. The version probe and
   the login probe are separate processes, each with its own counter and its own
   budget.
2. **One monotonic deadline**, started before the process is spawned, covering
   spawn, stdin, both pipes, parsing, schema validation and domain validation,
   with a slice reserved up front for cleanup.
3. **Bounded reading of every channel.** stdout and stderr are drained
   concurrently and capped separately, a single line has its own cap, and the
   one payload the parser retains has a cap distinct from the stream caps.

### What the deadline governs, and what it does not

The deadline bounds **work**: how long the attempt may run and whether a result
may still be accepted. It does not bound **ownership recovery or cleanup**.
Making sure no process is left running is allowed to take longer than the
attempt was given, so a spawn recovery can push one attempt past its total wall
clock. That is deliberate. Nothing here should be read as a hard overall
wall-clock guarantee — the guarantee is about accepting results, not about
finishing.

Schema parsing and the injected domain validator are ordinary synchronous
calls. The event loop cannot preempt them, so a validator that runs long is not
cut short. The deadline bounds when a result may be accepted, not how long every
step may occupy the thread. Saying otherwise would claim an interruptibility
asyncio does not provide.

What it does instead is stop at every boundary where control comes back: before
parsing, between schema validation and domain validation, and after both. A
schema validation that overruns therefore does not fund a domain validator that
could only overrun further; the attempt is rejected at the first observable
point with `SCHEMA_VALIDATION_OVERRAN`.

## What it explicitly does not promise

- the number of model requests in a turn — `core/src/client.rs` states a turn
  streams "one or more Responses API requests", and websocket prewarm counts as
  a connection attempt of its own;
- the number of HTTP attempts — the defaults are `request_max_retries = 4` and
  `stream_max_retries = 5`, and neither can be lowered for the built-in
  provider;
- the number of generated tokens — no such setting exists.

These numbers are unknown. The harness reports them as unknown and never
substitutes a value it did not observe.

## Authentication

Only the official ChatGPT login path. No auth file is opened, copied or parsed,
and no token is read into the Python process.

The login probe reads **stderr as well as stdout**. In 0.153.4,
`run_login_status` (`codex-rs/cli/src/login.rs`) reports every outcome with
`eprintln!`, so the status line never appears on stdout. The exit code is no
substitute: an API-key session exits 0 just like a ChatGPT one.

The match is an **exact line**, and the two channels are kept apart. A substring
test would accept the marker quoted inside an error message or as the prefix of
a longer status such as `Logged in using ChatGPT Enterprise workspace acme`, and
concatenating the buffers could splice one stream's tail onto the other's head
and manufacture a line neither ever emitted. Only `Logged in using ChatGPT`, as
a whole line on one channel, counts as a subscription session — access token,
personal access token and Bedrock modes are logged-in states too, and none of
them is what this probe is for.

Two layers, in this order:

1. **The child environment is built from nothing** — exactly `CODEX_HOME`,
   `HOME`, `PATH`, `TMPDIR`, `LANG`. Three variables can substitute a credential
   (`OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`) and three can
   redirect an auth endpoint (`CODEX_REFRESH_TOKEN_URL_OVERRIDE`,
   `CODEX_REVOKE_TOKEN_URL_OVERRIDE`, `CODEX_APP_SERVER_LOGIN_CLIENT_ID`). A
   list that starts empty cannot forget one.
2. **`forced_login_method="chatgpt"`** confirms the choice.

The order is not cosmetic. `login/src/auth/manager.rs` logs out when a forced
method conflicts with an active API-key session, so an inherited key combined
with a forced method could destroy a stored ChatGPT login. The allowlist
prevents the conflict from arising; the forced method only restates it.

`shell_environment_policy` is not used for this and would not help: it governs
the environment Codex hands to shell-like tools, not the environment Codex
itself runs in.

### Launcher

The npm entry point is a `#!/usr/bin/env node` script that re-spawns the real
binary, so it cannot start unless `node` is on the PATH given to the child. The
self-contained platform binary needs no interpreter. `LauncherKind` makes the
difference explicit and `validate_launcher` refuses a shim the minimal
environment could not start, instead of quietly widening the environment.

## Process group cleanup

Cleanup targets the **group**, not just the direct child. The group id is
recorded at spawn time and kept, because `os.getpgid` stops answering once the
leader is gone.

A leader exiting on its own settles nothing: a process group outlives its
leader as long as any member is left, so a child that spawned a descendant and
then exited cleanly leaves that descendant running. Cleanup therefore signals
and then re-checks the group whatever the leader did, and `CleanupReport.group`
records what the check found:

| State | Meaning |
|---|---|
| `EMPTY` | the group was checked and nothing is left in it |
| `OCCUPIED` | something survived SIGTERM and SIGKILL within the budget |
| `UNVERIFIED` | the cleanup budget ran out before the group could be checked |

`UNVERIFIED` is not a synonym for success. Signals are cheap and were still
delivered, but nothing confirmed they took effect.

The report answers two independent questions and never lets one answer the
other:

- `group_cleared` — the child was reaped and the group was checked and found
  empty;
- `overran_reserve` — cleanup used up the time it was allowed. Read once, at
  the end, from the clock alone. A successful outcome does not clear it, so a
  group cleared late is still reported as cleared late.

`complete` is both: cleared, and cleared in time.

Cleanup runs on its own `CleanupBudget` rather than on the attempt's deadline,
because after a spawn overrun the deadline is already at zero and terminating a
process needs time that no longer exists there.

### Ownership during an unfinished spawn

The dangerous window is not a running child; it is a child that already exists
while `create_subprocess_exec` has not yet handed back a `Process`. Cancelling
there does not undo the spawn. asyncio closes the transport, which calls `kill`
on the direct child — not `killpg` — and a descendant started in that window
survives it. Once the spawn task ends as cancelled the handle is gone, so no
group is ever recorded, signalled or checked for that tree.

The probe therefore waits for the spawn to settle, and the wait ends when the
spawn settles and at no other point. There is **no time cap and no cancellation
count**, because both are ways of walking away from a process that already
exists. The cost is small: a subprocess creation settles as soon as the kernel
has forked and the pipes are connected — it never waits for the child to do
anything. With a real Codex process, giving up instead would mean a tree that
keeps talking to the network after the harness believed it had stopped.

A caller's cancellation is **recorded, not obeyed**. Each `CancelledError` is
absorbed, the task is un-cancelled so recovery can keep awaiting, and the
cancellation is delivered to the caller once the child has been terminated and
its group checked. Cancelling says "stop"; it does not say "let go".

Waiting that long spends the cleanup reserve, so termination then gets a fresh
`EMERGENCY_CLEANUP_SECONDS` allowance. Without it `_terminate` would inherit an
exhausted deadline and could neither reap the child nor check its group: the
budget would be honoured and the process would survive. The overrun is reported
either way.

Two limits stay explicit: a descendant that called `setsid` has left the group
and is invisible here, and a group id can in principle be reused once the group
is empty, which is why the group is only signalled while it is known to exist.

## Filesystem and tool boundary

**`--sandbox read-only` is a write boundary, not a read boundary.** It does not
stop a reading tool from opening `.env`, a database configuration or
`~/.codex/auth.json`. Treating the two as the same thing would be the mistake.

The read boundary rests on a different claim: no reading tool is offered.

| Tool | How it is removed | Evidence |
|---|---|---|
| shell, `unified_exec`, `write_stdin` | `--disable shell_tool`, `--disable unified_exec` | `add_shell_tools` returns early when `Feature::ShellTool` is off (`core/src/tools/spec_plan.rs:1074-1082`) |
| `view_image` | `--disable view_image` | feature-gated in `add_core_utility_tools` |
| web search | `-c tools.web_search=false`, `--disable standalone_web_search` | `tools.web_search` in the config schema |
| MCP resource tools | `-c mcp_servers={}` | `add_mcp_resource_tools` registers nothing without servers |
| apps, plugins, browser use, computer use, code mode, multi-agent | `--disable` per flag | feature flags in the config schema |
| **`apply_patch`** | **not removable** | gated only on an environment existing and on `model_info.apply_patch_tool_type` |

The workspace is a fresh empty directory outside the repository, and
`build_arguments` refuses a workspace inside a forbidden root. Instruction
sources are closed with `--ignore-user-config`, `--ephemeral`,
`-c project_doc_max_bytes=0` and `-c skills.include_instructions=false`.

`--ignore-rules` is deliberately **not** used: `.rules` are execpolicy files
that restrict commands, so ignoring them loosens the run. Managed requirements
are a separate configuration layer that neither `--ignore-user-config` nor
`--ignore-rules` reaches, and the harness does not attempt to bypass them. No
`--dangerously-bypass-*` flag is ever emitted.

## Open blockers

Three claims remain **unproven**, and no test in this suite can prove them:

1. **Which tools the CLI actually offers.** The table above is read from source,
   not measured. `observed_tool_activity` records tool *use*; an offered but
   unused tool emits no event, so an empty result is not evidence of a
   tool-free run.
2. **How far the sandbox restricts reading.** Unexamined. The harness relies on
   no reading tool being offered, which is a configuration argument.
3. **`apply_patch` stays on offer.** Its effect is blocked by the read-only
   sandbox; the offer is not.

Subscription metering, quotas and the admissibility of automated subscription
use are likewise unverified. They are contract questions, not code questions.

**A real run would not settle any of this either.** A successful invocation
shows that one attempt produced one validated answer. It does not establish the
complete set of tools the model was offered, and it does not establish
filesystem isolation in general. `observed_tool_activity` evidences observed
activity and nothing beyond it: a tool that was offered and left unused emits no
event, so no number of quiet runs adds up to a proof of absence. Establishing
the tool surface or the read boundary needs a different method than running the
probe and reading its result -- and that method is not part of this harness.

## Version binding

`SUPPORTED_CLI_VERSION = "0.153.4"`, and the build is **measured, not
configured**. Before anything else runs, a separate counted and bounded probe
asks the launcher on disk (`codex --version`) which build it is, and its answer
decides. A configured version string would only record an expectation: a stale
or edited setting would let this harness run against a CLI whose event contract,
default tools and flag names it has never seen.

An unreadable version is treated the same as a wrong one. No attempt starts.

This matters because argument tests bound only our own argument construction — a
later build could enable a tool by default, rename a flag or ignore an override
and every argument assertion would still pass. The measured version, not the
argument test, is what stops a run against a build this harness never saw.

## Validation

Local validation is authoritative in both stages:

1. `output_model.model_validate(...)` — shape;
2. an **injected** domain validator that holds the original task input bound in
   its closure — content.

The generic client imports nothing from `src.agents`. The ORBIT test supplies
`validate_assessment(output, task_input)` against a synthetic `OrbitTaskInput`
marked `is_fixture=True`. Domain-invalid output can never produce an
`EvaluationCompleted`.

Success also requires a consistent ending: the turn started, an `agent_message`
item completed, `turn.completed` arrived, and the process exited zero. An
earlier `agent_message` on its own is not a result.

## Schema handling

Strict structured output is always on (`output_schema_strict` defaults to `true`
in `core/src/client_common.rs`, with no flag to turn it off). `strict_schema`
therefore applies exactly the two documented requirements — every property in
`required`, objects closed with `additionalProperties: false` — recursively and
without mutating the caller's schema.

It deliberately does not:

- strip `pattern`, `minLength`, `maxLength`, `minItems`, `maxItems` or `format`,
  because nothing shows strict mode rejects them;
- turn a defaulted field into a nullable one, which would change its meaning;
- close a free-form mapping — `dict[str, object]` means "any keys", so such a
  model is refused with `SCHEMA_UNSUPPORTED` rather than silently narrowed.

## Running the tests

```
cd backend
uv run pytest tests/evaluation
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

No PostgreSQL, no migration, no network, no credential, no model call.
