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

### The launch gate

The dangerous window is not a running child; it is a child that already exists
while `create_subprocess_exec` has not yet handed back a `Process`. asyncio
forks before it connects the pipes: in `unix_events._make_subprocess_transport`
the transport is created and only then is the pipe-connection waiter awaited,
and if that waiter fails the cleanup is `transp.close()` plus `await
transp._wait()` — and `BaseSubprocessTransport.close()` calls `self._proc.kill()`,
the direct child, not the group. The caller gets an exception and no handle.

So "the spawn raised" does **not** mean "nothing was started". Without a gate, a
Codex process could already be running, could already have started descendants,
and the parent would own nothing it could terminate or even name.

The child the probe starts is therefore not Codex. It is `launch_gate.py`,
started with `-I -S` so that no `PYTHONPATH`, user-site directory, `.pth` file
or `sitecustomize` runs in the window before the parent owns the handle. It
becomes the session and process group leader, and then does nothing but wait on
an inherited file descriptor. Codex is executed — via `os.execv`, keeping the
pid, the group and every pipe — only after the parent holds the `Process` handle
and has recorded the process group. If the transport fails first, the parent
closes its end instead; the gate reads EOF and exits without ever starting
Codex.

A failed spawn is consequently never reported as an empty group. It is
`UNVERIFIED`, because nothing looked. What makes it *safe* is the gate, not the
report.

### Ownership during an unfinished spawn

Cancelling a pending spawn does not undo it, for the same reason: asyncio kills
the direct child and a descendant started in that window survives. Once the
spawn task ends as cancelled the handle is gone, so no group is ever recorded,
signalled or checked for that tree.

The probe therefore waits for the spawn to settle, and the wait ends when the
spawn settles and at no other point. There is **no time cap and no cancellation
count**, because both are ways of walking away from a process that already
exists. The cost is small: a subprocess creation settles as soon as the kernel
has forked and the pipes are connected — it never waits for the child to do
anything. With a real Codex process, giving up instead would mean a tree that
keeps talking to the network after the harness believed it had stopped.

A caller's cancellation is **recorded, not obeyed**, and that holds for the
whole sequence — claiming the handle, recording the group, terminating the
leader, reaping it, clearing the group and verifying it is empty. Cleanup runs
as its own task and is awaited through a shield, so a cancellation delivered to
the caller never reaches it; each `CancelledError` is noted as owed, the task is
un-cancelled so the wait can resume, and the same cleanup task is waited on
again. Only when it has finished is the cancellation delivered.

Catching `CancelledError` inside the individual steps is not a substitute: that
only lets one step give up early, which is how a cancelled attempt could end
with `CHILD_NOT_REAPED` or an unverified group. Cancelling says "stop"; it does
not say "let go".

Waiting that long spends the cleanup reserve, so termination then gets a fresh
`EMERGENCY_CLEANUP_SECONDS` allowance. Without it `_terminate` would inherit an
exhausted deadline and could neither reap the child nor check its group: the
budget would be honoured and the process would survive. The overrun is reported
either way.

Two limits stay explicit: a descendant that called `setsid` has left the group
and is invisible here, and a group id can in principle be reused once the group
is empty, which is why the group is only signalled while it is known to exist.

## Filesystem and tool boundary

**`--sandbox read-only` is a write boundary, not a read boundary — and this is
now proven from the sources, not inferred.** Two facts settle it.

`protocol/src/protocol.rs` answers the read question for every policy variant
the same way:

```rust
pub fn has_full_disk_read_access(&self) -> bool {
    true
}
```

and `sandboxing/src/seatbelt.rs` acts on it:

```rust
let (file_read_policy, ...) = if file_system_sandbox_policy.has_full_disk_read_access() {
    ("; allow read-only file operations\n(allow file-read*)".to_string(), ...)
```

So the seatbelt profile generated for `read-only` contains a blanket
`(allow file-read*)`. The doc comment on `new_workspace_write_policy` says the
same thing in prose: "a policy that can read the entire disk". The policy
machinery does have `unreadable_roots` and `unreadable_globs`, but no
configuration key reaches them and `read-only` short-circuits past them.

Second: the seatbelt wraps **commands the agent runs**, not the agent. The
profile is assembled into a `/usr/bin/sandbox-exec` command line by
`sandboxing/src/manager.rs`. The Codex process itself runs with the caller's
ordinary privileges.

The read boundary therefore rests entirely on a different claim: no reading
tool is offered. That is a configuration property, not an operating-system one.

| Tool | How it is removed | Evidence |
|---|---|---|
| shell, `unified_exec`, `write_stdin` | `--disable shell_tool`, `--disable unified_exec` | `add_shell_tools` returns early when `Feature::ShellTool` is off (`core/src/tools/spec_plan.rs:1074-1082`) |
| `view_image` | `--disable view_image` | feature-gated in `add_core_utility_tools` |
| web search | `-c web_search="disabled"` plus `--disable standalone_web_search`, `web_search_cached`, `web_search_request` | see below |
| MCP resource tools | `-c mcp_servers={}` | `add_mcp_resource_tools` registers nothing without servers |
| apps, plugins, browser use, computer use, code mode, multi-agent | `--disable` per flag | feature flags in the config schema |
| **`apply_patch`** | **not removable** | gated only on an environment existing and on `model_info.apply_patch_tool_type`, which every model in the 0.153.4 catalog sets to `freeform`. Its spec is a grammar for patches, with no read operation, and `handlers/apply_patch.rs` refuses any path `can_write_path_with_cwd` rejects — under `read-only` that is every path. |
| **code mode** | **not removable by flag** | see below |

### Two toggles that did nothing

`tools.web_search=false` **does not disable web search.** That key is a
`WebSearchToolConfig` (domains, context size, location); its legacy boolean form
is parsed and then discarded —

```rust
Some(WebSearchToolConfigInput::Enabled(enabled)) => { let _ = enabled; None }
```

— and without an explicit mode `resolve_web_search_mode(...)` falls back to
`WebSearchMode::Cached` while the default provider advertises `web_search:
true`. The real control is the top-level `web_search` mode, so the harness sets
`web_search="disabled"` and disables the deprecated feature gates as well.

`tools.update_plan=false` was the wrong shape too: the type is
`UpdatePlanToolConfig { enabled }`, so the path is
`tools.update_plan.enabled=false`. And `experimental_request_user_input`
defaults to **enabled** — `.is_none_or(|config| config.enabled)` — so silence
there meant the tool was on. Both are now set explicitly.

#### What `use_responses_lite` does and does not do

An earlier version of this guard refused `use_responses_lite` models, arguing
standalone web search stayed reachable. That was wrong. The field does enter

```rust
standalone_web_search_enabled = namespace_tools_enabled
    && provider.capabilities().web_search
    && (model_info.use_responses_lite || Feature::StandaloneWebSearch)
```

but `append_extension_tool_executors` has the last word:

```rust
let web_search_mode_on = config.web_search_mode.value() != WebSearchMode::Disabled;
if is_standalone_web_search && (!standalone_web_search_enabled || !web_search_mode_on) {
    continue;
}
```

With `web_search="disabled"` that is false, so the standalone executor is
dropped whatever the model declares. Lite also *reduces* the surface elsewhere:
`hosted_model_tool_specs` returns `Vec::new()` immediately for a lite model. Its
remaining effect is on turn metadata. The guard type-checks the field and
accepts either value.

A test hands the complete override set to the supported build through `codex
debug models --bundled`, which loads the config and prints the bundled catalog
without a turn, a model request or a network call. Being accepted at the
argument layer is not the same as being understood.

### The model catalog outranks the flags

`core/src/tools/mod.rs`:

```rust
pub(crate) fn requested_tool_mode(turn_context, model_info) -> ToolMode {
    model_info.tool_mode.unwrap_or_else(|| { ...features... })
}
```

`unwrap_or_else` is the whole story: when the catalog entry names a tool mode,
the feature flags are never consulted. `effective_tool_mode` downgrades
`CodeMode` to `Direct` when code mode is unavailable but never touches
`CodeModeOnly`, and `register_code_mode_executors` gates on the mode rather than
on `Feature::CodeMode`.

`codex debug models --bundled` — an offline dump of the catalog compiled into
the binary — shows what that means for the shipped entries:

| model | `tool_mode` | consequence |
|---|---|---|
| `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, both `gpt-daybreak-*` | `code_mode_only` | code-mode executors registered **despite** `--disable code_mode` |
| `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.2` | *null* | falls through to the flags, so `Direct` |

Code mode is a local code-execution surface. A model that can run code can read
files, and no flag in `command.py` removes it from a `code_mode_only` model.

### Is `code_mode_only` a choice or a requirement?

`gpt-5.6-sol` is refused because its catalog entry declares
`tool_mode = "code_mode_only"`. Whether a reviewed static snapshot could
legitimately say `null` instead turns on what that field *is*. Five things in
0.153.4 answer that, and none of them was established by removing the field and
seeing what happened.

1. **The code calls it a selector, and `Direct` is one of its values.** The type
   is `Option<ToolMode>`, and `code_mode_warning_tests.rs` has a case named
   `does_not_warn_when_model_has_tool_mode_selector` that iterates
   `[Direct, CodeMode, CodeModeOnly]` as metadata a model may carry. The field
   expresses which surface to build, and "direct" is among the answers.
2. **It never reaches the server.** `tool_mode` appears nowhere in
   `ResponsesApiRequest`. There is no capability negotiation and no server-side
   validation that could reject a turn for running the model without code mode.
3. **This model's instructions do not assume code mode.** Its
   `base_instructions` are 17 730 characters and mention code mode, JavaScript,
   Node and REPL exactly zero times, while mentioning `apply_patch` three times
   and `shell` four — the direct surface.
4. **There is no code-mode variant of the instructions.** `model_messages`
   carries one `instructions_template`, used either way.
5. **Nothing errors or warns in this direction.**
   `unsupported_code_mode_warning` fires only when code mode is enabled *by
   feature* while `model_info.tool_mode.is_none()`. With a catalog saying `null`
   and every code-mode feature disabled, the condition is false.

So the answer is **(A), a catalog-chosen tool configuration** — not a capability
the model or the request requires.

Two caveats belong with that answer. Codex's only statement about a mismatch
between features and model metadata is "**This may degrade model performance**",
and that is for the mirror case; the symmetric claim is plausible but not
demonstrated. And "code_mode_only model, direct surface" is an *unexercised*
path in 0.153.4 rather than a proven-good one. Overriding vendor metadata is a
quality and support question even where it is not a correctness one — though for
a single structured classification with no tools at all, agentic coding
performance is not what this harness is asking the model for.

This settles what the field is. It does not select a model.

### Judging the catalog the turn actually uses

Reading the *bundled* dump would not have been enough, and that was a real hole.
A root session resolves `ModelInfo` through the `ModelsManager` with
`RefreshStrategy::OnlineIfUncached`, so a fresh cache entry or a remote
`/models` response could carry a different `tool_mode` than the catalog compiled
into the binary. Inspecting one catalog while the turn runs against another is
not a check.

The CLI supports pinning, and the harness uses it.
`model-provider/src/provider.rs`:

```rust
fn models_manager(&self, codex_home, config_model_catalog) -> SharedModelsManager {
    match config_model_catalog {
        Some(model_catalog) => Arc::new(StaticModelsManager::new(auth, model_catalog)),
        None => { /* OpenAiModelsEndpoint: cache, then remote */ }
    }
}
```

`StaticModelsManager::raw_model_catalog` ignores the refresh strategy entirely,
returns the catalog it was constructed with, and implements `refresh_if_new_etag`
as a no-op. `config_model_catalog` reaches it from `model_catalog_json` through
`load_model_catalog` and `thread_manager::build_models_manager`; the only call
site that hardcodes `None` is a test helper.

So every attempt pins the catalog — and pins the **bytes**, not the name. Same
pathname is not same content: a path can be replaced between the guard's read
and the child's read, and a check with a window in it is not a check.

There are two ways to change a file after it has been judged, and holding the
operator's own descriptor only stops one of them. A descriptor binds to an
**inode**, not to bytes: `open(path, O_WRONLY|O_TRUNC)` followed by a write
changes what an already-open read handle sees. Replacing the path is the other
way. An earlier version of this harness handled only the second.

So the judged bytes are frozen rather than referenced, and every step that could
leave the copy different from what was judged is checked rather than assumed:

1. the operator file is read once, bounded by `MAX_CATALOG_BYTES`, and decoded
   as **strict** UTF-8 — `load_catalog_json` uses `read_to_string`, so repairing
   invalid bytes here would mean judging a text the runtime loader never sees;
2. those exact bytes are hashed and judged;
3. they are written to a fresh file in a **write-all loop** — one `os.write` is
   not a promise to write everything, and a write that makes no progress is an
   error;
4. `fsync`, then the writer is closed;
5. the copy is reopened **read-only**;
6. it is **unlinked, and the unlink must succeed** — a snapshot still reachable
   by name is not immutable, so a failure here refuses the attempt rather than
   being swallowed;
7. the finished file is **read back and hashed** through that descriptor and
   compared with the judged digest, which catches a truncation, extra bytes or
   any transformation in between;
8. the descriptor is rewound.

Only then does a snapshot exist. What the child inherits through `pass_fds` has no name left to
write through and carries no write capability of its own.
`model_catalog_json` points at `/dev/fd/<n>`, which resolves through that open
file description, and the descriptor survives the gate's `os.execv` because
`pass_fds` clears `FD_CLOEXEC`. Regressions mutate the operator file both ways —
in place on the same inode, and by atomic replacement — and the child still
reads the reviewed bytes; the fake verifies that from inside the child rather
than the parent asserting it about itself.

A descriptor reference requires `PLATFORM_BINARY`. Our own gate execs that
binary and keeps the descriptor; the npm entry point spawns a separate Node
process, and nothing shows Node forwards a descriptor it was never told about.
That case is refused rather than assumed.

The approved content is pinned by digest as well. `expected_catalog_sha256`
makes the whole `ModelInfo` snapshot part of the contract instead of the few
fields this guard samples, so changing the catalog becomes a reviewable change
rather than an edit to an operational file.

The digest is **always** mandatory. It used to be optional with a run mode
deciding when it mattered, which meant a caller setting the wrong mode could
have reached a real turn without a pin — a security property behind a flag is
not a security property. `CodexClientConfig` now rejects a missing or malformed
digest at construction, so no combination of it carries one as far as `exec`,
and fixtures are pinned exactly like anything else.

The child verifies this too, by digest rather than by looking for a bad string.
It hashes whatever `model_catalog_json` delivers and compares that with the
snapshot digest, so the end-to-end regression covers a short write, a
truncation, extra bytes and any transformation — not merely the absence of a
marker.

The guard refuses anything outside an explicit allowlist: a declared
`tool_mode`, a non-empty `experimental_supported_tools`, an unknown
`apply_patch_tool_type`, a model missing from the catalog, a file that cannot be
read, and any field of the wrong type. A wrong type is never read as absent:
`{"unexpected": "shape"}` is not `null`, and treating it as `null` would turn a
parsing accident into a permission.

None of this is observable from a model's reply. A tool that was offered and
never invoked emits no event, so "the run looked clean" is not evidence about
what was on offer.

The workspace is a fresh empty directory outside the repository, and
`build_arguments` refuses a workspace inside a forbidden root. Instruction
sources are closed with `--ignore-user-config`, `--ephemeral`,
`-c project_doc_max_bytes=0` and `-c skills.include_instructions=false`.

`--ignore-rules` is deliberately **not** used: `.rules` are execpolicy files
that restrict commands, so ignoring them loosens the run. Managed requirements
are a separate configuration layer that neither `--ignore-user-config` nor
`--ignore-rules` reaches, and the harness does not attempt to bypass them. No
`--dangerously-bypass-*` flag is ever emitted.

## The outer read boundary (macOS)

Codex's `--sandbox read-only` restricts writes and nothing else, and the profile
it builds wraps the commands the agent runs rather than the agent. So the
harness puts **its own** Seatbelt profile in front of Codex:

```
harness -> launch_gate -> /usr/bin/sandbox-exec -f <profile> -- codex ...
```

`sandbox-exec` applies the profile and `exec`s in the same process, so the
gate's pid, its process group and every inherited descriptor — including the
pinned catalog — carry through unchanged. Nothing about the ownership
architecture moves.

The profile denies by default. Its platform section is Codex's own vetted
minimum for a sandboxed process on macOS, copied verbatim so the harness does
not re-derive it; the harness section allows exactly three roots:

| root | why |
|---|---|
| `CODEX_VENDOR` | the binary and the resources it ships with |
| `WORKSPACE` | the empty evaluation workspace, read-only |
| `CODEX_HOME` | the isolated home, holding the login state and nothing else |

plus `/dev/fd` for the pinned catalog. Not `HOME`, not `/Volumes`, not the
repository. Paths are **resolved** before they reach the profile, because
Seatbelt matches the real filesystem and `/tmp` would never match
`/private/tmp`.

The boundary is tested with sentinel files standing in for the repository,
`HOME` and credential classes — reading an actual `.env` would prove one path
instead of a class, and would put a real secret in a test. `codex --version`
was confirmed to start under the profile, offline and without any request.

There is no fallback. On macOS the outer sandbox is required; where it cannot be
established the release gate fails. Linux would need bwrap or landlock and is
not implemented.

### The isolated CODEX_HOME

Pointing the attempt at `~/.codex` would put config, history, sessions, skills,
plugins and caches inside the boundary. Instead the harness builds a `0700`
directory holding a `0600` copy of the one file 0.153.4 reads for a ChatGPT
session, `auth.json`, and discards it afterwards. No token value is read into
the harness, logged, asserted on or reported; a refresh during an attempt lands
in the copy, so the user's own session is untouched.

Whether that copied state is *sufficient* to authenticate cannot be settled
offline. `AUTH_HOME_ISOLATION` is therefore `UNVERIFIED`, not `PASS`, and a real
run stays refused until one is separately authorised.

## Release gates

`release.evaluate_release` answers, without contacting a model, whether a real
turn could proceed: `MODEL_CATALOG`, `CATALOG_DIGEST`, `CODEX_VERSION`,
`CHATGPT_SESSION`, `TOOL_SURFACE`, `OUTER_READ_SANDBOX`, `AUTH_HOME_ISOLATION`.

Each is `PASS`, `FAIL` or `UNVERIFIED`, and `may_run` is true only when every
gate is `PASS`. There is no "warning but continue" — `OUTER_READ_SANDBOX` and
`CATALOG_DIGEST` in particular have no degraded mode, and `UNVERIFIED` is its
own answer rather than a soft pass.

## Open blockers

Three claims remain **unproven**, and no test in this suite can prove them:

1. **Which tools the CLI actually offers.** The table above is read from source,
   not measured. `observed_tool_activity` records tool *use*; an offered but
   unused tool emits no event, so an empty result is not evidence of a
   tool-free run.
2. **The read boundary is now examined, and there is none.**
   `SandboxPolicy::has_full_disk_read_access` returns `true` for every variant
   and `seatbelt.rs` turns that into a blanket `(allow file-read*)`, so
   `read-only` restricts writes and nothing else. The seatbelt also wraps
   commands the agent runs rather than the agent itself. What remains is not an
   open question but a stated limit: read safety rests on no reading tool being
   offered, which is a configuration property rather than an enforced one.
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
