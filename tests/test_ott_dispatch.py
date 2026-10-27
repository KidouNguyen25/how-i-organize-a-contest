import json
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from ott import engine
from ott.runner import MatchRunner
from ott.store import MAX_ATTEMPTS, Lease, LeaseLost, Store, StoreError
from ott.tournament import Dispatcher, round_robin


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.db")
        with self.store.tx() as con:
            con.executemany(
                "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,'unused','CONTESTANT',0)",
                [(n, f"player{n}") for n in range(1, 35)],
            )

    def tournament(self, status="RUNNING", players=4):
        tournament = self.store.create_tournament("Dispatch Cup", 300, 1)
        tid = tournament["id"]
        ids = [f"{tid}-entry-{n:02}" for n in range(1, players + 1)]
        with self.store.tx() as con:
            for user_id, eid in enumerate(ids, start=1):
                bot_id = f"{eid}-bot"
                con.execute(
                    "INSERT INTO bot_submissions(id,tournament_id,user_id,language,source_sha256,source_size,compile_status,created_at) "
                    "VALUES (?,?,?,'cpp17',?,0,'READY',0)",
                    (bot_id, tid, user_id, "a" * 64),
                )
                con.execute(
                    "INSERT INTO entries(id,tournament_id,user_id,bot_submission_id,status,registered_at) VALUES (?,?,?,?,'READY',0)",
                    (eid, tid, user_id, bot_id),
                )
        self.store.create_matches(tid, round_robin(ids), 300000)
        self.store.update_tournament(tid, status=status)
        return tid, self.store.list_matches(tid, limit=2000)

    def test_match_runner_passes_each_bot_language_to_runtime(self):
        class StopPlay(Exception):
            pass

        class Adapter:
            def actor(self, state):
                raise StopPlay

        match_runner = object.__new__(MatchRunner)
        match_runner.adapter = Adapter()
        match_runner._check_control = lambda: None
        launched = []

        def bot(exe, side, language):
            launched.append((side, language))
            instance = MagicMock()
            instance.wait_ready.return_value = None
            return instance

        with patch("ott.runner.BotProcess", side_effect=bot):
            with self.assertRaises(StopPlay):
                match_runner._play({"last_event_seq": 0}, {"white": Path("w"), "black": Path("b")},
                                   {}, {}, {"white": 300000, "black": 300000},
                                   {"white": "cpp17", "black": "java17"})
        self.assertEqual(launched, [("white", "cpp17"), ("black", "java17")])

    def test_claim_finds_running_tournament_beyond_blocked_prefix(self):
        blocked_tid, _ = self.tournament("LOCKED", players=34)
        running_tid, matches = self.tournament()
        with self.store.tx() as con:
            con.execute("UPDATE matches SET round_no=round_no+1000 WHERE tournament_id=?", (running_tid,))
        prefix = self.store.list_matches(statuses=("SCHEDULED",))
        self.assertEqual(len(prefix), 500)
        self.assertTrue(all(row["tournament_id"] == blocked_tid for row in prefix))

        match, lease = self.store.claim_next_match("worker")
        self.assertEqual((match["tournament_id"], match["id"]), (running_tid, matches[0]["id"]))
        self.assertEqual(lease, Lease("worker", 1))
        claimed = self.store.get_match(match["id"])
        self.assertEqual((claimed["status"], claimed["attempt_count"]), ("RUNNING", 1))
        snapshot = json.loads(claimed["execution_snapshot"])
        self.assertEqual(snapshot["clock_seconds"], 300)
        self.assertEqual(len(snapshot["entries"]), 2)

    def test_claim_skips_delay_retry_limit_and_missing_bots(self):
        delayed_tid, _ = self.tournament(players=2)
        exhausted_tid, _ = self.tournament(players=2)
        unbuildable_tid, _ = self.tournament(players=2)
        ready_tid, _ = self.tournament(players=2)
        with self.store.tx() as con:
            con.execute("UPDATE matches SET available_at=? WHERE tournament_id=?", (self.store._db_now(con) + 60, delayed_tid))
            con.execute("UPDATE matches SET attempt_count=? WHERE tournament_id=?", (MAX_ATTEMPTS, exhausted_tid))
            con.execute("UPDATE entries SET bot_submission_id=NULL WHERE tournament_id=?", (unbuildable_tid,))
        match, _ = self.store.claim_next_match("worker")
        self.assertEqual(match["tournament_id"], ready_tid)
        for tid in (delayed_tid, exhausted_tid, unbuildable_tid):
            self.assertTrue(all(row["status"] == "SCHEDULED" for row in self.store.list_matches(tid)))

    def test_concurrent_claims_use_distinct_entries_and_obey_global_capacity(self):
        self.tournament()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda n: self.store.claim_next_match(f"worker-{n}", max_running=2), range(6)))
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({match["id"] for match, _ in claimed}), 2)
        entries = [match[key] for match, _ in claimed for key in ("white_entry_id", "black_entry_id")]
        self.assertEqual(len(set(entries)), 4)
        with self.store.tx() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM matches WHERE status='RUNNING'").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM entry_leases").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM match_attempts").fetchone()[0], 2)

    def test_claim_by_id_and_next_claim_share_leases_and_preserve_snapshot_on_retry(self):
        _, matches = self.tournament()
        mid = matches[0]["id"]
        lease = self.store.claim_match(mid, "manual", max_running=2)
        self.assertIsNotNone(lease)
        first = self.store.get_match(mid)
        other, _ = self.store.claim_next_match("automatic", max_running=2)
        first_entries = {first["white_entry_id"], first["black_entry_id"]}
        self.assertFalse(first_entries & {other["white_entry_id"], other["black_entry_id"]})
        self.assertIsNone(self.store.claim_next_match("third", max_running=2))
        self.assertIsNone(self.store.claim_match(matches[-1]["id"], "third", max_running=2))

        self.store.pause_match(mid, "INFRA: START_FAILED", lease=lease, retry=True)
        with self.store.tx() as con:
            con.execute("UPDATE matches SET available_at=0 WHERE id=?", (mid,))
            con.execute("UPDATE entries SET bot_submission_id=NULL WHERE id IN (?,?)", tuple(first_entries))
        retried, next_lease = self.store.claim_next_match("retry", max_running=2)
        self.assertEqual(retried["id"], mid)
        self.assertEqual(next_lease.epoch, lease.epoch + 1)
        self.assertEqual(self.store.get_match(mid)["execution_snapshot"], first["execution_snapshot"])
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(mid, lease)
        self.store.heartbeat(mid, next_lease)

    def test_event_snapshots_compress_losslessly_and_keep_idempotent_replay(self):
        _, matches = self.tournament(players=2)
        match_id = matches[0]["id"]
        lease = self.store.claim_match(match_id, "worker")
        state = engine.new_state()
        action = engine.legal_actions(state)[0]
        side, phase = state["side"], state["phase"]
        engine.apply_action(state, action)
        clocks = {"white": 300000, "black": 300000}
        self.store.record_action(match_id, 1, state, action, side, phase, clocks, lease=lease)
        serialized = json.dumps(state, separators=(",", ":"))
        with self.store.tx() as con:
            event = con.execute("SELECT state_after_json,state_after_zlib FROM match_events WHERE match_id=? AND sequence_no=1",
                                (match_id,)).fetchone()
        self.assertEqual(event["state_after_json"], "")
        self.assertEqual(zlib.decompress(event["state_after_zlib"]).decode("utf-8"), serialized)
        self.assertLess(len(event["state_after_zlib"]), len(serialized.encode("utf-8")))
        self.store.record_action(match_id, 1, state, action, side, phase, clocks, lease=lease)

        conflicting = dict(state, move_count=state["move_count"] + 1)
        with self.assertRaises(StoreError) as error:
            self.store.record_action(match_id, 1, conflicting, action, side, phase, clocks, lease=lease)
        self.assertEqual(error.exception.code, "EVENT_CONFLICT")

    def test_schema_v3_migration_preserves_legacy_event_snapshots(self):
        _, matches = self.tournament(players=2)
        match_id = matches[0]["id"]
        lease = self.store.claim_match(match_id, "worker")
        state = engine.new_state()
        action = engine.legal_actions(state)[0]
        side, phase = state["side"], state["phase"]
        engine.apply_action(state, action)
        clocks = {"white": 300000, "black": 300000}
        self.store.record_action(match_id, 1, state, action, side, phase, clocks, lease=lease)
        serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
        with self.store.tx() as con:
            con.execute("UPDATE match_events SET state_after_json=?,state_after_zlib=NULL WHERE match_id=? AND sequence_no=1",
                        (serialized, match_id))
            con.execute("ALTER TABLE match_events DROP COLUMN state_after_zlib")
            con.execute("DROP INDEX users_email_idx")
            con.execute("DROP TABLE email_verification_tokens")
            con.execute("ALTER TABLE users DROP COLUMN email_verified_at")
            con.execute("ALTER TABLE users DROP COLUMN email")
            con.execute("PRAGMA user_version=3")

        migrated = Store(self.store.path)
        with migrated.tx() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 5)
            columns = {row[1] for row in con.execute("PRAGMA table_info(match_events)")}
            event = con.execute("SELECT state_after_json,state_after_zlib FROM match_events WHERE match_id=? AND sequence_no=1",
                                (match_id,)).fetchone()
        self.assertIn("state_after_zlib", columns)
        self.assertEqual(event["state_after_json"], serialized)
        self.assertIsNone(event["state_after_zlib"])
        migrated.record_action(match_id, 1, state, action, side, phase, clocks, lease=lease)

    def test_no_candidate_and_invalid_arguments_do_not_create_leases(self):
        self.tournament("LOCKED", players=2)
        self.assertIsNone(self.store.claim_next_match("worker"))
        for owner, capacity in (("", 1), ("worker", 0)):
            with self.assertRaises(ValueError):
                self.store.claim_next_match(owner, capacity)
        with self.store.tx() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM entry_leases").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM match_attempts").fetchone()[0], 0)

    def test_dispatcher_fills_workers_without_loading_tournament_lists(self):
        self.tournament()
        dispatcher = Dispatcher(self.store, self.root, workers=2)
        with (
            patch.object(self.store, "list_tournaments", side_effect=AssertionError("Unexpected tournament scan")),
            patch.object(self.store, "list_matches", side_effect=AssertionError("Unexpected queue scan")),
            patch("ott.tournament.MatchRunner") as runner,
            patch("ott.tournament.threading.Thread", side_effect=lambda **kwargs: MagicMock()),
            patch.object(dispatcher.stop_event, "wait", side_effect=lambda _: dispatcher.stop_event.set()),
        ):
            dispatcher.run()
        self.assertEqual(len(dispatcher.active), 2)
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(all(call.kwargs["lease"].owner == dispatcher.owner for call in runner.call_args_list))

    def test_runner_start_failure_releases_entries_and_active_slot(self):
        self.tournament(players=2)
        dispatcher = Dispatcher(self.store, self.root)
        match, lease = self.store.claim_next_match(dispatcher.owner)
        with patch("ott.tournament.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("Thread start failed")
            with self.assertRaisesRegex(RuntimeError, "Thread start failed"):
                dispatcher._launch(match, lease)
        self.assertEqual(dispatcher.active, {})
        row = self.store.get_match(match["id"])
        self.assertEqual(row["status"], "SCHEDULED")
        self.assertIsNone(row["lease_owner"])
        self.assertEqual(row["result_reason"], "INFRA: RUNNER_START_FAILED")
        with self.store.tx() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM entry_leases").fetchone()[0], 0)

    def test_control_change_after_claim_releases_entries(self):
        for control in ("stop", "maintenance"):
            with self.subTest(control=control):
                self.tournament(players=2)
                dispatcher = Dispatcher(self.store, self.root)
                match, lease = self.store.claim_next_match(dispatcher.owner)
                flag = self.root / ".maintenance"
                if control == "stop":
                    dispatcher.stop_event.set()
                else:
                    flag.touch()
                try:
                    with patch("ott.tournament.MatchRunner") as runner:
                        dispatcher._launch(match, lease)
                        runner.assert_not_called()
                finally:
                    flag.unlink(missing_ok=True)
                row = self.store.get_match(match["id"])
                self.assertEqual(row["status"], "SCHEDULED")
                self.assertIsNone(row["lease_owner"])
                self.assertEqual(dispatcher.active, {})
                with self.store.tx() as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM entry_leases").fetchone()[0], 0)

    def test_dispatcher_completes_tournament_with_real_worker_threads(self):
        tid, matches = self.tournament()
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        lock = threading.Lock()
        counts = {"started": 0, "finished": 0}

        class CompletingRunner:
            def __init__(self, store, data_dir, mid, *, lease, stop_event):
                self.store, self.mid, self.lease = store, mid, lease

            def run(self):
                with lock:
                    counts["started"] += 1
                    if counts["started"] == 2:
                        started.set()
                release.wait(5)
                self.store.finish_match(self.mid, "DRAW", "MOVE_LIMIT", {"white": 300000, "black": 300000}, lease=self.lease)
                with lock:
                    counts["finished"] += 1
                    if counts["finished"] == len(matches):
                        completed.set()

        dispatcher = Dispatcher(self.store, self.root, workers=2)
        with patch("ott.tournament.MatchRunner", CompletingRunner):
            dispatcher.start()
            try:
                self.assertTrue(started.wait(5), "Dispatcher did not fill both worker slots")
                with self.store.tx() as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM matches WHERE status='RUNNING'").fetchone()[0], 2)
                release.set()
                self.assertTrue(completed.wait(10), "Dispatcher did not finish the tournament")
            finally:
                release.set()
                dispatcher.shutdown()
        self.assertEqual(self.store.get_tournament(tid)["status"], "FINISHED")
        self.assertEqual(counts["finished"], len(matches))
        with self.store.tx() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM entry_leases").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
