import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.languages import LANGUAGES, build_key, language_profile
from ott.api import ApiError, make_app
from ott.sandbox import InfraError, LocalSandbox
from ott.store import SCHEMA_VERSION, Store, StoreError


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "int main(void) { return 0; }\n"
DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()


class LanguageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "ott.db")
        with self.store.tx() as con:
            con.execute("INSERT INTO users(id,username,password_hash,role,created_at) VALUES (1,'player','unused','CONTESTANT',0)")
        self.tournament = self.store.create_tournament("Language Cup", 300, 1)
        self.tid = self.tournament["id"]
        self.entry = self.store.create_entry(self.tid, 1)
        self.eid = self.entry["id"]
        self.received_at = time.time()
        with self.store.tx() as con:
            con.execute("UPDATE tournaments SET status='CODING',coding_started_at=?,coding_deadline=? WHERE id=?",
                        (int(self.received_at) - 5, int(self.received_at) + 3600, self.tid))
        self.app = make_app(self.store, self.root, ROOT / "mockup")

    def route(self, method, path, *, payload=None, query=None):
        context = SimpleNamespace(user={"id": 1, "role": "CONTESTANT"}, query=query or {},
                                  received_at=time.time(), json=lambda: payload or {})
        for candidate, pattern, handler, _ in self.app.routes:
            match = pattern.fullmatch(path)
            if candidate == method and match:
                return handler(context, **match.groupdict())
        self.fail(f"Route missing: {method} {path}")

    def reserve(self, language):
        return self.store.reserve_submission(self.eid, self.tid, 1, SOURCE,
                                             self.received_at, language=language)

    def test_profiles_and_cache_keys_are_fixed_and_distinct(self):
        self.assertEqual(set(LANGUAGES), {"cpp17", "c17", "rust2021", "go", "java17"})
        self.assertEqual(build_key(DIGEST), DIGEST)
        self.assertEqual(len({build_key(DIGEST, item) for item in LANGUAGES}), 5)
        for invalid in (None, [], {}, "python", "c17; id", "../../bot.c"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                language_profile(invalid)
        for profile in LANGUAGES.values():
            command = profile.command("/src/" + profile.filename, "/build/bot")
            self.assertEqual(command[0], profile.compiler)
            output_flag = "-d" if profile.artifact_format == "jar" else "-o"
            self.assertEqual(command[-3:], [output_flag, "/build/bot", "/src/" + profile.filename])

    def test_version_two_database_migrates_language_columns_and_preserves_sources(self):
        self.store.save_draft(self.eid, SOURCE)
        receipt = self.reserve("cpp17")
        with self.store.tx() as con:
            con.execute("ALTER TABLE entries DROP COLUMN draft_language")
            con.execute("ALTER TABLE submission_receipts DROP COLUMN language")
            con.execute("DROP INDEX users_email_idx")
            con.execute("DROP TABLE email_verification_tokens")
            con.execute("ALTER TABLE users DROP COLUMN email_verified_at")
            con.execute("ALTER TABLE users DROP COLUMN email")
            con.execute("PRAGMA user_version = 2")
        migrated = Store(self.root / "ott.db")
        self.assertEqual(migrated.get_draft(self.eid, with_language=True), (SOURCE, int(self.received_at), "cpp17"))
        current = migrated.get_pending_submission(self.eid)
        self.assertEqual((current["id"], current["source"], current["language"]), (receipt["id"], SOURCE, "cpp17"))
        with migrated.tx() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_migration_keeps_new_language_and_rejects_unknown_future_schema(self):
        self.store.save_draft(self.eid, SOURCE, "c17")
        Store(self.root / "ott.db")
        self.assertEqual(self.store.get_draft(self.eid, with_language=True)[2], "c17")
        with self.store.tx() as con:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Store(self.root / "ott.db")

    def test_templates_match_language_and_remain_plain_text(self):
        for language, profile in LANGUAGES.items():
            with self.subTest(language=language):
                status, (content_type, source) = self.route("GET", "/bot-template", query={"language": [language]})
                template = ROOT / "samples" / ("bot_template" + Path(profile.filename).suffix)
                self.assertEqual(status, 200)
                self.assertEqual(content_type, "text/plain; charset=utf-8")
                self.assertEqual(source, template.read_text(encoding="utf-8"))

    def test_unsupported_language_is_rejected_before_draft_write(self):
        for language in (None, [], {}, "python", "c17; id"):
            with self.subTest(language=language), self.assertRaises(ApiError) as error:
                self.route("PUT", f"/tournaments/{self.tid}/code", payload={"source": SOURCE, "language": language})
            self.assertEqual(error.exception.code, "UNSUPPORTED_LANGUAGE")
        self.assertEqual(self.store.get_draft(self.eid, with_language=True), (None, None, "cpp17"))

    def test_draft_read_returns_matching_source_and_language(self):
        self.route("PUT", f"/tournaments/{self.tid}/code", payload={"source": SOURCE, "language": "c17"})
        _, payload = self.route("GET", f"/tournaments/{self.tid}/code")
        self.assertEqual((payload["source"], payload["language"]), (SOURCE, "c17"))
        self.assertEqual({item["id"] for item in payload["languages"]}, set(LANGUAGES))

    def test_receipt_replay_is_language_bound_and_immutable(self):
        receipt = self.reserve("c17")
        self.assertEqual(self.reserve("c17")["id"], receipt["id"])
        with self.assertRaises(StoreError) as error:
            self.reserve("cpp17")
        self.assertEqual(error.exception.code, "SUBMISSION_PENDING")
        with self.assertRaises(StoreError):
            self.store.save_draft(self.eid, SOURCE, "rust2021")
        self.assertEqual(self.store.get_pending_submission(self.eid)["language"], "c17")

    def test_auto_submit_uses_language_read_inside_transaction(self):
        self.store.save_draft(self.eid, SOURCE, "c17")
        with self.store.tx() as con:
            con.execute("UPDATE tournaments SET coding_deadline=? WHERE id=?", (int(time.time()) - 1, self.tid))
        receipt = self.store.reserve_submission(self.eid, self.tid, 1, "stale source", time.time(),
                                                auto=True, language="rust2021")
        self.assertEqual((receipt["source"], receipt["language"]), (SOURCE, "c17"))

    def test_finalization_rejects_language_change_and_keeps_original_language(self):
        receipt = self.reserve("c17")
        with self.assertRaises(StoreError) as error:
            self.store.finalize_entry(self.eid, self.tid, 1, receipt["bot_id"], SOURCE, language="cpp17")
        self.assertEqual(error.exception.code, "RECEIPT_CONFLICT")
        self.assertTrue(self.store.finalize_entry(self.eid, self.tid, 1, receipt["bot_id"], SOURCE, language="c17"))
        self.assertEqual(self.store.get_bot(receipt["bot_id"])["language"], "c17")

    def test_success_in_other_language_does_not_bypass_compile_quota(self):
        with self.store.tx() as con:
            con.execute("UPDATE entries SET compile_count=10,last_ok_sha=? WHERE id=?", (DIGEST, self.eid))
        with self.assertRaises(StoreError) as error:
            self.reserve("c17")
        self.assertEqual(error.exception.code, "COMPILE_LIMIT")
        with self.store.tx() as con:
            con.execute("UPDATE entries SET last_ok_sha=? WHERE id=?", (build_key(DIGEST, "c17"), self.eid))
        self.assertEqual(self.reserve("c17")["free_compile"], 1)

    def artifact(self, language):
        sandbox = LocalSandbox()
        source = self.root / sandbox.exe_name
        if os.name == "posix":
            header = bytearray(64)
            header[:7] = b"\x7fELF\x02\x01\x01"
            header[16:18] = (2).to_bytes(2, "little")
        else:
            header = b"MZ" + bytes(62)
        source.write_bytes(header)
        manifest = sandbox._manifest(source, SOURCE, language)
        source.with_name(source.name + ".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return sandbox, source, manifest

    def test_artifact_verification_and_copy_require_matching_language(self):
        sandbox, source, _ = self.artifact("c17")
        self.assertEqual(sandbox.verify_artifact(source, DIGEST, "c17")["language"], "c17")
        dest = self.root / "copied" / sandbox.exe_name
        with self.assertRaisesRegex(InfraError, "language"):
            sandbox.copy_artifact(source, dest, DIGEST, "cpp17")
        self.assertFalse(dest.exists())
        self.assertEqual(sandbox.copy_artifact(source, dest, DIGEST, "c17")["language"], "c17")

    def test_legacy_cpp_manifest_and_tampered_flags(self):
        sandbox, source, manifest = self.artifact("cpp17")
        path = source.with_name(source.name + ".manifest.json")
        del manifest["language"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        sandbox.verify_artifact(source, DIGEST, "cpp17")
        with self.assertRaisesRegex(InfraError, "language"):
            sandbox.verify_artifact(source, DIGEST, "c17")
        manifest["language"] = "rust2021"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(InfraError, "flags"):
            sandbox.verify_artifact(source, DIGEST, "rust2021")
