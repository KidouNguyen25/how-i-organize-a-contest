import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from judge.database import Database
from judge.judge_engine import DockerJudge
from judge.problems import ProblemSet
from judge.protocol import MAX_LINE_BYTES, PROTOCOL_VERSION
from judge.server import JudgeService


ROOT = Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database = Database(Path(self.temporary.name) / "judge.sqlite3")
        self.service = JudgeService(database, ProblemSet(ROOT / "problems"), DockerJudge())
        self.server = await asyncio.start_server(
            self.service.handle_connection,
            "127.0.0.1",
            0,
            limit=MAX_LINE_BYTES + 1,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    async def _connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port, limit=MAX_LINE_BYTES + 1)

    @staticmethod
    def _request(operation, data=None, token=None, request_id="test-1"):
        return {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "op": operation,
            "token": token,
            "data": data or {},
        }

    async def _exchange(self, writer, reader, request):
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        return json.loads((await reader.readline()).decode("utf-8"))

    async def test_register_login_submit_and_private_result(self):
        reader, writer = await self._connect()
        try:
            response = await self._exchange(
                writer,
                reader,
                self._request("REGISTER", {"username": "alice", "password": "correct horse battery"}),
            )
            self.assertTrue(response["ok"])
            login = await self._exchange(
                writer,
                reader,
                self._request("LOGIN", {"username": "alice", "password": "correct horse battery"}),
            )
            token = login["data"]["token"]
            submitted = await self._exchange(
                writer,
                reader,
                self._request(
                    "SUBMIT",
                    {"problem_id": "sum-two-integers", "language": "cpp17", "source": "int main() {}"},
                    token,
                ),
            )
            submission_id = submitted["data"]["submission_id"]
            result = await self._exchange(
                writer,
                reader,
                self._request("GET_SUBMISSION", {"submission_id": submission_id}, token),
            )
            self.assertEqual(result["data"]["submission"]["status"], "QUEUED")
            history = await self._exchange(
                writer,
                reader,
                self._request("LIST_MY_SUBMISSIONS", {"limit": 20}, token),
            )
            self.assertEqual(history["data"]["submissions"][0]["submission_id"], submission_id)

            await self._exchange(
                writer,
                reader,
                self._request("REGISTER", {"username": "bob", "password": "another correct password"}),
            )
            bob_login = await self._exchange(
                writer,
                reader,
                self._request("LOGIN", {"username": "bob", "password": "another correct password"}),
            )
            hidden = await self._exchange(
                writer,
                reader,
                self._request(
                    "GET_SUBMISSION",
                    {"submission_id": submission_id},
                    bob_login["data"]["token"],
                ),
            )
            self.assertEqual(hidden["error"]["code"], "NOT_FOUND")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_public_problem_list_never_returns_test_data(self):
        reader, writer = await self._connect()
        try:
            response = await self._exchange(writer, reader, self._request("LIST_PROBLEMS"))
            problem = response["data"]["problems"][0]
            self.assertNotIn("tests", problem)
            self.assertNotIn("sample_input", problem)
            details = await self._exchange(
                writer,
                reader,
                self._request("GET_PROBLEM", {"problem_id": problem["id"]}),
            )
            self.assertIn("statement", details["data"]["problem"])
            self.assertNotIn("tests", details["data"]["problem"])
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_invalid_json_returns_error_and_keeps_connection(self):
        reader, writer = await self._connect()
        try:
            writer.write(b"{broken json}\n")
            await writer.drain()
            error = json.loads((await reader.readline()).decode("utf-8"))
            self.assertEqual(error["error"]["code"], "INVALID_JSON")
            response = await self._exchange(writer, reader, self._request("LIST_PROBLEMS"))
            self.assertTrue(response["ok"])
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_oversized_line_is_rejected(self):
        reader, writer = await self._connect()
        try:
            writer.write(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
            await writer.drain()
            response = json.loads((await reader.readline()).decode("utf-8"))
            self.assertEqual(response["error"]["code"], "REQUEST_TOO_LARGE")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_submission_requires_login(self):
        reader, writer = await self._connect()
        try:
            response = await self._exchange(
                writer,
                reader,
                self._request(
                    "SUBMIT",
                    {"problem_id": "sum-two-integers", "language": "cpp17", "source": "int main() {}"},
                ),
            )
            self.assertEqual(response["error"]["code"], "AUTH_REQUIRED")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_token_cannot_be_reused_on_another_connection(self):
        reader, writer = await self._connect()
        try:
            await self._exchange(
                writer,
                reader,
                self._request("REGISTER", {"username": "carol", "password": "correct horse battery"}),
            )
            login = await self._exchange(
                writer,
                reader,
                self._request("LOGIN", {"username": "carol", "password": "correct horse battery"}),
            )
            token = login["data"]["token"]
        finally:
            writer.close()
            await writer.wait_closed()

        reader, writer = await self._connect()
        try:
            response = await self._exchange(
                writer,
                reader,
                self._request("LIST_MY_SUBMISSIONS", {"limit": 10}, token),
            )
            self.assertEqual(response["error"]["code"], "AUTH_REQUIRED")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_framing_and_protocol_version_errors(self):
        reader, writer = await self._connect()
        try:
            writer.write(b'{"version":1}\n')
            await writer.drain()
            malformed = json.loads((await reader.readline()).decode("utf-8"))
            self.assertEqual(malformed["error"]["code"], "INVALID_REQUEST")

            wrong_version = self._request("LIST_PROBLEMS")
            wrong_version["version"] = PROTOCOL_VERSION + 1
            response = await self._exchange(writer, reader, wrong_version)
            self.assertEqual(response["error"]["code"], "UNSUPPORTED_VERSION")

            bool_version = self._request("LIST_PROBLEMS")
            bool_version["version"] = True
            response = await self._exchange(writer, reader, bool_version)
            self.assertEqual(response["error"]["code"], "UNSUPPORTED_VERSION")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_source_limit_is_enforced(self):
        reader, writer = await self._connect()
        try:
            await self._exchange(
                writer,
                reader,
                self._request("REGISTER", {"username": "dave", "password": "correct horse battery"}),
            )
            login = await self._exchange(
                writer,
                reader,
                self._request("LOGIN", {"username": "dave", "password": "correct horse battery"}),
            )
            response = await self._exchange(
                writer,
                reader,
                self._request(
                    "SUBMIT",
                    {
                        "problem_id": "sum-two-integers",
                        "language": "cpp17",
                        "source": "x" * (256 * 1024 + 1),
                    },
                    login["data"]["token"],
                ),
            )
            self.assertEqual(response["error"]["code"], "SOURCE_TOO_LARGE")
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()

