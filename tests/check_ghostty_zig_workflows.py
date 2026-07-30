#!/usr/bin/env python3
"""Require Ghostty initialization before workflow steps execute Zig consumers."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import bashlex
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


CONSUMER_NAMES = (
    "scripts/install-zig-ci.sh",
    "scripts/build-ghostty-cli-helper.sh",
    "scripts/ghostty-zig-version.sh",
)
CHECKOUT_ACTION = "actions/checkout@"
SETUP_ZIG_ACTION = "mlugg/setup-zig@"
SETUP_ZIG_VERSION = "${{ steps.ghostty-zig-version.outputs.version }}"
SHELL_INTERPRETERS = {"bash", "sh", "zsh"}
COMMAND_WRAPPERS = {"exec", "sudo", "time"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
QUOTED_HEREDOC = re.compile(r"(<<-?\s*)(['\"])([A-Za-z_][A-Za-z0-9_]*)(\2)")
HEREDOC = re.compile(
    r"<<(?P<strip_tabs>-?)\s*(?P<quote>['\"]?)"
    r"(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"
)


@dataclass(frozen=True)
class Step:
    line: int
    name: str
    identifier: str | None
    uses: str | None
    run: str | None
    inputs: dict[str, str]


@dataclass(frozen=True)
class Job:
    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class RunEvent:
    kind: str
    line: int
    consumer: str | None = None


def _mapping_value(mapping: Node | None, key: str) -> Node | None:
    if not isinstance(mapping, MappingNode):
        return None
    for key_node, value_node in mapping.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return value_node
    return None


def _scalar(mapping: Node | None, key: str) -> str | None:
    value = _mapping_value(mapping, key)
    return value.value if isinstance(value, ScalarNode) else None


def _scalar_mapping(mapping: Node | None) -> dict[str, str]:
    if not isinstance(mapping, MappingNode):
        return {}
    values: dict[str, str] = {}
    for key_node, value_node in mapping.value:
        if isinstance(key_node, ScalarNode) and isinstance(value_node, ScalarNode):
            values[key_node.value] = value_node.value
    return values


def _workflow_jobs(path: Path) -> tuple[list[Job], list[str]]:
    try:
        root = yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        return [], [f"{path.name}: invalid workflow YAML: {error}"]

    jobs_node = _mapping_value(root, "jobs")
    if jobs_node is None:
        return [], []
    if not isinstance(jobs_node, MappingNode):
        return [], [f"{path.name}: jobs must be a mapping"]

    jobs: list[Job] = []
    failures: list[str] = []
    for job_name_node, job_node in jobs_node.value:
        if not isinstance(job_name_node, ScalarNode):
            failures.append(f"{path.name}: job name must be a scalar")
            continue
        if not isinstance(job_node, MappingNode):
            continue

        steps_node = _mapping_value(job_node, "steps")
        if steps_node is None:
            continue
        if not isinstance(steps_node, SequenceNode):
            failures.append(
                f"{path.name}:{steps_node.start_mark.line + 1}: "
                f"{job_name_node.value} steps must be a sequence"
            )
            continue

        steps: list[Step] = []
        for step_node in steps_node.value:
            if not isinstance(step_node, MappingNode):
                failures.append(
                    f"{path.name}:{step_node.start_mark.line + 1}: "
                    f"{job_name_node.value} step must be a mapping"
                )
                continue
            steps.append(
                Step(
                    line=step_node.start_mark.line + 1,
                    name=_scalar(step_node, "name") or "<unnamed step>",
                    identifier=_scalar(step_node, "id"),
                    uses=_scalar(step_node, "uses"),
                    run=_scalar(step_node, "run"),
                    inputs=_scalar_mapping(_mapping_value(step_node, "with")),
                )
            )
        jobs.append(Job(name=job_name_node.value, steps=tuple(steps)))
    return jobs, failures


def _normalize_heredocs(script: str) -> str:
    # bashlex 0.18 parses heredocs but retains quotes in the expected delimiter,
    # unlike Bash. Removing only those delimiter quotes preserves line numbers
    # and leaves heredoc bodies represented as data, not executable commands.
    return QUOTED_HEREDOC.sub(
        lambda match: (
            match.group(1)
            + match.group(3)
            + (" " * (len(match.group(2)) + len(match.group(4))))
        ),
        script,
    )


def _child_nodes(node: object) -> Iterator[object]:
    for value in vars(node).values():
        if hasattr(value, "kind"):
            yield value
        elif isinstance(value, list):
            yield from (item for item in value if hasattr(item, "kind"))


def _commands_in_execution_order(node: object) -> Iterator[object]:
    if getattr(node, "kind", None) == "command":
        # Expansions and redirections execute before their containing command.
        for child in _child_nodes(node):
            yield from _commands_in_execution_order(child)
        yield node
        return
    for child in _child_nodes(node):
        yield from _commands_in_execution_order(child)


def _command_words(command: object) -> list[str]:
    return [
        part.word
        for part in getattr(command, "parts", ())
        if getattr(part, "kind", None) == "word"
    ]


def _skip_options(words: list[str], index: int) -> int:
    while index < len(words) and words[index].startswith("-"):
        index += 1
    return index


def _executed_target(words: list[str]) -> str | None:
    if not words:
        return None

    index = 0
    if words[index] == "env":
        index += 1
        index = _skip_options(words, index)
        while index < len(words) and ASSIGNMENT.match(words[index]):
            index += 1

    while index < len(words) and words[index] in COMMAND_WRAPPERS:
        index += 1
        index = _skip_options(words, index)

    if index >= len(words):
        return None
    if words[index] == "command":
        index += 1
        if index < len(words) and words[index] in {"-v", "-V"}:
            return None
        index = _skip_options(words, index)
    if index >= len(words):
        return None

    command = words[index]
    if command in SHELL_INTERPRETERS:
        index = _skip_options(words, index + 1)
        return words[index] if index < len(words) else None
    if command in {"source", "."}:
        return words[index + 1] if index + 1 < len(words) else None
    return command


def _consumer_name(target: str | None) -> str | None:
    if target is None:
        return None
    normalized = target.removeprefix("./")
    return normalized if normalized in CONSUMER_NAMES else None


def _is_ghostty_init(words: list[str]) -> bool:
    return (
        len(words) >= 4
        and words[0] == "git"
        and words[1:3] == ["submodule", "update"]
        and "ghostty" in words[3:]
    )


def _events_from_roots(
    roots: list[object], script: str, step_line: int,
) -> list[RunEvent]:
    events: list[RunEvent] = []
    for root in roots:
        for command in _commands_in_execution_order(root):
            words = _command_words(command)
            line = step_line + script.count("\n", 0, command.pos[0])
            if _is_ghostty_init(words):
                events.append(RunEvent(kind="init", line=line))
            consumer = _consumer_name(_executed_target(words))
            if consumer is not None:
                events.append(RunEvent(kind="consumer", line=line, consumer=consumer))
    return events


def _relevant_lines(script: str) -> Iterator[tuple[int, str]]:
    heredoc_delimiter: str | None = None
    strip_tabs = False
    for line_offset, line in enumerate(script.splitlines()):
        if heredoc_delimiter is not None:
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == heredoc_delimiter:
                heredoc_delimiter = None
            continue

        heredoc = HEREDOC.search(line)
        if heredoc is not None:
            heredoc_delimiter = heredoc.group("delimiter")
            strip_tabs = bool(heredoc.group("strip_tabs"))

        if any(name in line for name in CONSUMER_NAMES) or (
            "git submodule" in line and "ghostty" in line
        ):
            yield line_offset, line


def _run_events(script: str, step_line: int) -> tuple[list[RunEvent], str | None]:
    if not any(name in script for name in CONSUMER_NAMES) and (
        "git submodule" not in script or "ghostty" not in script
    ):
        return [], None

    normalized = _normalize_heredocs(script)
    try:
        roots = bashlex.parse(normalized)
    except bashlex.errors.ParsingError as error:
        # bashlex intentionally implements a Bash subset and rejects otherwise
        # valid constructs such as [[ ... ]] and array assignments. If an
        # unrelated construct blocks the whole body, parse only logical lines
        # that can affect this guard. Heredoc bodies remain excluded.
        events: list[RunEvent] = []
        for line_offset, line in _relevant_lines(normalized):
            try:
                line_roots = bashlex.parse(line)
            except bashlex.errors.ParsingError as line_error:
                return [], f"{line_error} (whole run body: {error})"
            events.extend(
                _events_from_roots(line_roots, line, step_line + line_offset,)
            )
        return events, None

    return _events_from_roots(roots, normalized, step_line), None


def _checkout_initializes_ghostty(step: Step) -> bool:
    return (
        step.uses is not None
        and step.uses.startswith(CHECKOUT_ACTION)
        and step.inputs.get("submodules", "").lower() in {"true", "recursive"}
    )


def _job_events(path: Path, job: Job,) -> tuple[list[tuple[int, RunEvent]], list[str]]:
    events: list[tuple[int, RunEvent]] = []
    failures: list[str] = []
    for step_index, step in enumerate(job.steps):
        if _checkout_initializes_ghostty(step):
            events.append((step_index, RunEvent(kind="init", line=step.line)))
        if step.run is None:
            continue
        run_events, parse_error = _run_events(step.run, step.line)
        if parse_error is not None:
            failures.append(
                f"{path.name}:{step.line}: {job.name} cannot parse relevant run "
                f"step {step.name!r}: {parse_error}"
            )
            continue
        events.extend((step_index, event) for event in run_events)
    return events, failures


def _setup_zig_failures(
    path: Path, job: Job, events: list[tuple[int, RunEvent]],
) -> tuple[list[str], bool]:
    setup_steps = [
        (index, step)
        for index, step in enumerate(job.steps)
        if step.uses is not None and step.uses.startswith(SETUP_ZIG_ACTION)
    ]
    if not setup_steps:
        return [], False

    failures: list[str] = []
    init_events = [(index, event) for index, event in events if event.kind == "init"]
    resolver_steps = [
        (index, step)
        for index, step in enumerate(job.steps)
        if step.identifier == "ghostty-zig-version"
    ]
    helper_events = [
        (index, event)
        for index, event in events
        if event.kind == "consumer"
        and event.consumer == "scripts/ghostty-zig-version.sh"
    ]

    def fail(message: str) -> None:
        failures.append(f"{path.name}: job {job.name}: {message}")

    if len(init_events) != 1:
        fail("expected exactly one Ghostty submodule initialization")
    if len(resolver_steps) != 1:
        fail("expected exactly one Ghostty Zig resolver step")
    if len(helper_events) != 1:
        fail("resolver must execute scripts/ghostty-zig-version.sh exactly once")
    if len(setup_steps) != 1:
        fail("expected exactly one setup-zig action")

    if len(resolver_steps) == 1 and len(helper_events) == 1:
        resolver_index, _ = resolver_steps[0]
        helper_index, _ = helper_events[0]
        if helper_index != resolver_index:
            fail("Ghostty Zig resolver step must execute the version helper")

    if len(setup_steps) == 1:
        _, setup_step = setup_steps[0]
        if setup_step.inputs.get("version") != SETUP_ZIG_VERSION:
            fail("setup-zig must use the resolver output in its own step")

    if (
        len(init_events) == 1
        and len(resolver_steps) == 1
        and len(helper_events) == 1
        and len(setup_steps) == 1
    ):
        init_index, _ = init_events[0]
        resolver_index, _ = resolver_steps[0]
        helper_index, _ = helper_events[0]
        setup_index, _ = setup_steps[0]
        if not (
            init_index < resolver_index
            and resolver_index == helper_index
            and helper_index < setup_index
        ):
            fail("expected ordered Ghostty init -> resolver -> setup-zig wiring")

    return failures, True


def workflow_failures(
    workflow_dir: Path, *, require_setup_zig: bool = False,
) -> list[str]:
    failures: list[str] = []
    validated_setup_jobs = 0
    paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    for path in paths:
        jobs, parse_failures = _workflow_jobs(path)
        failures.extend(parse_failures)
        for job in jobs:
            events, event_failures = _job_events(path, job)
            failures.extend(event_failures)

            initialized = False
            for _, event in events:
                if event.kind == "init":
                    initialized = True
                elif event.kind == "consumer" and not initialized:
                    failures.append(
                        f"{path.name}:{event.line}: {job.name} executes "
                        f"{event.consumer} before Ghostty submodule init"
                    )

            setup_failures, validated = _setup_zig_failures(path, job, events)
            failures.extend(setup_failures)
            validated_setup_jobs += int(validated)

    if require_setup_zig and validated_setup_jobs == 0:
        failures.append("No setup-zig jobs were validated")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow_dir", type=Path)
    parser.add_argument("--require-setup-zig", action="store_true")
    arguments = parser.parse_args()

    failures = workflow_failures(
        arguments.workflow_dir, require_setup_zig=arguments.require_setup_zig,
    )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
