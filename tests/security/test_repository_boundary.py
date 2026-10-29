"""Offline, runtime-generated tests for the repository security boundary."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_repository_boundary.py"
SPEC = importlib.util.spec_from_file_location("check_repository_boundary", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load boundary checker")
boundary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = boundary
SPEC.loader.exec_module(boundary)


def _policy_text(fixtures: list[dict[str, object]] | None = None) -> str:
    fixture_blocks: list[str] = []
    for fixture in fixtures or []:
        fixture_blocks.append(
            "\n".join(
                [
                    "[[synthetic_fixture]]",
                    f"path = {json.dumps(fixture['path'])}",
                    f"sha256 = {json.dumps(fixture['sha256'])}",
                    f"bytes = {fixture['bytes']}",
                    f"classification = {json.dumps(fixture.get('classification', 'SYNTHETIC'))}",
                    f"purpose = {json.dumps(fixture.get('purpose', 'offline parser test'))}",
                    f"governing_issue = {json.dumps(fixture.get('governing_issue', '#4 (SEC-001)'))}",
                ]
            )
        )
    fixtures_text = "\n\n".join(fixture_blocks)
    canonical = (REPOSITORY_ROOT / ".policy" / "data-boundary.toml").read_text(
        encoding="utf-8"
    )
    return canonical.rstrip() + (f"\n\n{fixtures_text}\n" if fixtures_text else "\n")


def _xlsx_bytes(
    entries: list[tuple[str | zipfile.ZipInfo, str | bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return buffer.getvalue()


def _minimal_xlsx_entries() -> list[tuple[str | zipfile.ZipInfo, str | bytes]]:
    return [
        (
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        ),
        ("_rels/.rels", "<Relationships/>"),
        ("xl/workbook.xml", "<workbook>SYNTHETIC</workbook>"),
    ]


class RepositoryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="quant-engine-boundary-")
        self.base = Path(self._temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, relative_path: str, data: bytes) -> Path:
        target = self.root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def policy(self, fixtures: list[dict[str, object]] | None = None):
        policy_path = self.base / "boundary.toml"
        policy_path.write_text(_policy_text(fixtures), encoding="utf-8")
        return boundary.load_policy(policy_path)

    @staticmethod
    def codes(findings) -> set[str]:
        return {item.code for item in findings}

    def scan(self, policy, *paths: str):
        entries = [boundary.TrackedEntry("100644", path) for path in paths]
        return boundary.scan_tracked_entries(self.root, policy, entries)

    def test_policy_missing_required_fields_fails_clearly(self) -> None:
        policy_path = self.base / "invalid.toml"
        policy_path.write_text('schema_version = "1"\n', encoding="utf-8")
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_REQUIRED_TABLE_MISSING", caught.exception.code)

    def test_fixture_json_schema_mirrors_the_exact_entry_contract(self) -> None:
        schema = json.loads(
            (
                REPOSITORY_ROOT
                / "schemas"
                / "security"
                / "synthetic-fixture-allowlist.schema.json"
            ).read_text(encoding="utf-8")
        )
        required = {
            "path",
            "sha256",
            "bytes",
            "classification",
            "purpose",
            "governing_issue",
        }
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema", schema["$schema"]
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(required, set(schema["required"]))
        self.assertEqual(required, set(schema["properties"]))
        path_pattern = re.compile(schema["properties"]["path"]["pattern"])
        self.assertIsNotNone(
            path_pattern.fullmatch("tests/fixtures/synthetic/example.csv")
        )
        for invalid_path in (
            "Tests/fixtures/synthetic/example.csv",
            "tests/fixtures/example.csv",
            "tests/fixtures/synthetic/../example.csv",
            "tests/fixtures/synthetic/*.csv",
            "tests/fixtures/synthetic/example.json",
            "tests/fixtures/synthetic/folder /example.csv",
            "tests/fixtures/synthetic/CON.csv",
            "tests/fixtures/synthetic/aux.xlsx",
            "tests/fixtures/synthetic/nested/LPT9.csv",
        ):
            with self.subTest(invalid_path=invalid_path):
                self.assertIsNone(path_pattern.fullmatch(invalid_path))
        self.assertEqual("^[0-9a-f]{64}$", schema["properties"]["sha256"]["pattern"])
        self.assertEqual(boundary.FIVE_MIB, schema["properties"]["bytes"]["maximum"])

    def test_policy_security_floors_cannot_be_weakened_but_allow_additions(
        self,
    ) -> None:
        canonical = _policy_text()
        mutations = (
            canonical.replace('  "dataIBKR",\n', "", 1),
            canonical.replace('  "browser-profile",\n', "", 1),
            canonical.replace('  ".accdb",\n', "", 1),
            canonical.replace('  ".kdbx",\n', "", 1),
            canonical.replace("53514c69746520666f726d6174203300", "00", 1),
            canonical.replace(
                'name = "credential-literal"',
                'name = "credential-literal-weakened"',
                1,
            ),
            canonical.replace(
                'allowed_credential_classes = ["NONE", "OS_CREDENTIAL_REFERENCE", "SECRET_MANAGER_REFERENCE"]',
                'allowed_credential_classes = ["NONE", "PLAINTEXT"]',
                1,
            ),
            canonical.replace(
                'path_format = "repository-relative-posix"',
                'path_format = "relative-posix-ish"',
                1,
            ),
            canonical.replace(
                'path_format = "repository-relative-posix"',
                'path_format = " repository-relative-posix "',
                1,
            ),
        )
        policy_path = self.base / "mutated-policy.toml"
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.assertNotEqual(canonical, mutation)
                policy_path.write_text(mutation, encoding="utf-8")
                with self.assertRaises(boundary.PolicyLoadError):
                    boundary.load_policy(policy_path)

        additive = canonical.replace(
            "forbidden_extensions = [\n",
            'forbidden_extensions = [\n  ".extra-denied",\n',
            1,
        )
        policy_path.write_text(additive, encoding="utf-8")
        policy = boundary.load_policy(policy_path)
        self.assertIn(".extra-denied", policy.repository.forbidden_extensions)

    def test_fixture_glob_and_non_synthetic_classification_are_rejected(self) -> None:
        fixture = {
            "path": "tests/fixtures/synthetic/*.csv",
            "sha256": "0" * 64,
            "bytes": 0,
        }
        policy_path = self.base / "glob.toml"
        policy_path.write_text(_policy_text([fixture]), encoding="utf-8")
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_FIXTURE_PATH_NOT_EXACT", caught.exception.code)

        fixture["path"] = "tests/fixtures/synthetic/example.csv"
        fixture["classification"] = "REDACTED"
        policy_path.write_text(_policy_text([fixture]), encoding="utf-8")
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_FIXTURE_CLASSIFICATION_INVALID", caught.exception.code)

    def test_fixture_root_and_extra_fields_are_rejected(self) -> None:
        fixture = {
            "path": "tests/fixtures/outside.csv",
            "sha256": "0" * 64,
            "bytes": 0,
        }
        policy_path = self.base / "outside.toml"
        policy_path.write_text(_policy_text([fixture]), encoding="utf-8")
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_FIXTURE_OUTSIDE_ROOT", caught.exception.code)

        fixture["path"] = "tests/fixtures/synthetic/example.csv"
        policy_text = _policy_text([fixture]).replace(
            'governing_issue = "#4 (SEC-001)"',
            'governing_issue = "#4 (SEC-001)"\nunexpected = "denied"',
        )
        policy_path.write_text(policy_text, encoding="utf-8")
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_UNKNOWN_FIELD", caught.exception.code)

        provider_fixture = {
            "path": "tests/fixtures/synthetic/provider.csv",
            "sha256": "0" * 64,
            "bytes": 0,
            "governing_issue": "#20 (P123-SEC)",
        }
        policy_path.write_text(_policy_text([provider_fixture]), encoding="utf-8")
        policy = boundary.load_policy(policy_path)
        self.assertIn(provider_fixture["path"], policy.synthetic_fixtures)

        colliding_fixture = dict(provider_fixture)
        colliding_fixture["path"] = "tests/fixtures/synthetic/Provider.csv"
        policy_path.write_text(
            _policy_text([provider_fixture, colliding_fixture]), encoding="utf-8"
        )
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_FIXTURE_CASE_COLLISION", caught.exception.code)

        unsupported_fixture = dict(provider_fixture)
        unsupported_fixture["path"] = "tests/fixtures/synthetic/provider.json"
        policy_path.write_text(_policy_text([unsupported_fixture]), encoding="utf-8")
        with self.assertRaises(boundary.PolicyLoadError) as caught:
            boundary.load_policy(policy_path)
        self.assertEqual("POLICY_FIXTURE_EXTENSION_INVALID", caught.exception.code)

    def test_fixture_contract_rejects_nonexact_hash_path_and_required_fields(
        self,
    ) -> None:
        base_fixture = {
            "path": "tests/fixtures/synthetic/example.csv",
            "sha256": "a" * 64,
            "bytes": 0,
        }
        variants = (
            ({**base_fixture, "sha256": "A" * 64}, "POLICY_FIXTURE_HASH_INVALID"),
            (
                {**base_fixture, "sha256": " " + "a" * 64 + " "},
                "POLICY_FIXTURE_HASH_INVALID",
            ),
            (
                {**base_fixture, "path": " tests/fixtures/synthetic/example.csv "},
                "POLICY_FIXTURE_PATH_NOT_EXACT",
            ),
            (
                {**base_fixture, "path": "Tests/fixtures/synthetic/example.csv"},
                "POLICY_FIXTURE_OUTSIDE_ROOT",
            ),
            ({**base_fixture, "purpose": ""}, "POLICY_FIELD_INVALID"),
            (
                {**base_fixture, "governing_issue": "SEC-001"},
                "POLICY_FIXTURE_ISSUE_INVALID",
            ),
        )
        policy_path = self.base / "fixture-contract.toml"
        for fixture, expected_code in variants:
            with self.subTest(expected_code=expected_code, fixture=fixture):
                policy_path.write_text(_policy_text([fixture]), encoding="utf-8")
                with self.assertRaises(boundary.PolicyLoadError) as caught:
                    boundary.load_policy(policy_path)
                self.assertEqual(expected_code, caught.exception.code)

        complete_text = _policy_text([base_fixture])
        for field in (
            "path",
            "sha256",
            "bytes",
            "classification",
            "purpose",
            "governing_issue",
        ):
            with self.subTest(missing=field):
                filtered = "\n".join(
                    line
                    for line in complete_text.splitlines()
                    if not line.startswith(f"{field} = ")
                )
                policy_path.write_text(filtered + "\n", encoding="utf-8")
                with self.assertRaises(boundary.PolicyLoadError) as caught:
                    boundary.load_policy(policy_path)
                self.assertEqual("POLICY_REQUIRED_FIELD_MISSING", caught.exception.code)

    def test_every_tracked_fixture_requires_an_exact_allowlist_entry(self) -> None:
        paths = (
            "tests/fixtures/synthetic/unlisted.json",
            "tests/fixtures/synthetic/unlisted.txt",
            "tests/fixtures/other/unlisted.txt",
            "packages/p123/tests/fixtures/accounts.txt",
            "fixtures/accounts.txt",
        )
        for path in paths:
            self.write(path, b"SYNTHETIC but not approved")
        findings = self.scan(self.policy(), *paths)
        by_path = {path: set() for path in paths}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        self.assertIn("FIXTURE_NOT_ALLOWLISTED", by_path[paths[0]])
        self.assertIn("FIXTURE_NOT_ALLOWLISTED", by_path[paths[1]])
        self.assertIn("FIXTURE_ROOT_FORBIDDEN", by_path[paths[2]])
        self.assertIn("FIXTURE_ROOT_FORBIDDEN", by_path[paths[3]])
        self.assertIn("FIXTURE_ROOT_FORBIDDEN", by_path[paths[4]])

    def test_path_normalization_rejects_all_escape_forms(self) -> None:
        cases = {
            "/absolute/file.txt": "PATH_ABSOLUTE",
            "C:/drive/file.txt": "PATH_DRIVE_QUALIFIED",
            "//server/share/file.txt": "PATH_UNC",
            "\\\\server\\share\\file.txt": "PATH_UNC",
            "../outside.txt": "PATH_TRAVERSAL",
            "inside/../outside.txt": "PATH_TRAVERSAL",
            "inside\\file.txt": "PATH_NOT_POSIX",
            "inside//file.txt": "PATH_NOT_NORMALIZED",
            "inside/./file.txt": "PATH_NOT_NORMALIZED",
            "inside/fi\x01le.txt": "PATH_CONTROL_CHARACTER",
            "inside/CON.txt": "PATH_WINDOWS_RESERVED",
            "inside/lpt9": "PATH_WINDOWS_RESERVED",
        }
        for raw_path, expected_code in cases.items():
            with self.subTest(raw_path=repr(raw_path)):
                with self.assertRaises(boundary.PathBoundaryError) as caught:
                    boundary.normalize_repository_path(raw_path)
                self.assertEqual(expected_code, caught.exception.code)

    def test_forbidden_roots_and_segments_are_never_allowlisted(self) -> None:
        policy = self.policy()
        paths = [
            "data/item.txt",
            "raw/item.txt",
            "quarantine/item.txt",
            "export/item.txt",
            "report/item.txt",
            "tests/credentials/item.txt",
        ]
        for path in paths:
            self.write(path, b"ordinary text")
        findings = self.scan(policy, *paths)
        by_path = {path: set() for path in paths}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        for path in paths[:5]:
            self.assertIn("PATH_FORBIDDEN_ROOT", by_path[path])
        self.assertIn("PATH_FORBIDDEN_SEGMENT", by_path[paths[-1]])

    def test_current_and_legacy_portfolio123_roots_are_forbidden(self) -> None:
        policy = self.policy()
        paths = [
            "dataPortfolio123/ordinary.txt",
            "data_Portfolio123/ordinary.txt",
        ]
        for path in paths:
            self.write(path, b"ordinary text")
        findings = self.scan(policy, *paths)
        by_path = {path: set() for path in paths}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        for path in paths:
            self.assertIn("PATH_FORBIDDEN_ROOT", by_path[path])

    def test_git_symlink_and_reparse_attribute_have_stable_codes(self) -> None:
        policy = self.policy()
        findings = boundary.scan_tracked_entries(
            self.root, policy, [boundary.TrackedEntry("120000", "linked.txt")]
        )
        self.assertIn("GIT_SYMLINK", self.codes(findings))
        self.assertTrue(boundary.has_reparse_attribute(0x400))
        self.assertFalse(boundary.has_reparse_attribute(0))

    def test_filesystem_symlink_escape_is_rejected_where_supported(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "linked.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
        findings = self.scan(self.policy(), "linked.txt")
        self.assertIn("FS_SYMLINK", self.codes(findings))

    def test_prohibited_extensions_and_renamed_magic_are_rejected(self) -> None:
        self.write("capture.har", b"{}")
        self.write("ledger.sqlite", b"not a database")
        self.write("table.csv", b"column\nvalue\n")
        self.write("book.xlsx", b"not a workbook")
        self.write("capture.HAR~", b"{}")
        self.write("capture.HAR~.txt", b"{}")
        self.write("private.PEM~", b"not a key")
        self.write("ledger.sqlite-wal", b"sidecar")
        self.write(".sqlite-shm", b"sidecar")
        self.write("ledger.db-journal", b"sidecar")
        self.write("ledger.sqlite-wal.txt", b"renamed sidecar")
        self.write("ledger.db-journal.bak", b"renamed sidecar")
        self.write("#ledger.sqlite#", b"backup")
        self.write("token", b"bare sensitive locator")
        self.write("secret", b"bare sensitive locator")
        self.write("password", b"bare sensitive locator")
        self.write("authorization", b"bare sensitive locator")
        self.write("api_key", b"bare sensitive locator")
        for wrapped_sensitive_name in (
            ".netrc.bak",
            ".git-credentials.backup",
            ".pgpass.old",
            ".pypirc.txt",
            "_netrc.bak",
            "token.bak",
            "password.txt",
            "api_key.old",
            "secret.backup",
            "authorization.txt",
        ):
            self.write(wrapped_sensitive_name, b"wrapped credential store")
        for credential_store in (
            ".netrc",
            "_netrc",
            ".git-credentials",
            ".pgpass",
            ".pypirc",
        ):
            self.write(credential_store, b"credential store")
        sqlite_magic = bytes.fromhex("53514c69746520666f726d6174203300")
        self.write("renamed-sqlite.txt", sqlite_magic + b"synthetic bytes")
        zip_magic = bytes.fromhex("504b0304")
        self.write("renamed-archive.bin", zip_magic + b"synthetic bytes")
        paths = (
            "capture.har",
            "ledger.sqlite",
            "table.csv",
            "book.xlsx",
            "capture.HAR~",
            "capture.HAR~.txt",
            "private.PEM~",
            "ledger.sqlite-wal",
            ".sqlite-shm",
            "ledger.db-journal",
            "ledger.sqlite-wal.txt",
            "ledger.db-journal.bak",
            "#ledger.sqlite#",
            "token",
            "secret",
            "password",
            "authorization",
            "api_key",
            ".netrc.bak",
            ".git-credentials.backup",
            ".pgpass.old",
            ".pypirc.txt",
            "_netrc.bak",
            "token.bak",
            "password.txt",
            "api_key.old",
            "secret.backup",
            "authorization.txt",
            ".netrc",
            "_netrc",
            ".git-credentials",
            ".pgpass",
            ".pypirc",
            "renamed-sqlite.txt",
            "renamed-archive.bin",
        )
        findings = self.scan(self.policy(), *paths)
        by_path = {path: set() for path in paths}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        for path in (
            "capture.har",
            "ledger.sqlite",
            "capture.HAR~",
            "capture.HAR~.txt",
            "private.PEM~",
            "ledger.sqlite-wal",
            ".sqlite-shm",
            "ledger.db-journal",
            "ledger.sqlite-wal.txt",
            "ledger.db-journal.bak",
            "#ledger.sqlite#",
            "token",
            "secret",
            "password",
            "authorization",
            "api_key",
            ".netrc.bak",
            ".git-credentials.backup",
            ".pgpass.old",
            ".pypirc.txt",
            "_netrc.bak",
            "token.bak",
            "password.txt",
            "api_key.old",
            "secret.backup",
            "authorization.txt",
            ".netrc",
            "_netrc",
            ".git-credentials",
            ".pgpass",
            ".pypirc",
        ):
            self.assertIn("FILE_EXTENSION_NEVER_ALLOWED", by_path[path])
        for path in ("table.csv", "book.xlsx"):
            self.assertIn("FILE_EXTENSION_FORBIDDEN", by_path[path])
        self.assertIn("BOUNDARY_MAGIC_SQLITE", by_path["renamed-sqlite.txt"])
        self.assertIn("BOUNDARY_MAGIC_ZIP", by_path["renamed-archive.bin"])

    def test_har_content_is_rejected_after_renaming(self) -> None:
        key_log = "".join(("l", "o", "g"))
        key_entries = "".join(("ent", "ries"))
        key_request = "".join(("req", "uest"))
        key_response = "".join(("res", "ponse"))
        har_document = {
            key_log: {
                key_entries: [
                    {key_request: {"url": "example"}, key_response: {"status": 200}}
                ]
            }
        }
        self.write("renamed-capture.txt", json.dumps(har_document).encode("utf-8"))
        findings = self.scan(self.policy(), "renamed-capture.txt")
        self.assertIn("CONTENT_HAR", self.codes(findings))

    def test_generated_credential_session_account_private_key_and_lfs_are_rejected(
        self,
    ) -> None:
        credential = "".join(("s", "k", "_", "synthetic_", "A" * 24))
        session_header = "".join(("Coo", "kie: sess", "ionid=", "B" * 24))
        account_field = "".join(("external_", "account_", "number=", "123456789"))
        private_key = "".join(
            ("-----BEGIN ", "PRIVATE ", "KEY-----", "\n", "synthetic")
        )
        lfs_pointer = "".join(("version https://git-", "lfs.github.com/spec/v1", "\n"))
        generated = {
            "credential.txt": credential,
            "session.txt": session_header,
            "account.txt": account_field,
            "private.txt": private_key,
            "pointer.txt": lfs_pointer,
        }
        for path, value in generated.items():
            self.write(path, value.encode("utf-8"))
        findings = self.scan(self.policy(), *generated)
        by_path = {path: set() for path in generated}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        self.assertIn("BOUNDARY_CONTENT_CREDENTIAL_PREFIX", by_path["credential.txt"])
        self.assertIn("BOUNDARY_CONTENT_COOKIE", by_path["session.txt"])
        self.assertIn("BOUNDARY_CONTENT_EXTERNAL_ACCOUNT", by_path["account.txt"])
        self.assertIn("BOUNDARY_CONTENT_PRIVATE_KEY", by_path["private.txt"])
        self.assertIn("BOUNDARY_CONTENT_GIT_LFS_POINTER", by_path["pointer.txt"])

    def test_diagnostics_redact_credential_like_paths_and_error_text(self) -> None:
        sentinel = "".join(("s", "k", "_", "Z" * 24))
        sensitive_values = (
            sentinel,
            "".join(("API_", "KEY=", "V" * 24)),
            "".join(("account", "Number=", "123456789")),
            "".join(("Bearer ", "A" * 24)),
            "".join(("session", "Token=", "B" * 24)),
            "".join(("Coo", "kie=", "C" * 24)),
            "".join(("api_", "key_", "TOPSECRETVALUE123456")),
            "".join(("to", "ken_", "TOPSECRETVALUE123456")),
            "".join(("sec", "ret-", "TOPSECRETVALUE123456")),
            "".join(("account", "Number_", "TOPSECRETVALUE123456")),
            "".join(("to", "ken-", "supersecretvalue")),
            "".join(("MATRIXPORT_", "ACCESS_", "KEY=", "D" * 24)),
            "".join(("BIT_", "SECRET_", "KEY=", "E" * 24)),
            "".join(("SCHWAB_", "PRIVATE_", "KEY=", "F" * 24)),
            "".join(("P123_", "CLIENT_", "SECRET=", "G" * 24)),
            "".join(("p123", "Api", "Key=", "H" * 24)),
            "".join(("matrixport", "Private", "Key=", "I" * 24)),
            "".join(("IBKR_", "ACCOUNT_", "ID=", "U1234567")),
        )
        sensitive_paths = tuple(f"docs/{value}.txt" for value in sensitive_values)
        for sensitive_path in sensitive_paths:
            self.write(sensitive_path, b"ordinary documentation")
        findings = self.scan(self.policy(), *sensitive_paths)
        by_path = {path: set() for path in sensitive_paths}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        for sensitive_path in sensitive_paths:
            self.assertIn("PATH_SENSITIVE_LOCATOR", by_path[sensitive_path])

        for output_format in ("text", "json"):
            with self.subTest(output_format=output_format):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    boundary._print_findings(findings, output_format)
                rendered = stream.getvalue()
                for sensitive_value, sensitive_path in zip(
                    sensitive_values, sensitive_paths, strict=True
                ):
                    self.assertNotIn(sensitive_value, rendered)
                    self.assertNotIn(sensitive_path, rendered)
                self.assertIn("redacted-path:", rendered)

        for sensitive_value in sensitive_values:
            with self.subTest(stderr_value=sensitive_value):
                error = boundary.PolicyLoadError(
                    "POLICY_UNREADABLE",
                    f"field/{sensitive_value}",
                    f"detail/{sensitive_value}",
                )
                stderr = io.StringIO()
                with mock.patch.object(
                    boundary, "list_git_tracked_entries", side_effect=error
                ):
                    with redirect_stderr(stderr):
                        self.assertEqual(2, boundary.main(["--root", str(self.root)]))
                self.assertNotIn(sensitive_value, stderr.getvalue())
                self.assertIn("<redacted:", stderr.getvalue())

    def test_exact_synthetic_fixture_is_the_only_extension_exception(self) -> None:
        fixture_path = "tests/fixtures/synthetic/synthetic-table.csv"
        content = b"SYNTHETIC\nlabel,value\nalpha,1\n"
        self.write(fixture_path, content)
        fixture = {
            "path": fixture_path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        findings = self.scan(self.policy([fixture]), fixture_path)
        self.assertEqual([], findings)

        self.write("tests/fixtures/synthetic/unlisted.csv", content)
        findings = self.scan(
            self.policy([fixture]),
            fixture_path,
            "tests/fixtures/synthetic/unlisted.csv",
        )
        self.assertIn("FILE_EXTENSION_FORBIDDEN", self.codes(findings))

    def test_exact_synthetic_xlsx_is_a_bounded_container_exception(self) -> None:
        fixture_path = "tests/fixtures/synthetic/workbook.xlsx"
        content = _xlsx_bytes(_minimal_xlsx_entries())
        self.write(fixture_path, content)
        fixture = {
            "path": fixture_path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        self.assertEqual([], self.scan(self.policy([fixture]), fixture_path))

        sensitive_field = "".join(
            ("SYNTHETIC external_", "account_", "number=", "123456789")
        )
        sensitive_entries = _minimal_xlsx_entries()
        sensitive_entries[-1] = (
            "xl/workbook.xml",
            f"<workbook>{sensitive_field}</workbook>",
        )
        sensitive_content = _xlsx_bytes(sensitive_entries)
        self.write(fixture_path, sensitive_content)
        sensitive_fixture = {
            "path": fixture_path,
            "sha256": hashlib.sha256(sensitive_content).hexdigest(),
            "bytes": len(sensitive_content),
        }
        self.assertIn(
            "BOUNDARY_CONTENT_EXTERNAL_ACCOUNT",
            self.codes(self.scan(self.policy([sensitive_fixture]), fixture_path)),
        )

    def test_synthetic_xlsx_rejects_active_external_and_opaque_members(self) -> None:
        fixture_path = "tests/fixtures/synthetic/workbook.xlsx"
        symlink = zipfile.ZipInfo("xl/symlink.xml")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        symlink.compress_type = zipfile.ZIP_DEFLATED
        invalid_variants = {
            "required-directory": [
                *_minimal_xlsx_entries()[:-1],
                ("xl/workbook.xml/", b""),
            ],
            "zip-symlink": [*_minimal_xlsx_entries(), (symlink, b"target")],
            "macro": [*_minimal_xlsx_entries(), ("xl/vbaProject.bin", b"opaque")],
            "embedding": [
                *_minimal_xlsx_entries(),
                ("xl/embeddings/object1.xml", "<object>SYNTHETIC</object>"),
            ],
            "external-relationship": [
                _minimal_xlsx_entries()[0],
                (
                    "_rels/.rels",
                    '<Relationships><Relationship TargetMode="External" '
                    'Target="https://example.invalid/data"/></Relationships>',
                ),
                _minimal_xlsx_entries()[2],
            ],
            "nested-signature": [
                *_minimal_xlsx_entries(),
                (
                    "xl/opaque.xml",
                    bytes.fromhex("53514c69746520666f726d6174203300"),
                ),
            ],
            "malformed-xml": [
                *_minimal_xlsx_entries()[:-1],
                ("xl/workbook.xml", "<workbook>SYNTHETIC"),
            ],
            "xml-entity": [
                *_minimal_xlsx_entries()[:-1],
                (
                    "xl/workbook.xml",
                    '<!DOCTYPE x [<!ENTITY y "SYNTHETIC">]><workbook>&y;</workbook>',
                ),
            ],
            "case-collision": [
                *_minimal_xlsx_entries(),
                ("XL/WORKBOOK.XML", "<workbook>SYNTHETIC</workbook>"),
            ],
            "formula": [
                *_minimal_xlsx_entries()[:-1],
                (
                    "xl/workbook.xml",
                    '<workbook>SYNTHETIC<f>WEBSERVICE("https://example.invalid/data")</f></workbook>',
                ),
            ],
        }
        for name, entries in invalid_variants.items():
            with self.subTest(name=name):
                content = _xlsx_bytes(entries)
                self.write(fixture_path, content)
                fixture = {
                    "path": fixture_path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
                self.assertIn(
                    "FIXTURE_XLSX_INVALID",
                    self.codes(self.scan(self.policy([fixture]), fixture_path)),
                )

        bzip_content = _xlsx_bytes(
            _minimal_xlsx_entries(), compression=zipfile.ZIP_BZIP2
        )
        self.write(fixture_path, bzip_content)
        bzip_fixture = {
            "path": fixture_path,
            "sha256": hashlib.sha256(bzip_content).hexdigest(),
            "bytes": len(bzip_content),
        }
        self.assertIn(
            "FIXTURE_XLSX_INVALID",
            self.codes(self.scan(self.policy([bzip_fixture]), fixture_path)),
        )

    def test_fixture_hash_size_marker_and_missing_entry_fail_closed(self) -> None:
        fixture_path = "tests/fixtures/synthetic/synthetic.csv"
        content = b"ordinary,content\n"
        self.write(fixture_path, content)
        fixture = {
            "path": fixture_path,
            "sha256": "0" * 64,
            "bytes": len(content) + 1,
        }
        policy = self.policy([fixture])
        findings = self.scan(policy, fixture_path)
        codes = self.codes(findings)
        self.assertIn("FIXTURE_HASH_MISMATCH", codes)
        self.assertIn("FIXTURE_SIZE_MISMATCH", codes)
        self.assertIn("FIXTURE_SYNTHETIC_MARKER_MISSING", codes)

        findings = boundary.scan_tracked_entries(self.root, policy, [])
        self.assertIn("FIXTURE_ALLOWLIST_ENTRY_MISSING", self.codes(findings))

    def test_file_over_five_mib_is_rejected(self) -> None:
        self.write("oversized.txt", b"x" * (5 * 1024 * 1024 + 1))
        findings = self.scan(self.policy(), "oversized.txt")
        self.assertIn("FILE_TOO_LARGE", self.codes(findings))

    def endpoint(self, **overrides):
        record = {
            "provider": "synthetic-provider",
            "name": "read-example",
            "capability": "read-example",
            "method": "GET",
            "path_matcher": "/synthetic/read",
            "semantic_effect": "READ_ONLY",
            "terms_state": "APPROVED",
            "credential_class": "NONE",
            "approval_requirement": "GOVERNING_ISSUE",
            "credential_source": "NONE",
        }
        record.update(overrides)
        return record

    def test_semantically_read_only_endpoint_is_allowed_regardless_of_method(
        self,
    ) -> None:
        endpoint_policy = self.policy().endpoint
        self.assertEqual(
            [], boundary.validate_endpoint_record(self.endpoint(), endpoint_policy)
        )
        self.assertEqual(
            [],
            boundary.validate_endpoint_record(
                self.endpoint(method="POST", name="read-via-post"), endpoint_policy
            ),
        )

    def test_secret_manager_reference_is_allowed_but_literals_still_are_not(
        self,
    ) -> None:
        endpoint_policy = self.policy().endpoint
        manager = self.endpoint(
            credential_class="SECRET_MANAGER_REFERENCE",
            credential_source="SECRET_MANAGER_REFERENCE",
        )
        self.assertEqual(
            [], boundary.validate_endpoint_record(manager, endpoint_policy)
        )
        for bad in ("LITERAL", "PLAINTEXT", "SECRET_MANAGER"):
            with self.subTest(credential_class=bad):
                findings = boundary.validate_endpoint_record(
                    self.endpoint(credential_class=bad), endpoint_policy
                )
                self.assertTrue(findings)

    def test_every_externally_effective_get_is_denied(self) -> None:
        endpoint_policy = self.policy().endpoint
        for effect in sorted(boundary._ENDPOINT_REQUIRED_APPROVAL_EFFECTS):
            with self.subTest(effect=effect):
                record = self.endpoint(
                    name=effect.casefold(), method="GET", semantic_effect=effect
                )
                findings = boundary.validate_endpoint_record(record, endpoint_policy)
                self.assertIn("ENDPOINT_EFFECT_REQUIRES_APPROVAL", self.codes(findings))

    def test_endpoint_literal_credential_unapproved_terms_and_missing_field_fail(
        self,
    ) -> None:
        endpoint_policy = self.policy().endpoint
        literal = self.endpoint(credential_source="literal")
        findings = boundary.validate_endpoint_record(literal, endpoint_policy)
        self.assertIn("ENDPOINT_LITERAL_CREDENTIAL", self.codes(findings))

        embedded = self.endpoint()
        embedded["api_key"] = "".join(("s", "k", "_", "synthetic_", "C" * 24))
        findings = boundary.validate_endpoint_record(embedded, endpoint_policy)
        self.assertIn("ENDPOINT_LITERAL_CREDENTIAL", self.codes(findings))
        self.assertIn("ENDPOINT_UNKNOWN_FIELD", self.codes(findings))
        self.assertIn("ENDPOINT_CREDENTIAL_MATERIAL", self.codes(findings))

        nested = self.endpoint()
        nested["metadata"] = {"renamed": "".join(("s", "k", "_", "D" * 20))}
        findings = boundary.validate_endpoint_record(nested, endpoint_policy)
        self.assertIn("ENDPOINT_UNKNOWN_FIELD", self.codes(findings))
        self.assertIn("ENDPOINT_CREDENTIAL_MATERIAL", self.codes(findings))

        unapproved = self.endpoint(terms_state="UNKNOWN")
        findings = boundary.validate_endpoint_record(unapproved, endpoint_policy)
        self.assertIn("ENDPOINT_TERMS_UNAPPROVED", self.codes(findings))

        missing = self.endpoint()
        del missing["path_matcher"]
        findings = boundary.validate_endpoint_record(missing, endpoint_policy)
        self.assertIn("ENDPOINT_REQUIRED_FIELD_MISSING", self.codes(findings))

        unknown_credential = self.endpoint(credential_class="TYPO")
        self.assertIn(
            "ENDPOINT_CREDENTIAL_CLASS_DEFAULT_DENY",
            self.codes(
                boundary.validate_endpoint_record(unknown_credential, endpoint_policy)
            ),
        )
        unknown_approval = self.endpoint(approval_requirement="UNKNOWN")
        self.assertIn(
            "ENDPOINT_APPROVAL_REQUIREMENT_DEFAULT_DENY",
            self.codes(
                boundary.validate_endpoint_record(unknown_approval, endpoint_policy)
            ),
        )

    def test_live_policy_endpoint_contract_is_exact_and_default_deny(self) -> None:
        policy = boundary.load_policy(
            REPOSITORY_ROOT / ".policy" / "data-boundary.toml"
        )
        self.assertEqual(
            [], boundary.validate_endpoint_record(self.endpoint(), policy.endpoint)
        )
        for matcher in (
            "*",
            "/**",
            ".*",
            "^.*$",
            "(?s)^.*$",
            "(?:.*)",
            "/safe|.*",
            "".join(("/safe?to", "ken=value123")),
            "/safe#fragment",
            " /safe ",
            "/{provider}",
            "/{provider}/{capability}",
            "/../orders",
            "/safe/./route",
        ):
            with self.subTest(matcher=matcher):
                unbounded = self.endpoint(path_matcher=matcher)
                self.assertIn(
                    "ENDPOINT_MATCHER_INVALID",
                    self.codes(
                        boundary.validate_endpoint_record(unbounded, policy.endpoint)
                    ),
                )
        templated = self.endpoint(path_matcher="/accounts/{account_id}/positions")
        self.assertEqual(
            [], boundary.validate_endpoint_record(templated, policy.endpoint)
        )
        endpoint_dimensions = (
            ({"provider": "*"}, "ENDPOINT_PROVIDER_INVALID"),
            ({"capability": "*"}, "ENDPOINT_CAPABILITY_INVALID"),
            ({"method": "*"}, "ENDPOINT_METHOD_INVALID"),
            ({"name": {"nested": "value"}}, "ENDPOINT_OPTIONAL_FIELD_INVALID"),
            ({"id": ["nested"]}, "ENDPOINT_OPTIONAL_FIELD_INVALID"),
        )
        for overrides, expected_code in endpoint_dimensions:
            with self.subTest(overrides=overrides):
                findings = boundary.validate_endpoint_record(
                    self.endpoint(**overrides), policy.endpoint
                )
                self.assertIn(expected_code, self.codes(findings))
        for effect in policy.endpoint.approval_required_effects:
            with self.subTest(live_policy_effect=effect):
                denied = self.endpoint(semantic_effect=effect, method="GET")
                self.assertIn(
                    "ENDPOINT_EFFECT_REQUIRES_APPROVAL",
                    self.codes(
                        boundary.validate_endpoint_record(denied, policy.endpoint)
                    ),
                )

    def test_live_policy_rejects_required_negative_content(self) -> None:
        policy = boundary.load_policy(
            REPOSITORY_ROOT / ".policy" / "data-boundary.toml"
        )
        credential = "".join(("api_", 'key = "', "A" * 24, '"'))
        unquoted_credential = "".join(("API_", "KEY=", "value123"))
        generic_secret = "".join(("sec", "ret=", "value123"))
        credential_prefix = "".join(("s", "k", "_", "A" * 20))
        provider_api_key = "".join(("SCHWAB_", "API_", "KEY=", "value123456789"))
        provider_token = "".join(("P123_", "TO", "KEN=", "value123456789"))
        provider_secret = "".join(("BIT_", "SEC", "RET=", "value123456789"))
        provider_prefix = "".join(("SCHWAB_", "s", "k", "_", "D" * 20))
        provider_authorization = "".join(
            ("SCHWAB_", "Authori", "zation=Bearer ", "E" * 20)
        )
        provider_session_header = "".join(("P123_", "Coo", "kie=", "F" * 20))
        quoted_key = "".join(('"', "api_", "key", '":"', "G" * 20, '"'))
        quoted_provider_key = "".join(
            ('"', "SCHWAB_", "API_", "KEY", '":"', "H" * 20, '"')
        )
        quoted_camel_key = "".join(('"', "p123", "Api", "Key", '":"', "N" * 20, '"'))
        quoted_camel_private_key = "".join(
            ('"', "matrixport", "Private", "Key", '":"', "O" * 20, '"')
        )
        provider_access_key = "".join(("MATRIXPORT_", "ACCESS_", "KEY=", "I" * 20))
        provider_secret_key = "".join(("BIT_", "SECRET_", "KEY=", "J" * 20))
        alpaca_secret_key = "".join(("ALPACA_", "SECRET_", "KEY=", "K" * 20))
        provider_private_key = "".join(("SCHWAB_", "PRIVATE_", "KEY=", "L" * 20))
        provider_account_id = "".join(("IBKR_", "ACCOUNT_", "ID=", "U1234567"))
        internal_account_id = "".join(("account_", "id=", "INTERNAL-1234"))
        session = "".join(("Coo", "kie: sess", "ionid=", "B" * 24))
        account = "".join(("external_", "account_", "number=", "123456789"))
        camel_account = "".join(("account", "Number=", "123456789"))
        har_document = {
            "".join(("l", "o", "g")): {
                "".join(("ent", "ries")): [
                    {
                        "".join(("req", "uest")): {},
                        "".join(("res", "ponse")): {},
                    }
                ]
            }
        }
        generated = {
            "credential.txt": credential.encode(),
            "unquoted-credential.txt": unquoted_credential.encode(),
            "generic-secret.txt": generic_secret.encode(),
            "credential-prefix.txt": credential_prefix.encode(),
            "provider-api-key.txt": provider_api_key.encode(),
            "provider-token.txt": provider_token.encode(),
            "provider-secret.txt": provider_secret.encode(),
            "provider-prefix.txt": provider_prefix.encode(),
            "provider-authorization.txt": provider_authorization.encode(),
            "provider-cookie.txt": provider_session_header.encode(),
            "quoted-key.json": quoted_key.encode(),
            "quoted-provider-key.json": quoted_provider_key.encode(),
            "quoted-camel-key.json": quoted_camel_key.encode(),
            "quoted-camel-private-key.json": quoted_camel_private_key.encode(),
            "provider-access-key.txt": provider_access_key.encode(),
            "provider-secret-key.txt": provider_secret_key.encode(),
            "alpaca-secret-key.txt": alpaca_secret_key.encode(),
            "provider-private-key.txt": provider_private_key.encode(),
            "provider-account-id.txt": provider_account_id.encode(),
            "internal-account-id.txt": internal_account_id.encode(),
            "session.txt": session.encode(),
            "account.txt": account.encode(),
            "camel-account.txt": camel_account.encode(),
            "renamed-sqlite.txt": bytes.fromhex("53514c69746520666f726d6174203300"),
            "renamed-har.txt": json.dumps(har_document).encode(),
            "unlisted.csv": b"column\nvalue\n",
            "unlisted.xlsx": b"not a workbook",
            ".token": b"hidden credential artifact",
            ".env.backup": b"hidden environment artifact",
        }
        for path, content in generated.items():
            self.write(path, content)
        paths = tuple(generated)
        findings = self.scan(policy, *paths)
        by_path = {path: set() for path in paths}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        self.assertIn("BOUNDARY_CONTENT_CREDENTIAL", by_path["credential.txt"])
        self.assertIn("BOUNDARY_CONTENT_CREDENTIAL", by_path["unquoted-credential.txt"])
        self.assertIn("BOUNDARY_CONTENT_CREDENTIAL", by_path["generic-secret.txt"])
        self.assertIn(
            "BOUNDARY_CONTENT_CREDENTIAL_PREFIX", by_path["credential-prefix.txt"]
        )
        for provider_path in (
            "provider-api-key.txt",
            "provider-token.txt",
            "provider-secret.txt",
        ):
            self.assertIn("BOUNDARY_CONTENT_CREDENTIAL", by_path[provider_path])
        self.assertIn(
            "BOUNDARY_CONTENT_CREDENTIAL_PREFIX", by_path["provider-prefix.txt"]
        )
        self.assertIn(
            "BOUNDARY_CONTENT_AUTH_HEADER", by_path["provider-authorization.txt"]
        )
        self.assertIn("BOUNDARY_CONTENT_COOKIE", by_path["provider-cookie.txt"])
        for credential_path in (
            "quoted-key.json",
            "quoted-provider-key.json",
            "quoted-camel-key.json",
            "quoted-camel-private-key.json",
            "provider-access-key.txt",
            "provider-secret-key.txt",
            "alpaca-secret-key.txt",
            "provider-private-key.txt",
        ):
            self.assertIn("BOUNDARY_CONTENT_CREDENTIAL", by_path[credential_path])
        self.assertIn(
            "BOUNDARY_CONTENT_EXTERNAL_ACCOUNT", by_path["provider-account-id.txt"]
        )
        self.assertNotIn(
            "BOUNDARY_CONTENT_EXTERNAL_ACCOUNT", by_path["internal-account-id.txt"]
        )
        self.assertIn("BOUNDARY_CONTENT_COOKIE", by_path["session.txt"])
        self.assertIn("BOUNDARY_CONTENT_EXTERNAL_ACCOUNT", by_path["account.txt"])
        self.assertIn("BOUNDARY_CONTENT_EXTERNAL_ACCOUNT", by_path["camel-account.txt"])
        self.assertIn("BOUNDARY_MAGIC_SQLITE", by_path["renamed-sqlite.txt"])
        self.assertIn("CONTENT_HAR", by_path["renamed-har.txt"])
        self.assertIn("FILE_EXTENSION_FORBIDDEN", by_path["unlisted.csv"])
        self.assertIn("FILE_EXTENSION_FORBIDDEN", by_path["unlisted.xlsx"])
        self.assertIn("FILE_EXTENSION_NEVER_ALLOWED", by_path[".token"])
        self.assertIn("FILE_EXTENSION_NEVER_ALLOWED", by_path[".env.backup"])

    def test_content_scanning_covers_governed_unicode_encodings_and_fails_closed(
        self,
    ) -> None:
        policy = self.policy()
        credential = "".join(('"', "SCHWAB_", "API_", "KEY", '":"', "M" * 20, '"'))
        encoded_files = {
            "encoded-utf-8.txt": credential.encode("utf-8"),
            "encoded-utf-16-bom.txt": credential.encode("utf-16"),
            "encoded-utf-16-le.txt": credential.encode("utf-16-le"),
            "encoded-utf-16-be.txt": credential.encode("utf-16-be"),
            "encoded-utf-32-bom.txt": credential.encode("utf-32"),
            "encoded-utf-32-le.txt": credential.encode("utf-32-le"),
            "encoded-utf-32-be.txt": credential.encode("utf-32-be"),
            "benign-utf-8.txt": b"ordinary repository documentation\n",
            "opaque-nul.bin": b"\x00\xff\x00\x01\x02",
            "malformed-bom.bin": b"\xff\xfe\x00",
        }
        for path, content in encoded_files.items():
            self.write(path, content)
        findings = self.scan(policy, *encoded_files)
        by_path = {path: set() for path in encoded_files}
        for finding in findings:
            by_path[finding.path].add(finding.code)
        for credential_path in tuple(encoded_files)[:7]:
            with self.subTest(path=credential_path):
                self.assertIn("BOUNDARY_CONTENT_CREDENTIAL", by_path[credential_path])
                self.assertNotIn(
                    "CONTENT_ENCODING_UNSUPPORTED", by_path[credential_path]
                )
        self.assertEqual(set(), by_path["benign-utf-8.txt"])
        self.assertIn("CONTENT_ENCODING_UNSUPPORTED", by_path["opaque-nul.bin"])
        self.assertIn("CONTENT_ENCODING_UNSUPPORTED", by_path["malformed-bom.bin"])

    def test_git_index_parser_handles_spaces_and_rejects_nonzero_stage(self) -> None:
        object_id = "a" * 40
        payload = f"100644 {object_id} 0\tdocs/file with spaces.md\0".encode()
        self.assertEqual(
            [boundary.TrackedEntry("100644", "docs/file with spaces.md", object_id)],
            boundary.parse_git_ls_files(payload),
        )
        with self.assertRaises(boundary.RepositoryScanError):
            boundary.parse_git_ls_files(
                f"100644 {object_id} 2\tconflict.txt\0".encode()
            )

        sha256_id = "b" * 64
        parsed = boundary.parse_git_ls_files(
            f"100644 {sha256_id} 0\tsha256-object.txt\0".encode()
        )
        self.assertEqual(sha256_id, parsed[0].object_id)
        for invalid_id in ("deadbeef", "A" * 40, "g" * 40):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaises(boundary.RepositoryScanError):
                    boundary.parse_git_ls_files(
                        f"100644 {invalid_id} 0\tinvalid.txt\0".encode()
                    )

    def test_git_batch_parsers_are_binary_exact_and_fail_closed(self) -> None:
        first = "a" * 40
        second = "b" * 64
        metadata = f"{first} blob 3\n{second} blob 0\n".encode()
        self.assertEqual(
            {first: 3, second: 0},
            boundary.parse_git_cat_file_batch_check(metadata, [first, second]),
        )
        payload = (
            f"{first} blob 3\n".encode() + b"a\nb\n" + f"{second} blob 0\n\n".encode()
        )
        self.assertEqual(
            {first: b"a\nb", second: b""},
            boundary.parse_git_cat_file_batch(payload, [first, second]),
        )
        invalid_payloads = (
            (payload, [second, first]),
            (payload[:-1], [first, second]),
            (payload + b"extra", [first, second]),
            (f"{first} tree 0\n\n".encode(), [first]),
            (f"{first} blob nope\n".encode(), [first]),
        )
        for invalid_payload, expected in invalid_payloads:
            with self.subTest(expected=expected, size=len(invalid_payload)):
                with self.assertRaises(boundary.RepositoryScanError):
                    boundary.parse_git_cat_file_batch(invalid_payload, expected)

    def test_staged_blob_bytes_are_authoritative_over_worktree_bytes(self) -> None:
        path = "docs/staged.txt"
        self.write(path, b"benign worktree bytes")
        object_id = "c" * 40
        entry = boundary.TrackedEntry("100644", path, object_id)
        sqlite_magic = bytes.fromhex("53514c69746520666f726d6174203300")
        findings = boundary.scan_tracked_entries(
            self.root,
            self.policy(),
            [entry],
            staged_blobs={
                path: boundary.IndexBlob(object_id, len(sqlite_magic), sqlite_magic)
            },
        )
        self.assertIn("BOUNDARY_MAGIC_SQLITE", self.codes(findings))

        missing = boundary.scan_tracked_entries(
            self.root, self.policy(), [entry], staged_blobs={}
        )
        self.assertIn("GIT_INDEX_BLOB_MISSING", self.codes(missing))

        oversized = boundary.scan_tracked_entries(
            self.root,
            self.policy(),
            [entry],
            staged_blobs={
                path: boundary.IndexBlob(object_id, boundary.FIVE_MIB + 1, None)
            },
        )
        self.assertIn("FILE_TOO_LARGE", self.codes(oversized))

    def test_staged_control_inputs_require_semantic_text_parity(self) -> None:
        path = "control.toml"
        staged_content = b'kind = "SYNTHETIC"\n'
        self.write(path, staged_content)
        object_id = "d" * 40
        entries = [boundary.TrackedEntry("100644", path, object_id)]
        blobs = {
            path: boundary.IndexBlob(object_id, len(staged_content), staged_content)
        }
        self.assertEqual(
            staged_content,
            boundary.require_staged_control_parity(
                self.root,
                self.root / path,
                entries,
                blobs,
                control_name="policy",
            ),
        )

        self.write(path, staged_content.replace(b"\n", b"\r\n"))
        self.assertEqual(
            staged_content,
            boundary.require_staged_control_parity(
                self.root,
                self.root / path,
                entries,
                blobs,
                control_name="policy",
            ),
        )

        self.write(path, b'kind = "DRIFTED"\n')
        with self.assertRaises(boundary.RepositoryScanError) as caught:
            boundary.require_staged_control_parity(
                self.root,
                self.root / path,
                entries,
                blobs,
                control_name="policy",
            )
        self.assertEqual("CONTROL_FILE_DRIFT:policy", str(caught.exception))

    def test_endpoint_record_wrapper_is_exact(self) -> None:
        endpoint_path = self.base / "endpoints.json"
        exact = {"endpoints": [self.endpoint()]}
        endpoint_path.write_text(json.dumps(exact), encoding="utf-8")
        self.assertEqual(
            exact["endpoints"], boundary._load_endpoint_records(endpoint_path)
        )

        credential_key = "".join(("api_", "key"))
        extra = {**exact, credential_key: "".join(("synthetic", "value123"))}
        endpoint_path.write_text(json.dumps(extra), encoding="utf-8")
        with self.assertRaises(boundary.RepositoryScanError) as caught:
            boundary._load_endpoint_records(endpoint_path)
        self.assertIn("wrapper must contain only", str(caught.exception))

    def test_json_cli_success_output_is_one_machine_readable_document(self) -> None:
        policy = self.policy()
        with (
            mock.patch.object(boundary, "list_git_tracked_entries", return_value=[]),
            mock.patch.object(boundary, "read_git_index_blobs", return_value={}),
            mock.patch.object(
                boundary, "require_staged_control_parity", return_value=b""
            ),
            mock.patch.object(boundary, "load_policy", return_value=policy),
            mock.patch.object(boundary, "scan_tracked_entries", return_value=[]),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    0,
                    boundary.main(["--root", str(self.root), "--format", "json"]),
                )
        self.assertEqual([], json.loads(stdout.getvalue()))

    def test_git_top_level_rejects_repository_subdirectory(self) -> None:
        with self.assertRaises(boundary.RepositoryScanError):
            boundary._verify_git_top_level(REPOSITORY_ROOT / "docs")

    def test_ordinary_source_and_document_files_pass(self) -> None:
        self.write("src/module.py", b"def value():\n    return 1\n")
        self.write("docs/guide.md", b"# Guide\n\nOrdinary documentation.\n")
        findings = self.scan(self.policy(), "src/module.py", "docs/guide.md")
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
