"""Offline tests for the governed GitHub Issue dependency guard."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from typing import Any
import unittest
from unittest import mock
from urllib import error


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "issue_guard.py"
SPEC = importlib.util.spec_from_file_location("issue_guard", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load issue guard")
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)

OWNER = "example-owner"
REPO = "example-repo"
REPOSITORY_URL = f"https://api.github.com/repos/{OWNER}/{REPO}"


def _body(blocked_by: str, *, heading: str = "## Blocked by") -> str:
    return "\n".join(
        (
            "## Outcome",
            "Synthetic governance test.",
            "",
            heading,
            blocked_by,
            "",
            "## Acceptance evidence",
            "Synthetic offline evidence.",
        )
    )


def _issue(
    number: int,
    key: str,
    blocked_by: str = "None",
    *,
    state: str = "open",
    database_id: int | None = None,
    title: str | None = None,
    body: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, object]:
    resolved_labels = labels
    if resolved_labels is None:
        resolved_labels = ["backlog:active"]
    return {
        "number": number,
        "id": database_id if database_id is not None else 10_000 + number,
        "title": title if title is not None else f"[{key}] Synthetic item",
        "body": body if body is not None else _body(blocked_by),
        "state": state,
        "labels": [{"name": label} for label in resolved_labels],
    }


def _pull_request(number: int) -> dict[str, object]:
    result = _issue(number, f"PR-{number:03d}")
    result["pull_request"] = {"url": "https://example.invalid/pull"}
    return result


def _native(
    number: int,
    *,
    database_id: int | None = None,
    repository_url: str = REPOSITORY_URL,
) -> dict[str, object]:
    return {
        "number": number,
        "id": database_id if database_id is not None else 10_000 + number,
        "repository_url": repository_url,
    }


class FakeClient:
    """Stateful, dependency-injected GitHub client with no network access."""

    def __init__(
        self,
        issues: list[dict[str, object]],
        native: dict[int, list[dict[str, object]]] | None = None,
    ) -> None:
        self.issues = deepcopy(issues)
        self.native = deepcopy(native or {})
        self.calls: list[tuple[object, ...]] = []
        self.list_issue_count = 0
        self.change_on_second_read: tuple[int, str, object] | None = None
        self.fail_add = False
        self.ignore_add = False
        self.ignore_remove = False

    def list_issues(self) -> list[dict[str, object]]:
        self.list_issue_count += 1
        if self.list_issue_count == 2 and self.change_on_second_read is not None:
            number, field, value = self.change_on_second_read
            for item in self.issues:
                if item.get("number") == number:
                    item[field] = value
                    break
        return deepcopy(self.issues)

    def list_blocked_by(self, issue_number: int) -> list[dict[str, object]]:
        return deepcopy(self.native.get(issue_number, []))

    def reopen_issue(self, issue_number: int) -> None:
        self.calls.append(("reopen", issue_number))
        for item in self.issues:
            if item.get("number") == issue_number:
                item["state"] = "open"
                return
        raise AssertionError("unknown synthetic issue")

    def add_blocker(self, issue_number: int, blocker_database_id: int) -> None:
        self.calls.append(("add", issue_number, blocker_database_id))
        if self.fail_add:
            raise RuntimeError("synthetic add failure")
        if self.ignore_add:
            return
        blocker = next(
            item for item in self.issues if item.get("id") == blocker_database_id
        )
        self.native.setdefault(issue_number, []).append(
            _native(
                int(blocker["number"]),
                database_id=blocker_database_id,
            )
        )

    def remove_blocker(self, issue_number: int, blocker_database_id: int) -> None:
        self.calls.append(("remove", issue_number, blocker_database_id))
        if self.ignore_remove:
            return
        self.native[issue_number] = [
            item
            for item in self.native.get(issue_number, [])
            if item.get("id") != blocker_database_id
        ]


def _diagnostic_codes(report: Any) -> set[str]:
    return {diagnostic.code for diagnostic in report.diagnostics}


class ParserAndGraphTests(unittest.TestCase):
    def test_none_legacy_none_and_checkbox_references(self) -> None:
        for value in ("None", "None."):
            with self.subTest(value=value):
                references, diagnostics = guard.parse_blocked_by(_body(value), 1)
                self.assertEqual(references, ())
                self.assertEqual(diagnostics, ())

        references, diagnostics = guard.parse_blocked_by(
            _body(
                "\n".join(
                    (
                        "#2 (GOV-002)",
                        "- #3 (SEC-001)",
                        "- [ ] #4 (CI-001)",
                        "- [x] #5 (CORE-001)",
                        "- [X] #6 (HAR-001)",
                    )
                ),
                heading="### Blocked by",
            ),
            1,
        )
        self.assertEqual(diagnostics, ())
        self.assertEqual(
            [(item.number, item.key) for item in references],
            [
                (2, "GOV-002"),
                (3, "SEC-001"),
                (4, "CI-001"),
                (5, "CORE-001"),
                (6, "HAR-001"),
            ],
        )

    def test_fenced_and_commented_headings_are_ignored(self) -> None:
        body = "\n".join(
            (
                "```markdown",
                "## Blocked by",
                "#99 (FAKE-099)",
                "```",
                "<!--",
                "## Blocked by",
                "#98 (FAKE-098)",
                "-->",
                "## Blocked by",
                "None",
                "## Outcome",
                "Synthetic.",
            )
        )
        references, diagnostics = guard.parse_blocked_by(body, 1)
        self.assertEqual(references, ())
        self.assertEqual(diagnostics, ())

    def test_comment_delimiters_inside_fences_cannot_hide_real_section(
        self,
    ) -> None:
        body = "\n".join(
            (
                "```text",
                "<!--",
                "```",
                "## Blocked by",
                "#2 (A-002)",
                "-->",
                "```text",
                "## Blocked by",
                "None",
                "```",
                "## Outcome",
                "Synthetic.",
            )
        )
        references, diagnostics = guard.parse_blocked_by(body, 1)
        self.assertEqual(
            [(item.number, item.key) for item in references], [(2, "A-002")]
        )
        self.assertIn("BLOCKED_BY_LINE_INVALID", {item.code for item in diagnostics})

    def test_invalid_backtick_fence_info_cannot_hide_real_section(self) -> None:
        body = "\n".join(
            (
                "```bad`info",
                "## Blocked by",
                "#2 (A-002)",
                "```",
                "## Blocked by",
                "None",
                "```",
            )
        )
        references, diagnostics = guard.parse_blocked_by(body, 1)
        self.assertEqual(
            [(item.number, item.key) for item in references], [(2, "A-002")]
        )
        self.assertEqual(diagnostics, ())

    def test_html_delimiters_inside_inline_code_do_not_hide_headings(self) -> None:
        body = "\n".join(
            (
                "`<!--`",
                "## Blocked by",
                "#2 (A-002)",
                "`-->`",
                "## Blocked by",
                "None",
            )
        )
        _, diagnostics = guard.parse_blocked_by(body, 1)
        self.assertEqual(
            {item.code for item in diagnostics},
            {"BLOCKED_BY_SECTION_DUPLICATE"},
        )

    def test_multiline_code_span_cannot_start_a_false_html_comment(self) -> None:
        body = "\n".join(
            (
                "`<!--",
                "synthetic text",
                "`",
                "## Blocked by",
                "#2 (A-002)",
                "-->",
                "## Blocked by",
                "None",
            )
        )
        _, diagnostics = guard.parse_blocked_by(body, 1)
        self.assertEqual(
            {item.code for item in diagnostics},
            {"BLOCKED_BY_SECTION_DUPLICATE"},
        )

    def test_comment_removal_cannot_synthesize_a_fence(self) -> None:
        body = "\n".join(
            (
                "<!--synthetic-->```",
                "## Blocked by",
                "#2 (A-002)",
                "```",
                "## Blocked by",
                "None",
                "```",
            )
        )
        references, diagnostics = guard.parse_blocked_by(body, 1)
        self.assertEqual(
            [(item.number, item.key) for item in references], [(2, "A-002")]
        )
        self.assertEqual(diagnostics, ())

    def test_missing_duplicate_malformed_mixed_and_duplicate_lines(self) -> None:
        cases = {
            "missing": ("## Outcome\nSynthetic", "BLOCKED_BY_SECTION_MISSING"),
            "duplicate": (
                "## Blocked by\nNone\n## Blocked by\nNone",
                "BLOCKED_BY_SECTION_DUPLICATE",
            ),
            "malformed": (_body("depends on #2"), "BLOCKED_BY_LINE_INVALID"),
            "mixed": (
                _body("None\n- #2 (GOV-002)"),
                "BLOCKED_BY_LINE_INVALID",
            ),
            "duplicate-line": (
                _body("#2 (GOV-002)\n- [x] #2 (GOV-002)"),
                "BLOCKER_DUPLICATE",
            ),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name=name):
                _, diagnostics = guard.parse_blocked_by(body, 1)
                self.assertIn(expected, {item.code for item in diagnostics})

    def test_missing_pr_mismatch_duplicate_key_and_self_reference(self) -> None:
        issues = [
            _issue(
                1,
                "GOV-001",
                "\n".join(
                    (
                        "#1 (GOV-001)",
                        "#2 (WRONG-002)",
                        "#3 (PR-003)",
                        "#99 (MISS-099)",
                    )
                ),
            ),
            _issue(2, "GOV-002"),
            _pull_request(3),
            _issue(4, "GOV-002"),
        ]
        client = FakeClient(issues)
        report = guard.audit(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertTrue(
            {
                "BLOCKER_SELF_REFERENCE",
                "BLOCKER_KEY_MISMATCH",
                "BLOCKER_PR_REFERENCE",
                "BLOCKER_MISSING",
                "ISSUE_KEY_DUPLICATE",
                "DEPENDENCY_CYCLE",
            }.issubset(_diagnostic_codes(report))
        )
        self.assertEqual(client.calls, [])

    def test_two_three_and_disconnected_cycles_are_detected(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002", "#1 (A-001)"),
            _issue(3, "B-003", "#4 (B-004)"),
            _issue(4, "B-004", "#5 (B-005)"),
            _issue(5, "B-005", "#3 (B-003)"),
            _issue(6, "C-006"),
        ]
        report = guard.audit(FakeClient(issues), OWNER, REPO)
        cycles = [
            item.related_numbers
            for item in report.diagnostics
            if item.code == "DEPENDENCY_CYCLE"
        ]
        self.assertEqual(cycles, [(1, 2), (3, 4, 5)])

    def test_cycle_detection_handles_a_deep_acyclic_graph_iteratively(self) -> None:
        adjacency = {number: (number + 1,) for number in range(1, 2_000)}
        adjacency[2_000] = ()
        self.assertEqual(guard._cycle_diagnostics(adjacency), ())

    def test_effective_open_status_propagates_through_closed_chain(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)", state="closed"),
            _issue(2, "A-002", "#3 (A-003)", state="closed"),
            _issue(3, "A-003", state="open"),
            _issue(4, "A-004", "#1 (A-001)", state="open"),
            _issue(5, "A-005", state="closed"),
        ]
        native = {
            1: [_native(2)],
            2: [_native(3)],
            4: [_native(1)],
        }
        report = guard.audit(FakeClient(issues, native), OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertEqual(
            [
                item.issue_number
                for item in report.diagnostics
                if item.code == "ILLEGAL_CLOSED"
            ],
            [1, 2],
        )
        statuses = {item.number: item.status for item in report.issues}
        self.assertEqual(
            statuses,
            {
                1: "ILLEGAL_CLOSED",
                2: "ILLEGAL_CLOSED",
                3: "GRAPH_READY",
                4: "BLOCKED",
                5: "CLOSED",
            },
        )

    def test_only_active_issues_can_be_graph_ready(self) -> None:
        issues = [
            _issue(1, "A-001", labels=["backlog:active"]),
            _issue(2, "A-002", labels=["backlog:next"]),
            _issue(3, "A-003", labels=["backlog:deferred"]),
            _issue(4, "A-004", labels=["backlog:folded"]),
        ]
        report = guard.audit(FakeClient(issues), OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(
            {item.number: item.status for item in report.issues},
            {
                1: "GRAPH_READY",
                2: "BACKLOG_NEXT",
                3: "BACKLOG_DEFERRED",
                4: "BACKLOG_FOLDED",
            },
        )
        self.assertEqual(
            {item.number: item.backlog_state for item in report.issues},
            {1: "active", 2: "next", 3: "deferred", 4: "folded"},
        )

    def test_open_issue_requires_exactly_one_backlog_state(self) -> None:
        cases = {
            "missing": ([], "BACKLOG_STATE_MISSING"),
            "multiple": (
                ["backlog:active", "backlog:next"],
                "BACKLOG_STATE_MULTIPLE",
            ),
        }
        for name, (labels, expected) in cases.items():
            with self.subTest(name=name):
                report = guard.audit(
                    FakeClient([_issue(1, "A-001", labels=labels)]), OWNER, REPO
                )
                self.assertFalse(report.ok)
                self.assertIn(expected, _diagnostic_codes(report))

    def test_closed_issue_may_have_no_backlog_state(self) -> None:
        report = guard.audit(
            FakeClient([_issue(1, "A-001", state="closed", labels=[])]),
            OWNER,
            REPO,
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.issues[0].status, "CLOSED")
        self.assertIsNone(report.issues[0].backlog_state)


class ReconciliationTests(unittest.TestCase):
    def test_exact_mirror_has_no_writes(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002"),
        ]
        client = FakeClient(issues, {1: [_native(2)]})
        report = guard.reconcile(client, OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(report.mutations, ())
        self.assertEqual(client.calls, [])

    def test_reopen_order_is_blocker_nearest_first(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)", state="closed"),
            _issue(2, "A-002", "#3 (A-003)", state="closed"),
            _issue(3, "A-003", state="open"),
        ]
        client = FakeClient(issues, {1: [_native(2)], 2: [_native(3)]})
        report = guard.reconcile(client, OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(client.calls, [("reopen", 2), ("reopen", 1)])

    def test_reopen_order_honors_blockers_in_a_shortcut_graph(self) -> None:
        issues = [
            _issue(
                1,
                "A-001",
                "#2 (A-002)\n#3 (A-003)",
                state="closed",
            ),
            _issue(2, "A-002", "#3 (A-003)", state="closed"),
            _issue(3, "A-003", state="open"),
        ]
        client = FakeClient(
            issues,
            {1: [_native(2), _native(3)], 2: [_native(3)]},
        )
        report = guard.reconcile(client, OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(client.calls, [("reopen", 2), ("reopen", 1)])

    def test_add_precedes_remove_and_database_ids_are_used(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)", database_id=5001),
            _issue(2, "A-002", database_id=9002),
            _issue(3, "A-003", database_id=9003),
        ]
        client = FakeClient(
            issues,
            {1: [_native(3, database_id=9003)]},
        )
        report = guard.reconcile(client, OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(
            client.calls,
            [("add", 1, 9002), ("remove", 1, 9003)],
        )

    def test_cycle_breaking_removal_precedes_reversed_dependency_adds(self) -> None:
        issues = [
            _issue(1, "A-001", "#3 (A-003)"),
            _issue(2, "A-002", "#3 (A-003)"),
            _issue(3, "A-003"),
        ]
        client = FakeClient(
            issues,
            {
                2: [_native(1)],
                3: [_native(2)],
            },
        )
        report = guard.reconcile(client, OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(
            client.calls,
            [
                ("remove", 2, 10_001),
                ("remove", 3, 10_002),
                ("add", 1, 10_003),
                ("add", 2, 10_003),
            ],
        )

    def test_unobserved_cycle_breaking_removal_prevents_adds(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002"),
        ]
        client = FakeClient(issues, {2: [_native(1)]})
        client.ignore_remove = True
        report = guard.reconcile(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertEqual(client.calls, [("remove", 2, 10_001)])
        self.assertEqual(_diagnostic_codes(report), {"REMOVAL_VERIFICATION_FAILED"})

    def test_add_failure_prevents_every_removal(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002"),
            _issue(3, "A-003"),
        ]
        client = FakeClient(issues, {1: [_native(3)]})
        client.fail_add = True
        report = guard.reconcile(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertEqual(client.calls, [("add", 1, 10_002)])
        self.assertNotIn("remove", {str(call[0]) for call in client.calls})
        self.assertEqual(_diagnostic_codes(report), {"MUTATION_FAILED"})

    def test_unobserved_add_prevents_every_removal(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002"),
            _issue(3, "A-003"),
        ]
        client = FakeClient(issues, {1: [_native(3)]})
        client.ignore_add = True
        report = guard.reconcile(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertEqual(client.calls, [("add", 1, 10_002)])
        self.assertEqual(_diagnostic_codes(report), {"ADD_VERIFICATION_FAILED"})
        self.assertEqual(client.native[1], [_native(3)])

    def test_native_number_and_database_id_must_describe_same_issue(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002", database_id=12_002),
            _issue(3, "A-003", database_id=12_003),
        ]
        client = FakeClient(
            issues,
            {1: [_native(3, database_id=12_002)]},
        )
        report = guard.audit(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertIn("NATIVE_RECORD_IDENTITY_MISMATCH", _diagnostic_codes(report))

    def test_duplicate_issue_database_id_fails_before_native_reads(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002", database_id=12_002),
            _issue(3, "A-003", database_id=12_002),
        ]
        client = FakeClient(issues)
        report = guard.reconcile(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertIn("ISSUE_DATABASE_ID_DUPLICATE", _diagnostic_codes(report))
        self.assertEqual(client.calls, [])

    def test_cross_repository_native_only_dependency_is_removed(self) -> None:
        issues = [_issue(1, "A-001")]
        client = FakeClient(
            issues,
            {
                1: [
                    _native(
                        9,
                        database_id=77_009,
                        repository_url="https://api.github.com/repos/else/where",
                    )
                ]
            },
        )
        report = guard.reconcile(client, OWNER, REPO)
        self.assertTrue(report.ok)
        self.assertEqual(client.calls, [("remove", 1, 77_009)])

    def test_audit_mode_never_mutates_a_mismatch(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002"),
        ]
        client = FakeClient(issues)
        report = guard.audit(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertEqual(_diagnostic_codes(report), {"NATIVE_MIRROR_MISMATCH"})
        self.assertEqual(client.calls, [])

    def test_invalid_graph_causes_zero_writes_and_zero_native_reads(self) -> None:
        issues = [_issue(1, "A-001", "not a governed reference")]
        client = FakeClient(issues)
        with mock.patch.object(
            client,
            "list_blocked_by",
            side_effect=AssertionError("native mirror must not be read"),
        ):
            report = guard.reconcile(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertIn("BLOCKED_BY_LINE_INVALID", _diagnostic_codes(report))
        self.assertEqual(client.calls, [])

    def test_concurrent_body_title_state_or_label_change_aborts_before_writes(
        self,
    ) -> None:
        changes: list[tuple[str, object]] = [
            ("body", _body("None.")),
            ("title", "[A-001] Changed synthetic title"),
            ("state", "closed"),
            ("labels", [{"name": "backlog:next"}]),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                client = FakeClient([_issue(1, "A-001")])
                client.change_on_second_read = (1, field, value)
                report = guard.reconcile(client, OWNER, REPO)
                self.assertFalse(report.ok)
                self.assertEqual(_diagnostic_codes(report), {"CONCURRENT_STATE_CHANGE"})
                self.assertEqual(client.calls, [])

    def test_post_reconcile_full_audit_failure_is_reported(self) -> None:
        issues = [
            _issue(1, "A-001", "#2 (A-002)"),
            _issue(2, "A-002"),
        ]
        client = FakeClient(issues)
        client.ignore_add = True
        report = guard.reconcile(client, OWNER, REPO)
        self.assertFalse(report.ok)
        self.assertIn("POSTCONDITION_FAILED", _diagnostic_codes(report))
        self.assertIn("NATIVE_MIRROR_MISMATCH", _diagnostic_codes(report))


class FakeResponse:
    def __init__(
        self,
        payload: object | None,
        *,
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._raw = (
            b""
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    def read(self, limit: int = -1) -> bytes:
        return self._raw if limit < 0 else self._raw[:limit]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class QueueOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def __call__(self, outgoing: object, *, timeout: float) -> object:
        self.requests.append(outgoing)
        if not self.responses:
            raise AssertionError("unexpected synthetic HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _auth_value() -> str:
    return "".join(("synthetic", "-", "http", "-", "auth"))


class HttpClientTests(unittest.TestCase):
    def test_pagination_is_bounded_and_aggregated(self) -> None:
        second = (
            "https://api.github.com/repos/example-owner/example-repo/issues"
            "?state=all&per_page=100&page=2"
        )
        opener = QueueOpener(
            [
                FakeResponse(
                    [_issue(1, "A-001")],
                    headers={"Link": f'<{second}>; rel="next"'},
                ),
                FakeResponse([_issue(2, "A-002")]),
            ]
        )
        client = guard.GitHubClient(
            auth_value=_auth_value(), owner=OWNER, repo=REPO, opener=opener
        )
        self.assertEqual([item["number"] for item in client.list_issues()], [1, 2])
        self.assertEqual(len(opener.requests), 2)

    def test_repeated_next_link_and_page_bound_fail_closed(self) -> None:
        first = (
            "https://api.github.com/repos/example-owner/example-repo/issues"
            "?state=all&per_page=100&page=1"
        )
        repeated = QueueOpener(
            [FakeResponse([], headers={"Link": f'<{first}>; rel="next"'})]
        )
        client = guard.GitHubClient(
            auth_value=_auth_value(), owner=OWNER, repo=REPO, opener=repeated
        )
        with self.assertRaises(guard.GitHubApiError) as caught:
            client.list_issues()
        self.assertEqual(caught.exception.code, "PAGINATION_LINK_REPEATED")

        second = first[:-1] + "2"
        bounded = QueueOpener(
            [FakeResponse([], headers={"Link": f'<{second}>; rel="next"'})]
        )
        client = guard.GitHubClient(
            auth_value=_auth_value(),
            owner=OWNER,
            repo=REPO,
            opener=bounded,
            max_pages=1,
        )
        with self.assertRaises(guard.GitHubApiError) as caught:
            client.list_issues()
        self.assertEqual(caught.exception.code, "PAGINATION_BOUND_EXCEEDED")

    def test_http_error_codes_do_not_leak_response_content(self) -> None:
        for status in (403, 404, 410, 422, 429, 503):
            with self.subTest(status=status):
                failure = error.HTTPError(
                    "https://api.github.com/synthetic",
                    status,
                    "private response material",
                    {},
                    io.BytesIO(b"private response material"),
                )
                client = guard.GitHubClient(
                    auth_value=_auth_value(),
                    owner=OWNER,
                    repo=REPO,
                    opener=QueueOpener([failure]),
                )
                with self.assertRaises(guard.GitHubApiError) as caught:
                    client.list_issues()
                self.assertEqual(caught.exception.status, status)
                rendered = str(caught.exception)
                self.assertNotIn("private", rendered)
                self.assertEqual(rendered, "GITHUB_API_HTTP_ERROR")

    def test_missing_environment_auth_fails_before_network(self) -> None:
        opener = mock.Mock(side_effect=AssertionError("network must not run"))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(guard.GuardConfigurationError) as caught:
                guard.GitHubClient.from_environment(opener=opener)
        self.assertEqual(caught.exception.code, "GITHUB_TOKEN_MISSING")
        opener.assert_not_called()

    def test_api_base_must_use_https(self) -> None:
        with self.assertRaises(guard.GuardConfigurationError) as caught:
            guard.GitHubClient(
                auth_value=_auth_value(),
                owner=OWNER,
                repo=REPO,
                api_url="http://api.github.test",
                opener=QueueOpener([]),
            )
        self.assertEqual(caught.exception.code, "GITHUB_API_URL_INVALID")

    def test_io_bounds_reject_nonfinite_timeout_and_excessive_pages(self) -> None:
        for timeout in (float("nan"), float("inf"), 61.0):
            with self.subTest(timeout=timeout):
                with self.assertRaises(guard.GuardConfigurationError) as caught:
                    guard.GitHubClient(
                        auth_value=_auth_value(),
                        owner=OWNER,
                        repo=REPO,
                        timeout_seconds=timeout,
                        opener=QueueOpener([]),
                    )
                self.assertEqual(caught.exception.code, "HTTP_TIMEOUT_INVALID")
        with self.assertRaises(guard.GuardConfigurationError) as caught:
            guard.GitHubClient(
                auth_value=_auth_value(),
                owner=OWNER,
                repo=REPO,
                max_pages=101,
                opener=QueueOpener([]),
            )
        self.assertEqual(caught.exception.code, "PAGINATION_BOUND_INVALID")

    def test_redirect_handler_rejects_cross_origin_before_forwarding_auth(
        self,
    ) -> None:
        handler = guard._SameOriginRedirectHandler(("https", "api.github.com"))
        header_name = "".join(("Author", "ization"))
        outgoing = guard.request.Request(
            "https://api.github.com/repos/example-owner/example-repo/issues",
            headers={header_name: "Bearer " + _auth_value()},
        )
        with self.assertRaises(guard.GitHubApiError) as caught:
            handler.redirect_request(
                outgoing,
                None,
                301,
                "moved",
                {},
                "https://example.invalid/capture",
            )
        self.assertEqual(caught.exception.code, "REDIRECT_ORIGIN_INVALID")

    def test_mutation_requests_use_only_approved_payloads_and_database_ids(
        self,
    ) -> None:
        opener = QueueOpener(
            [FakeResponse(None), FakeResponse(None), FakeResponse(None)]
        )
        client = guard.GitHubClient(
            auth_value=_auth_value(), owner=OWNER, repo=REPO, opener=opener
        )
        client.reopen_issue(7)
        client.add_blocker(7, 88_008)
        client.remove_blocker(7, 88_008)

        requests = opener.requests
        self.assertEqual(
            [item.get_method() for item in requests],
            ["PATCH", "POST", "DELETE"],
        )
        self.assertEqual(
            json.loads(requests[0].data.decode("utf-8")),
            {"state": "open", "state_reason": "reopened"},
        )
        self.assertEqual(
            json.loads(requests[1].data.decode("utf-8")),
            {"issue_id": 88_008},
        )
        self.assertIsNone(requests[2].data)
        self.assertTrue(requests[2].full_url.endswith("/88008"))
        for item in requests:
            self.assertEqual(item.get_header("X-github-api-version"), guard.API_VERSION)


if __name__ == "__main__":
    unittest.main()
