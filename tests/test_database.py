import tempfile
import unittest
from pathlib import Path

from judge.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "judge.sqlite3"
        self.database = Database(self.database_path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_registration_login_and_token_validation(self):
        account = self.database.register("alice", "correct horse battery")
        authenticated = self.database.authenticate("ALICE", "correct horse battery")
        self.assertEqual(account["username"], "alice")
        self.assertIsNotNone(authenticated)
        user_id, token = authenticated
        self.assertEqual(user_id, account["user_id"])
        self.assertEqual(self.database.user_for_token(token), user_id)
        self.assertIsNone(self.database.authenticate("alice", "wrong password"))
        self.assertIsNone(self.database.user_for_token("invalid-token"))

    def test_duplicate_username_is_rejected_case_insensitively(self):
        self.database.register("Alice", "correct horse battery")
        with self.assertRaisesRegex(ValueError, "USERNAME_TAKEN"):
            self.database.register("alice", "another correct password")

    def test_submission_is_private_and_queue_survives_restart(self):
        owner = self.database.register("owner", "correct horse battery")["user_id"]
        other = self.database.register("other", "another correct password")["user_id"]
        submission_id = self.database.create_submission(owner, "sum-two-integers", "int main() {}")
        self.assertEqual(self.database.get_submission(owner, submission_id)["status"], "QUEUED")
        self.assertIsNone(self.database.get_submission(other, submission_id))
        claimed = self.database.claim_next_submission()
        self.assertEqual(claimed["id"], submission_id)
        restarted = Database(self.database_path)
        self.assertEqual(restarted.get_submission(owner, submission_id)["status"], "QUEUED")
        self.assertEqual(restarted.claim_next_submission()["id"], submission_id)


if __name__ == "__main__":
    unittest.main()

