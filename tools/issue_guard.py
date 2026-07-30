#!/usr/bin/env python3
"""Audit and reconcile the governed GitHub Issue dependency graph.

The issue body's ``Blocked by`` section is authoritative.  GitHub's native
blocked-by relationships are only a mirror.  This module deliberately uses the
standard library so the guard can run before the workspace itself is trusted.

Diagnostics contain issue numbers, fixed codes, and HTTP status codes only.
Issue titles, bodies, API response bodies, and credentials are never emitted.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import os
import re
from typing import Any, Literal, Protocol
from urllib import error, parse, request


API_VERSION = "2026-03-10"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_MAX_PAGES = 100
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_PAGES_LIMIT = 100
MAX_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_TITLE_KEY = re.compile(r"^\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\](?:\s|$)")
_KEY = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"
_BLOCKER_LINE = re.compile(
    rf"^\s*(?:-\s+(?:\[[ xX]\]\s+)?)?"
    rf"#([1-9][0-9]*)\s+\(({_KEY})\)\s*$"
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel\s*=\s*"?next"?', re.I)


class GuardConfigurationError(RuntimeError):
    """A local, sanitized configuration failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GitHubApiError(RuntimeError):
    """A sanitized GitHub API failure with no response-body content."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Diagnostic:
    """A deterministic diagnostic that cannot carry repository content."""

    code: str
    issue_number: int | None = None
    blocker_number: int | None = None
    line_number: int | None = None
    status: int | None = None
    related_numbers: tuple[int, ...] = ()
    operation: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code}
        if self.issue_number is not None:
            result["issue_number"] = self.issue_number
        if self.blocker_number is not None:
            result["blocker_number"] = self.blocker_number
        if self.line_number is not None:
            result["line_number"] = self.line_number
        if self.status is not None:
            result["status"] = self.status
        if self.related_numbers:
            result["related_numbers"] = list(self.related_numbers)
        if self.operation is not None:
            result["operation"] = self.operation
        return result


def _diagnostic_sort_key(
    diagnostic: Diagnostic,
) -> tuple[str, int, int, int, int, tuple[int, ...], str]:
    return (
        diagnostic.code,
        diagnostic.issue_number or -1,
        diagnostic.blocker_number or -1,
        diagnostic.line_number or -1,
        diagnostic.status or -1,
        diagnostic.related_numbers,
        diagnostic.operation or "",
    )


def _sorted_diagnostics(
    diagnostics: Sequence[Diagnostic],
) -> tuple[Diagnostic, ...]:
    return tuple(sorted(set(diagnostics), key=_diagnostic_sort_key))


@dataclass(frozen=True)
class DependencyReference:
    number: int
    key: str


BacklogState = Literal["active", "next", "deferred", "folded"]
BACKLOG_LABELS: dict[str, BacklogState] = {
    "backlog:active": "active",
    "backlog:next": "next",
    "backlog:deferred": "deferred",
    "backlog:folded": "folded",
}


@dataclass(frozen=True)
class IssueRecord:
    number: int
    database_id: int
    title: str
    body: str
    state: Literal["open", "closed"]
    key: str | None
    blockers: tuple[DependencyReference, ...]
    labels: tuple[str, ...]
    backlog_state: BacklogState | None


@dataclass(frozen=True, order=True)
class NativeDependency:
    database_id: int
    number: int
    repository_url: str


@dataclass(frozen=True)
class Mutation:
    action: Literal["reopen", "add", "remove"]
    issue_number: int
    blocker_number: int | None = None
    blocker_database_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "action": self.action,
            "issue_number": self.issue_number,
        }
        if self.blocker_number is not None:
            result["blocker_number"] = self.blocker_number
        if self.blocker_database_id is not None:
            result["blocker_database_id"] = self.blocker_database_id
        return result


IssueStatus = Literal[
    "GRAPH_READY",
    "BLOCKED",
    "BACKLOG_NEXT",
    "BACKLOG_DEFERRED",
    "BACKLOG_FOLDED",
    "CLOSED",
    "ILLEGAL_CLOSED",
    "INVALID",
]


@dataclass(frozen=True)
class IssueResult:
    number: int
    key: str | None
    backlog_state: BacklogState | None
    status: IssueStatus
    effective_open_blockers: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "key": self.key,
            "backlog_state": self.backlog_state,
            "status": self.status,
            "effective_open_blockers": list(self.effective_open_blockers),
        }


@dataclass(frozen=True)
class GuardReport:
    mode: Literal["audit", "reconcile"]
    ok: bool
    fingerprint: str
    issues: tuple[IssueResult, ...]
    diagnostics: tuple[Diagnostic, ...]
    mutations: tuple[Mutation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "ok": self.ok,
            "fingerprint": self.fingerprint,
            "issues": [item.to_dict() for item in self.issues],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "mutations": [item.to_dict() for item in self.mutations],
        }


@dataclass(frozen=True)
class Snapshot:
    issues: Mapping[int, IssueRecord]
    pull_request_numbers: frozenset[int]
    native: Mapping[int, tuple[NativeDependency, ...]]
    graph_diagnostics: tuple[Diagnostic, ...]
    native_diagnostics: tuple[Diagnostic, ...]
    mirror_diagnostics: tuple[Diagnostic, ...]
    fingerprint: str

    @property
    def structural_diagnostics(self) -> tuple[Diagnostic, ...]:
        return _sorted_diagnostics(self.graph_diagnostics + self.native_diagnostics)

    @property
    def all_diagnostics(self) -> tuple[Diagnostic, ...]:
        return _sorted_diagnostics(
            self.graph_diagnostics + self.native_diagnostics + self.mirror_diagnostics
        )


class IssueClient(Protocol):
    """The narrow client surface used by the graph engine."""

    def list_issues(self) -> Sequence[Mapping[str, object]]:
        """Return every issue and pull request in the repository."""

    def list_blocked_by(self, issue_number: int) -> Sequence[Mapping[str, object]]:
        """Return every native blocker for one issue."""

    def reopen_issue(self, issue_number: int) -> None:
        """Reopen an issue without changing any other field."""

    def add_blocker(self, issue_number: int, blocker_database_id: int) -> None:
        """Add one native relationship using the blocker's database ID."""

    def remove_blocker(self, issue_number: int, blocker_database_id: int) -> None:
        """Remove one native relationship using the blocker's database ID."""


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _normalize_labels(value: object) -> tuple[str, ...] | None:
    """Return deterministic, case-insensitive label names from an API record."""

    if not isinstance(value, list):
        return None
    names: set[str] = set()
    for item in value:
        name: object
        if isinstance(item, str):
            name = item
        elif isinstance(item, Mapping):
            name = item.get("name")
        else:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        names.add(name.strip().casefold())
    return tuple(sorted(names))


def _strip_html_comments(
    line: str, in_comment: bool, inline_ticks: int
) -> tuple[str, bool, int]:
    """Strip comments while preserving code spans, including multiline spans."""

    visible: list[str] = []
    cursor = 0
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            if end < 0:
                return "".join(visible), True, inline_ticks
            cursor = end + 3
            in_comment = False
            continue

        if line[cursor] == "`":
            end = cursor + 1
            while end < len(line) and line[end] == "`":
                end += 1
            run_length = end - cursor
            visible.append(line[cursor:end])
            if inline_ticks == 0:
                inline_ticks = run_length
            elif run_length == inline_ticks:
                inline_ticks = 0
            cursor = end
            continue

        if inline_ticks == 0 and line.startswith("<!--", cursor):
            cursor += 4
            in_comment = True
            continue

        next_tick = line.find("`", cursor)
        next_comment = line.find("<!--", cursor) if inline_ticks == 0 else -1
        candidates = [
            position for position in (next_tick, next_comment) if position >= 0
        ]
        end = min(candidates) if candidates else len(line)
        visible.append(line[cursor:end])
        cursor = end
    return "".join(visible), in_comment, inline_ticks


def _visible_markdown_lines(body: str) -> list[tuple[int, str]]:
    """Return lines outside fenced code and HTML comments."""

    result: list[tuple[int, str]] = []
    in_comment = False
    inline_ticks = 0
    fence_character: str | None = None
    fence_length = 0
    for line_number, raw_line in enumerate(body.splitlines(), start=1):
        if fence_character is not None:
            close = re.match(
                rf"^\s{{0,3}}{re.escape(fence_character)}"
                rf"{{{fence_length},}}\s*$",
                raw_line,
            )
            if close is not None:
                fence_character = None
                fence_length = 0
            continue
        # Comment delimiters inside fenced code are literal code.  Updating
        # comment state there can hide a later, authoritative section.
        raw_fence = _FENCE.match(raw_line)
        raw_fence_is_valid = raw_fence is not None and not (
            raw_fence.group(1).startswith("`") and "`" in raw_fence.group(2)
        )
        if not in_comment and (
            not raw_line.strip()
            or _HEADING.match(raw_line) is not None
            or raw_fence_is_valid
        ):
            # Inline code spans cannot cross a Markdown block boundary.
            inline_ticks = 0
        inline_before = inline_ticks
        line, in_comment, inline_ticks = _strip_html_comments(
            raw_line, in_comment, inline_ticks
        )
        if _HEADING.match(line) is not None and _HEADING.match(raw_line) is None:
            # Inline comment removal cannot create block syntax that was not
            # present at the start of the original Markdown line.
            line = raw_line
        opening = _FENCE.match(line)
        if (
            opening is not None
            and opening.group(1).startswith("`")
            and "`" in opening.group(2)
        ):
            # CommonMark does not recognize a backtick fence whose info string
            # contains a backtick.  Treating it as a fence could hide a real
            # dependency declaration.
            opening = None
        if opening is not None and raw_fence_is_valid and inline_before == 0:
            fence = opening.group(1)
            fence_character = fence[0]
            fence_length = len(fence)
            inline_ticks = 0
            continue
        result.append((line_number, line))
    return result


def _heading(line: str) -> tuple[int, str] | None:
    match = _HEADING.match(line)
    if match is None:
        return None
    text = match.group(2).strip()
    text = re.sub(r"\s+#+\s*$", "", text).strip()
    return len(match.group(1)), text


def parse_blocked_by(
    body: str, issue_number: int
) -> tuple[tuple[DependencyReference, ...], tuple[Diagnostic, ...]]:
    """Parse exactly one governed ``Blocked by`` section."""

    visible = _visible_markdown_lines(body)
    headings: list[tuple[int, int, int]] = []
    for index, (line_number, line) in enumerate(visible):
        parsed = _heading(line)
        if parsed is None:
            continue
        level, text = parsed
        if level in {2, 3} and text == "Blocked by":
            headings.append((index, line_number, level))
    if not headings:
        return (), (
            Diagnostic("BLOCKED_BY_SECTION_MISSING", issue_number=issue_number),
        )
    if len(headings) != 1:
        return (), (
            Diagnostic("BLOCKED_BY_SECTION_DUPLICATE", issue_number=issue_number),
        )

    start_index, _, level = headings[0]
    content: list[tuple[int, str]] = []
    for line_number, line in visible[start_index + 1 :]:
        parsed = _heading(line)
        if parsed is not None and parsed[0] <= level:
            break
        if line.strip():
            content.append((line_number, line))

    if len(content) == 1 and content[0][1].strip() in {"None", "None."}:
        return (), ()

    diagnostics: list[Diagnostic] = []
    references: list[DependencyReference] = []
    seen: set[int] = set()
    if not content:
        diagnostics.append(
            Diagnostic("BLOCKED_BY_LINE_INVALID", issue_number=issue_number)
        )
    for line_number, line in content:
        if line.strip() in {"None", "None."}:
            diagnostics.append(
                Diagnostic(
                    "BLOCKED_BY_LINE_INVALID",
                    issue_number=issue_number,
                    line_number=line_number,
                )
            )
            continue
        match = _BLOCKER_LINE.match(line)
        if match is None:
            diagnostics.append(
                Diagnostic(
                    "BLOCKED_BY_LINE_INVALID",
                    issue_number=issue_number,
                    line_number=line_number,
                )
            )
            continue
        number = int(match.group(1))
        key = match.group(2)
        if number in seen:
            diagnostics.append(
                Diagnostic(
                    "BLOCKER_DUPLICATE",
                    issue_number=issue_number,
                    blocker_number=number,
                    line_number=line_number,
                )
            )
            continue
        seen.add(number)
        references.append(DependencyReference(number, key))
    return tuple(references), _sorted_diagnostics(diagnostics)


def _cycle_diagnostics(
    adjacency: Mapping[int, tuple[int, ...]],
) -> tuple[Diagnostic, ...]:
    """Return deterministic strongly connected cycles without recursion."""

    normalized = {
        node: tuple(sorted(set(adjacency.get(node, ())))) for node in sorted(adjacency)
    }
    visited: set[int] = set()
    finish_order: list[int] = []
    for start in sorted(normalized):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[int, int]] = [(start, 0)]
        while stack:
            node, offset = stack[-1]
            neighbors = normalized.get(node, ())
            if offset < len(neighbors):
                neighbor = neighbors[offset]
                stack[-1] = (node, offset + 1)
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append((neighbor, 0))
                continue
            stack.pop()
            finish_order.append(node)

    transpose: dict[int, list[int]] = {node: [] for node in normalized}
    for node, neighbors in normalized.items():
        for neighbor in neighbors:
            transpose.setdefault(neighbor, []).append(node)

    assigned: set[int] = set()
    components: list[tuple[int, ...]] = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[int] = []
        stack_nodes = [start]
        while stack_nodes:
            node = stack_nodes.pop()
            component.append(node)
            for neighbor in sorted(transpose.get(node, ()), reverse=True):
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    stack_nodes.append(neighbor)
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or (
            len(ordered) == 1 and ordered[0] in normalized.get(ordered[0], ())
        ):
            components.append(ordered)

    return tuple(
        Diagnostic("DEPENDENCY_CYCLE", related_numbers=component)
        for component in sorted(components)
    )


def _parse_issue_graph(
    raw_items: Sequence[Mapping[str, object]],
) -> tuple[dict[int, IssueRecord], frozenset[int], tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = []
    raw_issues: dict[
        int, tuple[int, str, str, Literal["open", "closed"], tuple[str, ...]]
    ] = {}
    pull_requests: set[int] = set()
    seen_numbers: set[int] = set()

    for raw in raw_items:
        number = _positive_integer(raw.get("number"))
        database_id = _positive_integer(raw.get("id"))
        if number is None or database_id is None:
            diagnostics.append(Diagnostic("ISSUE_RECORD_INVALID"))
            continue
        if number in seen_numbers:
            diagnostics.append(
                Diagnostic("ISSUE_NUMBER_DUPLICATE", issue_number=number)
            )
            continue
        seen_numbers.add(number)
        if "pull_request" in raw:
            pull_requests.add(number)
            continue
        title = raw.get("title")
        body = raw.get("body")
        state = raw.get("state")
        labels = _normalize_labels(raw.get("labels"))
        if not isinstance(title, str) or not (isinstance(body, str) or body is None):
            diagnostics.append(Diagnostic("ISSUE_RECORD_INVALID", issue_number=number))
            continue
        if state not in {"open", "closed"}:
            diagnostics.append(Diagnostic("ISSUE_RECORD_INVALID", issue_number=number))
            continue
        if labels is None:
            diagnostics.append(Diagnostic("ISSUE_RECORD_INVALID", issue_number=number))
            continue
        normalized_state: Literal["open", "closed"] = state
        raw_issues[number] = (
            database_id,
            title,
            body or "",
            normalized_state,
            labels,
        )

    issues: dict[int, IssueRecord] = {}
    key_owner: dict[str, int] = {}
    database_id_owner: dict[int, int] = {}
    for number in sorted(raw_issues):
        database_id, title, body, state, labels = raw_issues[number]
        if database_id in database_id_owner:
            diagnostics.append(
                Diagnostic(
                    "ISSUE_DATABASE_ID_DUPLICATE",
                    issue_number=number,
                    blocker_number=database_id_owner[database_id],
                )
            )
        else:
            database_id_owner[database_id] = number
        key_match = _TITLE_KEY.match(title)
        key = key_match.group(1) if key_match is not None else None
        if key is None:
            diagnostics.append(
                Diagnostic("ISSUE_TITLE_KEY_MISSING", issue_number=number)
            )
        elif key in key_owner:
            diagnostics.append(
                Diagnostic(
                    "ISSUE_KEY_DUPLICATE",
                    issue_number=number,
                    blocker_number=key_owner[key],
                )
            )
        else:
            key_owner[key] = number
        blockers, body_diagnostics = parse_blocked_by(body, number)
        diagnostics.extend(body_diagnostics)
        backlog_labels = tuple(
            BACKLOG_LABELS[label] for label in labels if label in BACKLOG_LABELS
        )
        if len(backlog_labels) > 1:
            diagnostics.append(
                Diagnostic("BACKLOG_STATE_MULTIPLE", issue_number=number)
            )
        elif not backlog_labels and state == "open":
            diagnostics.append(Diagnostic("BACKLOG_STATE_MISSING", issue_number=number))
        backlog_state = backlog_labels[0] if len(backlog_labels) == 1 else None
        issues[number] = IssueRecord(
            number=number,
            database_id=database_id,
            title=title,
            body=body,
            state=state,
            key=key,
            blockers=blockers,
            labels=labels,
            backlog_state=backlog_state,
        )

    adjacency: dict[int, tuple[int, ...]] = {}
    for number in sorted(issues):
        record = issues[number]
        neighbors: list[int] = []
        for blocker in record.blockers:
            if blocker.number == number:
                diagnostics.append(
                    Diagnostic(
                        "BLOCKER_SELF_REFERENCE",
                        issue_number=number,
                        blocker_number=blocker.number,
                    )
                )
            if blocker.number in pull_requests:
                diagnostics.append(
                    Diagnostic(
                        "BLOCKER_PR_REFERENCE",
                        issue_number=number,
                        blocker_number=blocker.number,
                    )
                )
                continue
            target = issues.get(blocker.number)
            if target is None:
                diagnostics.append(
                    Diagnostic(
                        "BLOCKER_MISSING",
                        issue_number=number,
                        blocker_number=blocker.number,
                    )
                )
                continue
            neighbors.append(blocker.number)
            if target.key != blocker.key:
                diagnostics.append(
                    Diagnostic(
                        "BLOCKER_KEY_MISMATCH",
                        issue_number=number,
                        blocker_number=blocker.number,
                    )
                )
        adjacency[number] = tuple(neighbors)
    diagnostics.extend(_cycle_diagnostics(adjacency))
    return issues, frozenset(pull_requests), _sorted_diagnostics(diagnostics)


def _parse_native_dependencies(
    raw_items: Sequence[Mapping[str, object]],
    issue_number: int,
) -> tuple[tuple[NativeDependency, ...], tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = []
    dependencies: list[NativeDependency] = []
    seen_database_ids: set[int] = set()
    for raw in raw_items:
        database_id = _positive_integer(raw.get("id"))
        number = _positive_integer(raw.get("number"))
        repository_url = raw.get("repository_url")
        if (
            database_id is None
            or number is None
            or not isinstance(repository_url, str)
            or not repository_url
        ):
            diagnostics.append(
                Diagnostic("NATIVE_RECORD_INVALID", issue_number=issue_number)
            )
            continue
        if database_id in seen_database_ids:
            diagnostics.append(
                Diagnostic(
                    "NATIVE_MIRROR_DUPLICATE",
                    issue_number=issue_number,
                    blocker_number=number,
                )
            )
            continue
        seen_database_ids.add(database_id)
        dependencies.append(
            NativeDependency(database_id, number, repository_url.rstrip("/"))
        )
    return tuple(sorted(dependencies)), _sorted_diagnostics(diagnostics)


def _snapshot_fingerprint(
    issues: Mapping[int, IssueRecord],
    pull_request_numbers: frozenset[int],
    native: Mapping[int, tuple[NativeDependency, ...]],
) -> str:
    payload = {
        "issues": [
            {
                "number": issue.number,
                "id": issue.database_id,
                "title": issue.title,
                "body": issue.body,
                "state": issue.state,
                "labels": issue.labels,
            }
            for issue in sorted(issues.values(), key=lambda item: item.number)
        ],
        "pull_requests": sorted(pull_request_numbers),
        "native": {
            str(number): [
                {
                    "id": item.database_id,
                    "number": item.number,
                    "repository_url": item.repository_url,
                }
                for item in native.get(number, ())
            ]
            for number in sorted(issues)
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_snapshot(client: IssueClient, owner: str, repo: str) -> Snapshot:
    """Read and validate the complete body graph and native mirror."""

    raw_items = client.list_issues()
    issues, pull_requests, graph_diagnostics = _parse_issue_graph(raw_items)
    native: dict[int, tuple[NativeDependency, ...]] = {}
    native_diagnostics: list[Diagnostic] = []
    mirror_diagnostics: list[Diagnostic] = []
    expected_repository_url = f"https://api.github.com/repos/{owner}/{repo}".casefold()

    if not graph_diagnostics:
        for number in sorted(issues):
            parsed, findings = _parse_native_dependencies(
                client.list_blocked_by(number), number
            )
            native[number] = parsed
            native_diagnostics.extend(findings)
        database_id_to_number = {
            issue.database_id: number for number, issue in issues.items()
        }
        for number in sorted(issues):
            for dependency in native.get(number, ()):
                if dependency.repository_url.casefold() != expected_repository_url:
                    continue
                expected_number = database_id_to_number.get(dependency.database_id)
                known_number = issues.get(dependency.number)
                if (
                    expected_number is not None and expected_number != dependency.number
                ) or (
                    known_number is not None
                    and known_number.database_id != dependency.database_id
                ):
                    native_diagnostics.append(
                        Diagnostic(
                            "NATIVE_RECORD_IDENTITY_MISMATCH",
                            issue_number=number,
                            blocker_number=dependency.number,
                        )
                    )
        if not native_diagnostics:
            for number in sorted(issues):
                desired_ids = {
                    issues[reference.number].database_id
                    for reference in issues[number].blockers
                }
                current_ids = {
                    dependency.database_id
                    for dependency in native.get(number, ())
                    if dependency.repository_url.casefold() == expected_repository_url
                }
                cross_repository_ids = {
                    dependency.database_id
                    for dependency in native.get(number, ())
                    if dependency.repository_url.casefold() != expected_repository_url
                }
                if desired_ids != current_ids or cross_repository_ids:
                    mirror_diagnostics.append(
                        Diagnostic("NATIVE_MIRROR_MISMATCH", issue_number=number)
                    )

    fingerprint = _snapshot_fingerprint(issues, pull_requests, native)
    return Snapshot(
        issues=issues,
        pull_request_numbers=pull_requests,
        native=native,
        graph_diagnostics=graph_diagnostics,
        native_diagnostics=_sorted_diagnostics(native_diagnostics),
        mirror_diagnostics=_sorted_diagnostics(mirror_diagnostics),
        fingerprint=fingerprint,
    )


def _effective_open_depths(issues: Mapping[int, IssueRecord]) -> dict[int, int]:
    depths = {number: 0 for number, issue in issues.items() if issue.state == "open"}
    dependents: dict[int, list[int]] = {number: [] for number in issues}
    for number, issue in issues.items():
        for reference in issue.blockers:
            if reference.number in issues:
                dependents[reference.number].append(number)
    pending = deque(sorted(depths))
    while pending:
        blocker = pending.popleft()
        candidate_depth = depths[blocker] + 1
        for dependent in sorted(dependents.get(blocker, ())):
            existing = depths.get(dependent)
            if existing is None or candidate_depth < existing:
                depths[dependent] = candidate_depth
                pending.append(dependent)
    return depths


def _illegal_reopen_order(
    issues: Mapping[int, IssueRecord], depths: Mapping[int, int]
) -> tuple[int, ...]:
    """Order illegal closures so every illegal blocker reopens first."""

    illegal = {
        number
        for number, issue in issues.items()
        if issue.state == "closed" and number in depths
    }
    indegree = {number: 0 for number in illegal}
    dependents: dict[int, list[int]] = {number: [] for number in illegal}
    for number in illegal:
        for reference in issues[number].blockers:
            if reference.number in illegal:
                indegree[number] += 1
                dependents[reference.number].append(number)
    ready = [number for number, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[int] = []
    while ready:
        blocker = heapq.heappop(ready)
        ordered.append(blocker)
        for dependent in sorted(dependents.get(blocker, ())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(illegal):
        # Structural cycle validation should make this unreachable.
        raise RuntimeError("illegal closure subgraph is cyclic")
    return tuple(ordered)


def _graph_state_fingerprint(
    snapshot: Snapshot, *, force_open: frozenset[int] = frozenset()
) -> str:
    payload = [
        {
            "number": issue.number,
            "id": issue.database_id,
            "title": issue.title,
            "body": issue.body,
            "state": "open" if issue.number in force_open else issue.state,
            "labels": issue.labels,
        }
        for issue in sorted(snapshot.issues.values(), key=lambda item: item.number)
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue_results(
    snapshot: Snapshot,
    *,
    invalid_all: bool = False,
) -> tuple[IssueResult, ...]:
    depths = _effective_open_depths(snapshot.issues)
    mirror_invalid = {
        diagnostic.issue_number
        for diagnostic in snapshot.mirror_diagnostics
        if diagnostic.issue_number is not None
    }
    results: list[IssueResult] = []
    for number in sorted(snapshot.issues):
        issue = snapshot.issues[number]
        effective_blockers = tuple(
            sorted(
                reference.number
                for reference in issue.blockers
                if reference.number in depths
            )
        )
        if invalid_all or number in mirror_invalid:
            status: IssueStatus = "INVALID"
        elif issue.state == "closed" and number in depths:
            status = "ILLEGAL_CLOSED"
        elif issue.state == "closed":
            status = "CLOSED"
        elif issue.backlog_state == "next":
            status = "BACKLOG_NEXT"
        elif issue.backlog_state == "deferred":
            status = "BACKLOG_DEFERRED"
        elif issue.backlog_state == "folded":
            status = "BACKLOG_FOLDED"
        elif effective_blockers:
            status = "BLOCKED"
        else:
            status = "GRAPH_READY"
        results.append(
            IssueResult(
                number,
                issue.key,
                issue.backlog_state,
                status,
                effective_blockers,
            )
        )
    return tuple(results)


def _illegal_closure_diagnostics(snapshot: Snapshot) -> tuple[Diagnostic, ...]:
    depths = _effective_open_depths(snapshot.issues)
    return tuple(
        Diagnostic("ILLEGAL_CLOSED", issue_number=number)
        for number in sorted(snapshot.issues)
        if snapshot.issues[number].state == "closed" and number in depths
    )


def audit(client: IssueClient, owner: str, repo: str) -> GuardReport:
    """Audit the authoritative body graph and its native mirror."""

    snapshot = load_snapshot(client, owner, repo)
    diagnostics = _sorted_diagnostics(
        snapshot.all_diagnostics + _illegal_closure_diagnostics(snapshot)
    )
    return GuardReport(
        mode="audit",
        ok=not diagnostics,
        fingerprint=snapshot.fingerprint,
        issues=_issue_results(
            snapshot, invalid_all=bool(snapshot.structural_diagnostics)
        ),
        diagnostics=diagnostics,
    )


def _mutation_failure(
    snapshot: Snapshot,
    mutations: Sequence[Mutation],
    *,
    operation: str,
    issue_number: int,
    blocker_number: int | None = None,
) -> GuardReport:
    diagnostic = Diagnostic(
        "MUTATION_FAILED",
        issue_number=issue_number,
        blocker_number=blocker_number,
        operation=operation,
    )
    return GuardReport(
        mode="reconcile",
        ok=False,
        fingerprint=snapshot.fingerprint,
        issues=_issue_results(snapshot),
        diagnostics=(diagnostic,),
        mutations=tuple(mutations),
    )


def _native_delta(
    snapshot: Snapshot, owner: str, repo: str
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """Return missing and stale links as issue, blocker number, database ID."""

    expected_repository_url = f"https://api.github.com/repos/{owner}/{repo}".casefold()
    missing: list[tuple[int, int, int]] = []
    stale: list[tuple[int, int, int]] = []
    for number in sorted(snapshot.issues):
        desired_by_id = {
            snapshot.issues[reference.number].database_id: reference.number
            for reference in snapshot.issues[number].blockers
        }
        current = snapshot.native.get(number, ())
        current_ids = {
            item.database_id
            for item in current
            if item.repository_url.casefold() == expected_repository_url
        }
        for database_id, blocker_number in desired_by_id.items():
            if database_id not in current_ids:
                missing.append((number, blocker_number, database_id))
        for dependency in current:
            if (
                dependency.repository_url.casefold() != expected_repository_url
                or dependency.database_id not in desired_by_id
            ):
                stale.append((number, dependency.number, dependency.database_id))
    return sorted(missing), sorted(stale)


def _cycle_breaking_stale(
    snapshot: Snapshot,
    stale: Sequence[tuple[int, int, int]],
    owner: str,
    repo: str,
) -> tuple[tuple[int, int, int], ...]:
    """Return stale native links that must be removed before desired adds.

    GitHub rejects an add when the current native graph plus the new edge would
    form a cycle. A body-authority rewrite can legitimately reverse a whole
    dependency chain, so add-before-remove is impossible for only those stale
    edges that participate in cycles in the union of current and desired links.
    """

    expected_repository_url = f"https://api.github.com/repos/{owner}/{repo}".casefold()
    adjacency: dict[int, set[int]] = {
        number: {reference.number for reference in issue.blockers}
        for number, issue in snapshot.issues.items()
    }
    local_native_edges: set[tuple[int, int, int]] = set()
    for number, dependencies in snapshot.native.items():
        for dependency in dependencies:
            if (
                dependency.repository_url.casefold() == expected_repository_url
                and dependency.number in snapshot.issues
            ):
                adjacency[number].add(dependency.number)
                local_native_edges.add(
                    (number, dependency.number, dependency.database_id)
                )

    components = [
        frozenset(diagnostic.related_numbers)
        for diagnostic in _cycle_diagnostics(
            {number: tuple(neighbors) for number, neighbors in adjacency.items()}
        )
    ]
    return tuple(
        item
        for item in sorted(stale)
        if item in local_native_edges
        and any(
            item[0] in component and item[1] in component for component in components
        )
    )


def reconcile(client: IssueClient, owner: str, repo: str) -> GuardReport:
    """Conservatively reconcile issue state and the native dependency mirror."""

    initial = load_snapshot(client, owner, repo)
    if initial.structural_diagnostics:
        return GuardReport(
            mode="reconcile",
            ok=False,
            fingerprint=initial.fingerprint,
            issues=_issue_results(initial, invalid_all=True),
            diagnostics=initial.structural_diagnostics,
        )

    current = load_snapshot(client, owner, repo)
    if current.structural_diagnostics:
        return GuardReport(
            mode="reconcile",
            ok=False,
            fingerprint=current.fingerprint,
            issues=_issue_results(current, invalid_all=True),
            diagnostics=current.structural_diagnostics,
        )
    if current.fingerprint != initial.fingerprint:
        return GuardReport(
            mode="reconcile",
            ok=False,
            fingerprint=current.fingerprint,
            issues=_issue_results(current),
            diagnostics=(Diagnostic("CONCURRENT_STATE_CHANGE"),),
        )

    mutations: list[Mutation] = []
    depths = _effective_open_depths(current.issues)
    illegal_closed = _illegal_reopen_order(current.issues, depths)
    for number in illegal_closed:
        try:
            client.reopen_issue(number)
        except Exception:
            return _mutation_failure(
                current,
                mutations,
                operation="reopen",
                issue_number=number,
            )
        mutations.append(Mutation("reopen", number))

    missing, stale = _native_delta(current, owner, repo)
    cycle_breaking = _cycle_breaking_stale(current, stale, owner, repo)
    for issue_number, blocker_number, database_id in cycle_breaking:
        try:
            client.remove_blocker(issue_number, database_id)
        except Exception:
            return _mutation_failure(
                current,
                mutations,
                operation="remove-cycle",
                issue_number=issue_number,
                blocker_number=blocker_number,
            )
        mutations.append(Mutation("remove", issue_number, blocker_number, database_id))

    if cycle_breaking:
        verification = load_snapshot(client, owner, repo)
        if verification.structural_diagnostics:
            diagnostics = _sorted_diagnostics(
                verification.structural_diagnostics
                + (Diagnostic("REMOVAL_VERIFICATION_FAILED"),)
            )
            return GuardReport(
                mode="reconcile",
                ok=False,
                fingerprint=verification.fingerprint,
                issues=_issue_results(verification, invalid_all=True),
                diagnostics=diagnostics,
                mutations=tuple(mutations),
            )
        expected_graph = _graph_state_fingerprint(
            current, force_open=frozenset(illegal_closed)
        )
        if _graph_state_fingerprint(verification) != expected_graph:
            return GuardReport(
                mode="reconcile",
                ok=False,
                fingerprint=verification.fingerprint,
                issues=_issue_results(verification),
                diagnostics=(Diagnostic("CONCURRENT_STATE_CHANGE"),),
                mutations=tuple(mutations),
            )
        missing, stale = _native_delta(verification, owner, repo)
        if any(item in stale for item in cycle_breaking):
            return GuardReport(
                mode="reconcile",
                ok=False,
                fingerprint=verification.fingerprint,
                issues=_issue_results(verification),
                diagnostics=(Diagnostic("REMOVAL_VERIFICATION_FAILED"),),
                mutations=tuple(mutations),
            )
        current = verification

    for issue_number, blocker_number, database_id in missing:
        try:
            client.add_blocker(issue_number, database_id)
        except Exception:
            return _mutation_failure(
                current,
                mutations,
                operation="add",
                issue_number=issue_number,
                blocker_number=blocker_number,
            )
        mutations.append(Mutation("add", issue_number, blocker_number, database_id))

    # Before ordinary stale-link removal, re-read the complete graph and prove
    # that every add is visible. A successful HTTP response without the
    # relationship is an uncertain outcome, so non-cycle stale links remain.
    if stale:
        verification = load_snapshot(client, owner, repo)
        if verification.structural_diagnostics:
            diagnostics = _sorted_diagnostics(
                verification.structural_diagnostics
                + (Diagnostic("ADD_VERIFICATION_FAILED"),)
            )
            return GuardReport(
                mode="reconcile",
                ok=False,
                fingerprint=verification.fingerprint,
                issues=_issue_results(verification, invalid_all=True),
                diagnostics=diagnostics,
                mutations=tuple(mutations),
            )
        expected_graph = _graph_state_fingerprint(
            current, force_open=frozenset(illegal_closed)
        )
        if _graph_state_fingerprint(verification) != expected_graph:
            return GuardReport(
                mode="reconcile",
                ok=False,
                fingerprint=verification.fingerprint,
                issues=_issue_results(verification),
                diagnostics=(Diagnostic("CONCURRENT_STATE_CHANGE"),),
                mutations=tuple(mutations),
            )
        verified_missing, stale = _native_delta(verification, owner, repo)
        if verified_missing:
            return GuardReport(
                mode="reconcile",
                ok=False,
                fingerprint=verification.fingerprint,
                issues=_issue_results(verification),
                diagnostics=(Diagnostic("ADD_VERIFICATION_FAILED"),),
                mutations=tuple(mutations),
            )

    # Non-cycle removals happen only after every desired relationship is seen.
    for issue_number, blocker_number, database_id in stale:
        try:
            client.remove_blocker(issue_number, database_id)
        except Exception:
            return _mutation_failure(
                current,
                mutations,
                operation="remove",
                issue_number=issue_number,
                blocker_number=blocker_number,
            )
        mutations.append(Mutation("remove", issue_number, blocker_number, database_id))

    post = load_snapshot(client, owner, repo)
    post_depths = _effective_open_depths(post.issues)
    illegal_after = any(
        issue.state == "closed" and number in post_depths
        for number, issue in post.issues.items()
    )
    if post.all_diagnostics or illegal_after:
        post_diagnostics = list(post.all_diagnostics)
        post_diagnostics.append(Diagnostic("POSTCONDITION_FAILED"))
        return GuardReport(
            mode="reconcile",
            ok=False,
            fingerprint=post.fingerprint,
            issues=_issue_results(post, invalid_all=bool(post.structural_diagnostics)),
            diagnostics=_sorted_diagnostics(post_diagnostics),
            mutations=tuple(mutations),
        )
    return GuardReport(
        mode="reconcile",
        ok=True,
        fingerprint=post.fingerprint,
        issues=_issue_results(post),
        diagnostics=(),
        mutations=tuple(mutations),
    )


class _SameOriginRedirectHandler(request.HTTPRedirectHandler):
    """Allow redirects only when the authorization origin is unchanged."""

    def __init__(self, origin: tuple[str, str]) -> None:
        super().__init__()
        self._allowed_origin = origin

    def redirect_request(
        self,
        req: request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> request.Request | None:
        resolved = parse.urljoin(req.full_url, newurl)
        target = parse.urlsplit(resolved)
        target_origin = (target.scheme.casefold(), target.netloc.casefold())
        if target_origin != self._allowed_origin:
            raise GitHubApiError("REDIRECT_ORIGIN_INVALID")
        return super().redirect_request(req, fp, code, msg, headers, resolved)


class GitHubClient:
    """Bounded stdlib GitHub REST client for the issue guard."""

    def __init__(
        self,
        *,
        auth_value: str,
        owner: str,
        repo: str,
        api_url: str = DEFAULT_API_URL,
        max_pages: int = DEFAULT_MAX_PAGES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not auth_value:
            raise GuardConfigurationError("GITHUB_TOKEN_MISSING")
        if not owner or not repo or "/" in owner or "/" in repo:
            raise GuardConfigurationError("GITHUB_REPOSITORY_INVALID")
        if isinstance(max_pages, bool) or max_pages <= 0 or max_pages > MAX_PAGES_LIMIT:
            raise GuardConfigurationError("PAGINATION_BOUND_INVALID")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_TIMEOUT_SECONDS
        ):
            raise GuardConfigurationError("HTTP_TIMEOUT_INVALID")
        self._auth_value = auth_value
        self.owner = owner
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.max_pages = max_pages
        self.timeout_seconds = timeout_seconds
        origin = parse.urlsplit(self.api_url)
        if (
            origin.scheme.casefold() != "https"
            or not origin.netloc
            or origin.username is not None
            or origin.password is not None
        ):
            raise GuardConfigurationError("GITHUB_API_URL_INVALID")
        self._origin = (origin.scheme.casefold(), origin.netloc.casefold())
        self._opener = (
            opener
            or request.build_opener(_SameOriginRedirectHandler(self._origin)).open
        )

    @classmethod
    def from_environment(
        cls,
        *,
        repository: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> GitHubClient:
        auth_value = os.environ.get("GITHUB_TOKEN", "")
        if not auth_value:
            raise GuardConfigurationError("GITHUB_TOKEN_MISSING")
        repository_name = repository or os.environ.get("GITHUB_REPOSITORY", "")
        pieces = repository_name.split("/")
        if len(pieces) != 2 or not all(pieces):
            raise GuardConfigurationError("GITHUB_REPOSITORY_INVALID")
        return cls(
            auth_value=auth_value,
            owner=pieces[0],
            repo=pieces[1],
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = f"{self.api_url}/{path_or_url.lstrip('/')}"
        parsed = parse.urlsplit(url)
        origin = (parsed.scheme.casefold(), parsed.netloc.casefold())
        if origin != self._origin:
            raise GitHubApiError("PAGINATION_ORIGIN_INVALID")
        return url

    def _request(
        self,
        method: str,
        path_or_url: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[object | None, Mapping[str, str]]:
        url = self._url(path_or_url)
        data = None
        if payload is not None:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._auth_value}",
            "User-Agent": "quant-engine-issue-guard",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        outgoing = request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with self._opener(outgoing, timeout=self.timeout_seconds) as response:
                status_value = getattr(response, "status", 200)
                status = int(status_value)
                if status >= 400:
                    raise GitHubApiError("GITHUB_API_HTTP_ERROR", status=status)
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise GitHubApiError("GITHUB_API_RESPONSE_INVALID") from exc
                    if declared_length > MAX_RESPONSE_BYTES:
                        raise GitHubApiError("GITHUB_API_RESPONSE_TOO_LARGE")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {
                    str(key): str(value) for key, value in response.headers.items()
                }
        except error.HTTPError as exc:
            raise GitHubApiError("GITHUB_API_HTTP_ERROR", status=exc.code) from None
        except error.URLError:
            raise GitHubApiError("GITHUB_API_NETWORK_ERROR") from None
        except (TimeoutError, OSError):
            raise GitHubApiError("GITHUB_API_NETWORK_ERROR") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GitHubApiError("GITHUB_API_RESPONSE_TOO_LARGE")
        if not raw:
            return None, response_headers
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubApiError("GITHUB_API_RESPONSE_INVALID") from None
        return decoded, response_headers

    @staticmethod
    def _next_link(headers: Mapping[str, str]) -> str | None:
        link = next(
            (value for key, value in headers.items() if key.casefold() == "link"),
            None,
        )
        if link is None:
            return None
        matches = _LINK_NEXT.findall(link)
        if len(matches) > 1:
            raise GitHubApiError("PAGINATION_LINK_INVALID")
        return matches[0] if matches else None

    def _get_paginated(self, path_or_url: str) -> list[Mapping[str, object]]:
        next_url: str | None = self._url(path_or_url)
        visited: set[str] = set()
        items: list[Mapping[str, object]] = []
        pages = 0
        while next_url is not None:
            canonical_url = self._url(next_url)
            if canonical_url in visited:
                raise GitHubApiError("PAGINATION_LINK_REPEATED")
            if pages >= self.max_pages:
                raise GitHubApiError("PAGINATION_BOUND_EXCEEDED")
            visited.add(canonical_url)
            pages += 1
            payload, headers = self._request("GET", canonical_url)
            if not isinstance(payload, list):
                raise GitHubApiError("GITHUB_API_RESPONSE_INVALID")
            for item in payload:
                if not isinstance(item, dict):
                    raise GitHubApiError("GITHUB_API_RESPONSE_INVALID")
                items.append(item)
            next_url = self._next_link(headers)
        return items

    def list_issues(self) -> Sequence[Mapping[str, object]]:
        return self._get_paginated(
            f"/repos/{self.owner}/{self.repo}/issues?state=all&per_page=100&page=1"
        )

    def list_blocked_by(self, issue_number: int) -> Sequence[Mapping[str, object]]:
        return self._get_paginated(
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"
            "/dependencies/blocked_by?per_page=100&page=1"
        )

    def reopen_issue(self, issue_number: int) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
            {"state": "open", "state_reason": "reopened"},
        )

    def add_blocker(self, issue_number: int, blocker_database_id: int) -> None:
        self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"
            "/dependencies/blocked_by",
            {"issue_id": blocker_database_id},
        )

    def remove_blocker(self, issue_number: int, blocker_database_id: int) -> None:
        self._request(
            "DELETE",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"
            f"/dependencies/blocked_by/{blocker_database_id}",
        )


def _error_report(
    mode: Literal["audit", "reconcile"], error_code: str, status: int | None
) -> GuardReport:
    return GuardReport(
        mode=mode,
        ok=False,
        fingerprint="",
        issues=(),
        diagnostics=(Diagnostic(error_code, status=status),),
    )


def _render_text(report: GuardReport) -> str:
    outcome = "PASS" if report.ok else "FAIL"
    lines = [f"issue-guard: {outcome}"]
    for diagnostic in report.diagnostics:
        fields = [diagnostic.code]
        if diagnostic.issue_number is not None:
            fields.append(f"issue=#{diagnostic.issue_number}")
        if diagnostic.blocker_number is not None:
            fields.append(f"blocker=#{diagnostic.blocker_number}")
        if diagnostic.status is not None:
            fields.append(f"status={diagnostic.status}")
        lines.append(" ".join(fields))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or reconcile governed GitHub Issue dependencies."
    )
    parser.add_argument("mode", choices=("audit", "reconcile"))
    parser.add_argument(
        "--repository",
        help="owner/repository; defaults to GITHUB_REPOSITORY",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    mode: Literal["audit", "reconcile"] = arguments.mode
    try:
        client = GitHubClient.from_environment(
            repository=arguments.repository,
            max_pages=arguments.max_pages,
            timeout_seconds=arguments.timeout_seconds,
        )
        if mode == "audit":
            report = audit(client, client.owner, client.repo)
        else:
            report = reconcile(client, client.owner, client.repo)
    except GuardConfigurationError as exc:
        report = _error_report(mode, exc.code, None)
    except GitHubApiError as exc:
        report = _error_report(mode, exc.code, exc.status)
    except Exception:
        # A malformed remote response or unexpected runtime failure must not
        # print raw exception material that could contain issue or API data.
        report = _error_report(mode, "INTERNAL_ERROR", None)
    if arguments.format == "json":
        print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    else:
        print(_render_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
