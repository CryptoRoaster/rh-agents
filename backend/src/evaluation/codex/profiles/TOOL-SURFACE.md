# Expected model-visible tool surface — Codex CLI 0.153.4

**SOURCE-DERIVED EXPECTATION.** Every line below was read out of
`core/src/tools/spec_plan.rs` and the surrounding modules for the exact flag set
`command.py` emits, together with the pinned `gpt-5.5` catalog entry. Nothing
here was observed at runtime. A quiet run would not confirm it either: the
documented event stream reports tool *use*, and a tool that is offered and never
invoked emits nothing at all.

## Expected present

- `apply_patch` — registered whenever an environment exists and
  `model_info.apply_patch_tool_type` is set, which every 0.153.4 catalog entry
  does (`freeform`). It has no read operation in its spec, and
  `handlers/apply_patch.rs` refuses any path `can_write_path_with_cwd` rejects,
  which under `--sandbox read-only` is every path.

## Expected absent, and why

| surface | why it is not registered |
|---|---|
| shell, `unified_exec`, `write_stdin` | `add_shell_tools` returns on `!Feature::ShellTool` |
| code mode executors | `register_code_mode_executors` needs `CodeMode`/`CodeModeOnly`; the pinned entry has `tool_mode: null` and both features are off |
| web search (hosted and standalone) | `web_search="disabled"` makes `web_search_mode_on` false; the entry is not responses-lite; the three search features are off |
| image generation | `image_generation_available` needs `Feature::ImageGeneration`, disabled |
| `view_image` | `Feature::ViewImage`, disabled |
| `update_plan` | `tools.update_plan.enabled=false` |
| `request_user_input`, `request_user_input_async` | `tools.experimental_request_user_input.enabled=false`; the entry advertises no `experimental_supported_tools` |
| `request_permissions` | `Feature::RequestPermissionsTool`, disabled |
| `get_context_remaining`, `new_context_window` | `Feature::TokenBudget`, disabled |
| `current_time` | `Feature::CurrentTimeReminder` off and the entry advertises no `clock` |
| `sleep` | `Feature::SleepTool`, disabled |
| MCP resource tools | `add_mcp_resource_tools` needs servers; `mcp_servers={}` |
| apps, plugins, plugin install | `Feature::Apps`, `Feature::Plugins`, `Feature::ToolSuggest`, all disabled |
| browser use, computer use | both features disabled |
| multi-agent (v1 and v2) | `Feature::MultiAgent`, `Feature::MultiAgentV2`, disabled |
| skills, memories, goals, hooks | `Feature::SkillSearch`, `Feature::Memories`, `Feature::Goals`, `Feature::Hooks`, disabled; `skills.include_instructions=false` |
| deferred executor, fanout, context management | all three features disabled |
| tool search | needs a deferred tool with search info; none is registered |

## What this does not claim

That the request sent to the API contains exactly this list. Confirming that
needs the real invocation, which is separately authorised and has not happened.
The catalog guard and the flag set are what make the expectation checkable in
advance; this file records the expectation, not an observation.
