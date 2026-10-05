import json
import os
import sqlite3
import tempfile
import unittest

import backfill


class BackfillTests(unittest.TestCase):
    def test_session_list_is_oldest_first_with_stable_tie_breaker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "opencode.db")
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE session (id TEXT, title TEXT, directory TEXT, "
                "time_created INTEGER);"
                "CREATE TABLE message (id TEXT, session_id TEXT);"
            )
            connection.executemany(
                "INSERT INTO session VALUES (?, ?, ?, ?)",
                [("z", "new", "/z", 2),
                 ("b", "tie b", "/b", 1),
                 ("a", "tie a", "/a", 1)],
            )
            connection.commit()
            connection.close()

            original = backfill.DB_PATH
            backfill.DB_PATH = path
            try:
                self.assertEqual(
                    [row["id"] for row in backfill.session_list()],
                    ["a", "b", "z"],
                )
            finally:
                backfill.DB_PATH = original

    def test_provenance_is_added_once(self):
        session = {
            "id": "session-1",
            "time_created": 0,
            "directory": "/project",
        }
        document = "# Result\n"
        result = backfill.with_provenance(session, document)
        self.assertIn("Session ID: session-1", result)
        self.assertEqual(backfill.with_provenance(session, result), result)

    def test_chunks_preserve_order(self):
        self.assertEqual(
            list(backfill.chunks(list(range(7)), 3)),
            [[0, 1, 2], [3, 4, 5], [6]],
        )

    def test_external_metadata_shape_is_json_serializable(self):
        metadata = [{"session_id": "session-1", "started_at": "1970-01-01"}]
        self.assertEqual(json.loads(json.dumps(metadata)), metadata)

    def test_scrub_removes_complete_private_key_and_json_secret(self):
        source = (
            '"api_key": "secret-value"\n'
            "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"
        )
        result = backfill.scrub(source)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("abc123", result)
        self.assertIn("[REDACTED_PRIVATE_KEY]", result)

    def test_legacy_progress_is_normalized_from_persisted_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            original = backfill.DOCS_DIR
            backfill.DOCS_DIR = directory
            try:
                with open(os.path.join(directory, "kept.md"), "w") as file:
                    file.write("# Kept")
                progress = {
                    "kept": {"status": "remembered"},
                    "failed": {"status": "remember-failed", "error": "timeout"},
                }
                self.assertTrue(backfill.normalize_legacy_progress(progress))
                self.assertEqual(progress["kept"]["status"], "distilled")
                self.assertEqual(progress["failed"]["status"], "error")
            finally:
                backfill.DOCS_DIR = original


if __name__ == "__main__":
    unittest.main()
