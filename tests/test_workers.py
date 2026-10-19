import asyncio
import tempfile
import unittest
from pathlib import Path

from judge.database import Database
from judge.problems import ProblemSet
from judge.server import JudgeService


ROOT = Path(__file__).resolve().parents[1]


class MeasuringEngine:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def judge(self, problem, source):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return {"verdict": "AC", "score": 100, "test_results": [], "message": None}


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_workers_judge_queued_submissions_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "judge.sqlite3")
            user_id = (
                await asyncio.to_thread(database.register, "worker", "correct horse battery")
            )["user_id"]
            submission_ids = [
                database.create_submission(user_id, "sum-two-integers", "int main() {}")
                for _ in range(4)
            ]
            engine = MeasuringEngine()
            service = JudgeService(database, ProblemSet(ROOT / "problems"), engine)
            service.start_workers(count=2)
            try:
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline:
                    states = [database.get_submission(user_id, item)["status"] for item in submission_ids]
                    if all(state == "FINISHED" for state in states):
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(states, ["FINISHED"] * 4)
                self.assertEqual(engine.max_active, 2)
            finally:
                await service.stop_workers()


if __name__ == "__main__":
    unittest.main()

