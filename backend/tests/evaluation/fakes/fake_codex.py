#!/usr/bin/env python3
"""A stand-in for `codex exec` that speaks the Codex 0.153.4 event contract.

The event shapes here are copied from the documented contract for the supported
build -- `thread.started`, `turn.started`, `item.*` with a nested `item` object,
`turn.completed` with a `usage` object, `turn.failed` with a nested `error` --
so a green test means the parser handled real event shapes, not shapes invented
to suit it.

What these tests do NOT show: they say nothing about which tools the real CLI
offers, nothing about filesystem isolation, and nothing about how subscription
usage is metered. A fake process cannot demonstrate any of that.

The scenario is read from `scenario.json` in the working directory, because the
child environment is scrubbed down to five keys and carries no test channel.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ASSESSMENT = {
    "schema_version": 1,
    "classification": "INTERESTING",
    "strength": "MODERATE",
    "reason_codes": ["PRICE_AVAILABLE", "LIQUIDITY_PRESENT", "FIXTURE_DATA"],
    "data_gaps": [],
    "cited_observation_ids": [],
    "pair_id": "",
    "chain": "",
    "summary": "Fixture pair shows an available price and non-zero liquidity.",
}


def spawn_descendant(code: str) -> None:
    """Start a process in this process group with its pipes detached.

    Detaching matters: a descendant holding the inherited stdout would keep the
    parent's pipe open after the parent exits, and the reader would wait for an
    EOF that never comes. The point of these scenarios is the process group, not
    a stuck pipe.
    """
    subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def thread_started(thread_id: str = "11111111-2222-3333-4444-555555555555") -> None:
    emit({"type": "thread.started", "thread_id": thread_id})


def agent_message(text: str, item_id: str = "item_1") -> None:
    emit(
        {
            "type": "item.completed",
            "item": {"id": item_id, "type": "agent_message", "text": text},
        }
    )


DEFAULT_USAGE = {"input_tokens": 1200, "cached_input_tokens": 0, "output_tokens": 180}


def turn_completed(usage: object = "default") -> None:
    event: dict[str, object] = {"type": "turn.completed"}
    if usage == "default":
        event["usage"] = DEFAULT_USAGE
    elif usage is not None:
        event["usage"] = usage
    emit(event)


def answer(scenario: dict[str, object]) -> str:
    payload = dict(ASSESSMENT)
    payload["pair_id"] = scenario.get("pair_id", "")
    payload["chain"] = scenario.get("chain", "")
    payload["cited_observation_ids"] = scenario.get("observation_ids", [])
    overrides = scenario.get("assessment_overrides")
    if isinstance(overrides, dict):
        payload.update(overrides)
    return json.dumps(payload)


def observed_catalog_digest() -> str | None:
    """Hash the bytes `model_catalog_json` actually delivers, inside the child.

    A marker string would only say "nothing obviously bad arrived". A digest
    says the bytes are the ones the parent judged -- which also covers a short
    write, a truncation, extra bytes and any transformation in between.
    """
    for index, item in enumerate(sys.argv):
        if item == "-c" and index + 1 < len(sys.argv):
            override = sys.argv[index + 1]
            if override.startswith("model_catalog_json="):
                reference = override[len("model_catalog_json=") :].strip('"')
                try:
                    with open(reference, "rb") as handle:
                        return hashlib.sha256(handle.read()).hexdigest()
                except OSError:
                    return "<unreadable>"
    return None


def record_schema() -> None:
    """Record the reference and the bytes `--output-schema` actually delivers.

    Same reasoning as the catalog: the reference alone would only show that
    something was passed. The digest shows the child read the bytes the parent
    wrote, through a descriptor that has no name in any directory.
    """
    for index, item in enumerate(sys.argv):
        if item == "--output-schema" and index + 1 < len(sys.argv):
            reference = sys.argv[index + 1]
            try:
                with open(reference, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                digest = "<unreadable>"
            Path.cwd().joinpath("observed-schema.json").write_text(
                json.dumps({"reference": reference, "digest": digest}), encoding="utf-8"
            )
            return


def main() -> int:
    # Only the attempt carries a pinned catalog; the version and login probes
    # do not, and have nothing to compare.
    observed = observed_catalog_digest()
    record_schema()
    expected = Path.cwd() / "expected-catalog-digest.txt"
    if observed is not None and expected.exists():
        if observed != expected.read_text(encoding="utf-8").strip():
            # The bytes that arrived are not the bytes that were judged.
            sys.stderr.write(f"catalog digest mismatch: {observed}\n")
            return 9

    scenario_path = Path.cwd() / "scenario.json"
    scenario: dict[str, object] = json.loads(scenario_path.read_text(encoding="utf-8"))
    if sys.argv[1:4] == ["debug", "models", "--bundled"]:
        # Deliberately the opposite of what the pinned catalog says. Nothing in
        # the harness may consult this: a root session resolves ModelInfo
        # through the ModelsManager, not through the bundled dump, so a guard
        # that read this would be judging a catalog the turn never uses.
        sys.stdout.write(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": scenario.get("catalog_model", "gpt-5.4"),
                            "tool_mode": "code_mode_only",
                            "shell_type": "unified_exec",
                            "apply_patch_tool_type": "freeform",
                            "experimental_supported_tools": [],
                        }
                    ]
                }
            )
            + "\n"
        )
        return 0

    if sys.argv[1:2] == ["--version"]:
        # The build probe has its own scenario key so a test can pair any
        # reported version with any attempt answer.
        recorder = scenario.get("version_environment_out")
        if isinstance(recorder, str):
            Path(recorder).write_text(
                json.dumps(dict(os.environ), sort_keys=True), encoding="utf-8"
            )
        reported = scenario.get("version", "codex-cli 0.153.4")
        if reported is None:
            sys.stdout.write("codex-cli\n")
            return 0
        if reported == "__fail__":
            sys.stderr.write("cannot determine version\n")
            return 2
        sys.stdout.write(f"{reported}\n")
        return 0

    if sys.argv[1:3] == ["login", "status"]:
        # The login probe is a separate invocation with its own scenario key, so
        # a test can pair any login answer with any attempt answer.
        name = str(scenario.get("login", "login_status_chatgpt"))
    else:
        name = str(scenario.get("name", "success"))

    if name == "record_environment":
        Path(str(scenario["environment_out"])).write_text(
            json.dumps(dict(os.environ), sort_keys=True), encoding="utf-8"
        )
        thread_started()
        emit({"type": "turn.started"})
        agent_message(answer(scenario))
        turn_completed()
        return 0

    if name == "hang":
        Path(str(scenario["pgid_out"])).write_text(str(os.getpgid(0)), encoding="utf-8")
        thread_started()
        emit({"type": "turn.started"})
        time.sleep(600)
        return 0

    if name == "hang_with_grandchild":
        spawn_descendant("import time; time.sleep(600)")
        Path(str(scenario["pgid_out"])).write_text(str(os.getpgid(0)), encoding="utf-8")
        thread_started()
        emit({"type": "turn.started"})
        time.sleep(600)
        return 0

    if name == "parent_exits_grandchild_runs":
        # The leader finishes cleanly and leaves a descendant behind in the same
        # process group. Reaping the child alone would leave that descendant
        # running, which is exactly what cleanup has to catch.
        spawn_descendant("import time; time.sleep(600)")
        Path(str(scenario["pgid_out"])).write_text(str(os.getpgid(0)), encoding="utf-8")
        thread_started()
        emit({"type": "turn.started"})
        agent_message(answer(scenario))
        turn_completed()
        return 0

    if name == "hang_with_sigterm_immune_descendant":
        # Leader and descendant both outlive SIGTERM handling, so clearing the
        # group really has to wait out the grace period and then SIGKILL.
        spawn_descendant(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
        )
        Path(str(scenario["pgid_out"])).write_text(str(os.getpgid(0)), encoding="utf-8")
        thread_started()
        emit({"type": "turn.started"})
        time.sleep(600)
        return 0

    if name == "sigterm_immune_descendant":
        # The descendant ignores SIGTERM, so only SIGKILL ends it and only a
        # check after the fact can tell whether the group is really empty.
        spawn_descendant(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
        )
        Path(str(scenario["pgid_out"])).write_text(str(os.getpgid(0)), encoding="utf-8")
        thread_started()
        emit({"type": "turn.started"})
        agent_message(answer(scenario))
        turn_completed()
        return 0

    if name == "stdout_flood":
        thread_started()
        emit({"type": "turn.started"})
        filler = "x" * 900
        for index in range(5000):
            emit(
                {
                    "type": "item.updated",
                    "item": {"id": f"i{index}", "type": "reasoning", "text": filler},
                }
            )
        return 0

    if name == "long_line":
        sys.stdout.write("{" + '"pad":"' + "y" * 400_000 + '"}' + "\n")
        sys.stdout.flush()
        return 0

    if name == "stderr_flood":
        thread_started()
        emit({"type": "turn.started"})
        for _ in range(5000):
            sys.stderr.write("z" * 900 + "\n")
        sys.stderr.flush()
        time.sleep(5)
        return 0

    if name == "huge_message":
        thread_started()
        emit({"type": "turn.started"})
        agent_message("w" * 200_000)
        turn_completed()
        return 0

    if name == "turn_failed":
        thread_started()
        emit({"type": "turn.started"})
        emit({"type": "turn.failed", "error": {"message": "model refused"}})
        return 1

    if name == "no_final_message":
        thread_started()
        emit({"type": "turn.started"})
        turn_completed()
        return 0

    if name == "item_before_turn":
        thread_started()
        agent_message(answer(scenario))
        turn_completed()
        return 0

    if name == "message_without_turn_end":
        thread_started()
        emit({"type": "turn.started"})
        agent_message(answer(scenario))
        return 0

    if name == "no_usage":
        thread_started()
        emit({"type": "turn.started"})
        agent_message(answer(scenario))
        turn_completed(usage=None)
        return 0

    if name == "not_json":
        thread_started()
        emit({"type": "turn.started"})
        agent_message("I could not produce JSON.")
        turn_completed()
        return 0

    if name == "corrupt_line":
        thread_started()
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        return 0

    if name == "tool_activity":
        thread_started()
        emit({"type": "turn.started"})
        emit(
            {
                "type": "item.started",
                "item": {
                    "id": "c1",
                    "type": "command_execution",
                    "command": "ls",
                    "status": "in_progress",
                },
            }
        )
        emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "c1",
                    "type": "command_execution",
                    "command": "ls",
                    "exit_code": 0,
                    "aggregated_output": "",
                    "status": "completed",
                },
            }
        )
        emit(
            {
                "type": "item.completed",
                "item": {"id": "w1", "type": "web_search", "query": "anything"},
            }
        )
        agent_message(answer(scenario))
        turn_completed()
        return 0

    if name == "exit_nonzero":
        thread_started()
        emit({"type": "turn.started"})
        agent_message(answer(scenario))
        turn_completed()
        return 3

    if name == "two_messages":
        thread_started()
        emit({"type": "turn.started"})
        agent_message("a draft that is not the answer", item_id="item_0")
        agent_message(answer(scenario), item_id="item_1")
        turn_completed()
        return 0

    # `run_login_status` in codex-rs/cli/src/login.rs reports every outcome with
    # `eprintln!`, so stdout stays empty and the answer arrives on stderr. Both
    # logged-in cases exit 0, which is why the exit code alone cannot tell a
    # ChatGPT session from an API key.
    if name == "login_status_chatgpt":
        sys.stderr.write("Logged in using ChatGPT\n")
        return 0

    if name == "login_status_api_key":
        sys.stderr.write("Logged in using an API key - sk-ab...yz\n")
        return 0

    if name == "login_status_chatgpt_prefixed":
        # A longer status that merely starts with the marker. A substring test
        # would accept it; an exact line match must not.
        sys.stderr.write("Logged in using ChatGPT Enterprise workspace acme\n")
        return 0

    if name == "login_status_split_channels":
        # The marker only exists if the two channels are concatenated. Neither
        # stream ever emitted that line.
        sys.stdout.write("Logged in using ")
        sys.stdout.flush()
        sys.stderr.write("ChatGPT\n")
        return 0

    if name == "login_status_access_token":
        sys.stderr.write("Logged in using access token\n")
        return 0

    if name == "login_status_failure":
        sys.stderr.write("Not logged in\n")
        return 1

    thread_started()
    emit({"type": "turn.started"})
    agent_message(answer(scenario))
    turn_completed()
    return 0


if __name__ == "__main__":
    sys.exit(main())
