import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ott import engine
from ott.api import ApiError, TEMPLATE_PATH, make_app
from ott.events import HUB
from ott.store import Store
from ott.tournament import round_robin, standings


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "test.db")
        with self.store.tx() as con:
            con.executemany(
                "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,'unused','CONTESTANT',0)",
                [(n, f"player{n}") for n in range(1, 4)],
            )
        self.tournament = self.store.create_tournament("Read Cup", 300, 1)
        self.entries = [self.store.create_entry(self.tournament["id"], n) for n in range(1, 4)]
        self.store.create_matches(self.tournament["id"], round_robin([e["id"] for e in self.entries]), 300000)
        placement = engine.new_state()
        engine.apply_action(placement, engine.legal_actions(placement)[0])
        playing = engine.new_state()
        for _ in range(20):
            engine.apply_action(playing, engine.legal_actions(playing)[0])
        engine.apply_action(playing, engine.legal_actions(playing)[0])
        self.states = {}
        with self.store.tx() as con:
            for index, match in enumerate(self.store.list_matches(self.tournament["id"])):
                state = (None, placement, playing)[index % 3]
                self.states[match["id"]] = state
                con.execute(
                    "UPDATE matches SET status='RUNNING',state_json=?,updated_at=? WHERE id=?",
                    (json.dumps(state) if state else None, index, match["id"]),
                )
        self.app = make_app(self.store, root, Path(__file__).resolve().parents[1] / "mockup")

    def route(self, path, query=None, **kwargs):
        handler = next(handler for method, regex, handler, _ in self.app.routes
                       if method == "GET" and regex.fullmatch(path))
        return handler(SimpleNamespace(user=None, query=query or {}), **kwargs)

    def test_live_uses_one_query_and_preserves_public_projection(self):
        selects = []
        original_tx = self.store.tx

        @contextmanager
        def traced_tx():
            with original_tx() as con:
                con.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)
                yield con

        with patch.object(self.store, "tx", traced_tx):
            status, payload = self.route("/live")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 6)
        self.assertEqual(len(selects), 1)
        for item in payload["items"]:
            state = self.states[item["id"]]
            self.assertNotIn("moves", item)
            self.assertNotIn("game_id", item)
            self.assertNotIn("state_json", item)
            if not state or state["phase"] == "placement":
                self.assertFalse(item["revealed"])
                self.assertIsNone(item["board"])
                self.assertIsNone(item["placements"])
                self.assertIsNone(item["last_move"])
            else:
                self.assertTrue(item["revealed"])
                self.assertEqual(item["board"], state["board"])
                self.assertEqual(item["last_move"], {k: state["moves_log"][-1][k] for k in ("from", "to")})

    def test_match_detail_keeps_full_move_history(self):
        mid, state = next((mid, state) for mid, state in self.states.items() if state and state["phase"] == "play")
        status, payload = self.route(f"/matches/{mid}", mid=mid)
        self.assertEqual(status, 200)
        self.assertEqual(payload["moves"], state["moves_log"])
        self.assertNotIn("last_move", payload)
        self.assertFalse(payload["viewer_is_player"])

    def test_match_poll_returns_compact_live_delta_for_same_state(self):
        mid = next(iter(self.states))
        match = self.store.get_match(mid)
        full_status, full = self.route(f"/matches/{mid}", mid=mid)
        selects = []
        original_tx = self.store.tx

        @contextmanager
        def traced_tx():
            with original_tx() as con:
                con.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)
                yield con

        with patch.object(self.store, "tx", traced_tx), patch.object(
                self.store, "get_match", side_effect=AssertionError("poll must skip the full match row")):
            status, delta = self.route(f"/matches/{mid}", query={"since": [str(match["state_version"])]}, mid=mid)
        self.assertEqual((full_status, status), (200, 200))
        self.assertTrue(delta["not_modified"])
        self.assertEqual(delta["state_version"], full["state_version"])
        self.assertEqual(delta["status"], full["status"])
        self.assertEqual(delta["clocks_ms"], full["clocks_ms"])
        self.assertNotIn("moves", delta)
        self.assertLess(len(json.dumps(delta)), len(json.dumps(full)))
        self.assertEqual(len(selects), 1)
        self.assertNotIn("state_json", selects[0])
        self.assertNotIn("execution_snapshot", selects[0])
        stale_status, stale = self.route(f"/matches/{mid}", query={"since": ["0"]}, mid=mid)
        self.assertEqual(stale_status, 200)
        self.assertNotIn("not_modified", stale)
        self.assertIn("moves", stale)

    def test_live_respects_tournament_adapter_version(self):
        with self.store.tx() as con:
            con.execute("UPDATE tournaments SET game_version='unsupported'")
        with self.assertRaisesRegex(ValueError, "Adapter version is not approved"):
            self.route("/live")

    def test_user_history_preserves_both_colours_order_and_limit(self):
        with self.store.tx() as con:
            con.execute("UPDATE entries SET status='REMOVED' WHERE user_id=1")
            expected = [dict(row) for row in con.execute(
                self.store._MATCH_SQL + "WHERE we.user_id=? OR be.user_id=? ORDER BY m.updated_at DESC LIMIT ?",
                (1, 1, 30),
            )]
        actual = self.store.user_matches(1)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 4)
        self.assertEqual(len({m["id"] for m in actual}), 4)
        self.assertEqual(self.store.user_matches(1, limit=2), expected[:2])
        self.assertEqual(self.store.user_matches(99), [])
        self.assertEqual(self.store.user_entries(1), [])

    def test_existing_database_gains_indexes_without_losing_matches(self):
        names = (
            "entries_user_status_idx", "matches_white_entry_updated_idx", "matches_black_entry_updated_idx",
            "matches_white_entry_result_idx", "matches_black_entry_result_idx",
        )
        with self.store.tx() as con:
            for name in names:
                con.execute(f"DROP INDEX {name}")
        reopened = Store(self.store.path)
        with reopened.tx() as con:
            indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue(set(names) <= indexes)
        self.assertEqual(len(reopened.list_matches()), 6)

    def test_match_summaries_preserve_public_fields_without_snapshots(self):
        tid = self.tournament["id"]
        full = self.store.list_matches(tid)
        summaries = self.store.list_match_summaries(tid)
        self.assertEqual([self.app.summary(row) for row in summaries], [self.app.summary(row) for row in full])
        self.assertTrue(any(row["state_json"] for row in full))
        for row in summaries:
            self.assertNotIn("state_json", row)
            self.assertNotIn("execution_snapshot", row)
        self.assertEqual(self.store.list_match_summaries("missing"), [])

    def test_large_tournament_schedule_standings_and_profile_are_complete(self):
        tournament = self.store.create_tournament("Large Cup", 300, 1)
        tid = tournament["id"]
        ids = [f"large-entry-{n}" for n in range(1, 26)]
        with self.store.tx() as con:
            con.executemany(
                "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,'unused','CONTESTANT',0)",
                [(n, f"player{n}") for n in range(4, 26)],
            )
            con.executemany(
                "INSERT INTO entries(id,tournament_id,user_id,status,registered_at) VALUES (?,?,?,'READY',0)",
                [(eid, tid, n) for n, eid in enumerate(ids, start=1)],
            )
        self.store.create_matches(tid, round_robin(ids), 300000)
        with self.store.tx() as con:
            con.execute("UPDATE matches SET status='FINISHED',result='DRAW' WHERE tournament_id=?", (tid,))

        status, schedule = self.route(f"/tournaments/{tid}/schedule", tid=tid)
        self.assertEqual(status, 200)
        self.assertEqual(len(schedule["matches"]), 600)
        self.assertEqual(len({row["id"] for row in schedule["matches"]}), 600)
        self.assertEqual(len(schedule["byes"]), 50)

        status, table = self.route(f"/tournaments/{tid}/standings", tid=tid)
        self.assertEqual(status, 200)
        self.assertEqual(len(table["items"]), 25)
        for row in table["items"]:
            self.assertEqual((row["played"], row["points"], row["rank"]), (48, 48, 1))
        status, profile = self.route("/users/player1", name="player1")
        self.assertEqual(status, 200)
        entry = next(row for row in profile["entries"] if row["tournament_id"] == tid)
        self.assertEqual((entry["points"], entry["rank"]), (48, 1))
        self.assertEqual(profile["totals"], {"wins": 0, "draws": 48, "losses": 0, "points": 48})
        self.assertEqual(len(profile["games"]), 30)

    def test_recent_history_projection_preserves_order_without_snapshots(self):
        full = self.store.user_matches(1, limit=3)
        summaries = self.store.user_matches(1, limit=3, include_state=False)
        self.assertEqual([row["id"] for row in summaries], [row["id"] for row in full])
        for summary, match in zip(summaries, full):
            self.assertEqual(summary, {key: match[key] for key in summary})
            self.assertNotIn("state_json", summary)
            self.assertNotIn("execution_snapshot", summary)

    def test_result_projection_keeps_standings_without_boards_or_player_joins(self):
        tid = self.tournament["id"]
        with self.store.tx() as con:
            con.execute("UPDATE matches SET status='FINISHED',result='DRAW' WHERE tournament_id=? AND leg_no=1", (tid,))
        full = self.store.list_matches(tid)
        results = self.store.list_match_results(tid)
        self.assertEqual(len(results), len(full))
        for result, match in zip(results, full):
            self.assertEqual(result, {key: match[key] for key in result})
            self.assertNotIn("state_json", result)
            self.assertNotIn("white_name", result)
            self.assertNotIn("execution_snapshot", result)
        entries = self.store.list_entries(tid)
        expected = standings(entries, full)
        self.assertEqual(standings(entries, results), expected)
        _, payload = self.route(f"/tournaments/{tid}/standings", tid=tid)
        self.assertEqual(payload["items"], expected)
        self.assertEqual(self.store.list_match_results("missing"), [])

    def test_lifetime_totals_include_both_colours_and_removed_entries(self):
        matches = list(reversed(self.store.user_matches(1)))
        wins, losses = [], []
        with self.store.tx() as con:
            for index, match in enumerate(matches):
                white = match["white_name"] == "player1"
                outcome = ("win", "loss", "draw", "win")[index]
                result = "DRAW" if outcome == "draw" else "WHITE_WIN" if (outcome == "win") == white else "BLACK_WIN"
                con.execute("UPDATE matches SET status='FINISHED',result=? WHERE id=?", (result, match["id"]))
                if outcome == "win":
                    wins.append(match["id"])
                elif outcome == "loss":
                    losses.append(match["id"])
            con.execute("UPDATE entries SET status='REMOVED' WHERE user_id=1")
        self.assertEqual(self.store.user_match_totals(1), {"wins": 2, "draws": 1, "losses": 1, "points": 7})
        self.assertEqual(self.store.user_entries(1), [])

        with self.store.tx() as con:
            con.execute("UPDATE matches SET status='RUNNING' WHERE id=?", (wins[0],))
            con.execute("UPDATE matches SET status='PAUSED' WHERE id=?", (losses[0],))
        expected = {"wins": 1, "draws": 1, "losses": 0, "points": 4}
        self.assertEqual(self.store.user_match_totals(1), expected)
        _, profile = self.route("/users/player1", name="player1")
        self.assertEqual(profile["totals"], expected)
        self.assertEqual(profile["entries"], [])

    def test_empty_profile_has_integer_zero_totals(self):
        with self.store.tx() as con:
            con.execute("INSERT INTO users(id,username,password_hash,role,created_at) VALUES (4,'empty_player','unused','CONTESTANT',0)")
        _, profile = self.route("/users/empty_player", name="empty_player")
        self.assertEqual(profile["totals"], {"wins": 0, "draws": 0, "losses": 0, "points": 0})
        self.assertTrue(all(type(value) is int for value in profile["totals"].values()))
        self.assertEqual(profile["games"], [])
        self.assertEqual(profile["entries"], [])
        self.assertEqual(self.store.user_match_totals(99), profile["totals"])

    def test_many_tournament_profile_uses_fixed_queries_and_preserves_rankings(self):
        for index in range(4):
            tournament = self.store.create_tournament(f"Profile Cup {index}", 300, 1)
            tid = tournament["id"]
            entries = [self.store.create_entry(tid, n) for n in range(1, 4)]
            if index < 3:
                self.store.create_matches(tid, round_robin([e["id"] for e in entries]), 300000)
            with self.store.tx() as con:
                con.execute("UPDATE matches SET status='FINISHED',result='DRAW' WHERE tournament_id=?", (tid,))
                if index == 0:
                    con.execute(
                        "INSERT INTO bot_submissions(id,tournament_id,user_id,language,source_sha256,source_size,compile_status,created_at) "
                        "VALUES ('profile-bot',?,1,'cpp17',?,0,'READY',0)", (tid, "a" * 64),
                    )
                    con.execute("UPDATE entries SET bot_submission_id='profile-bot',status='READY' WHERE id=?", (entries[0]["id"],))
                if index == 1:
                    con.execute("UPDATE entries SET status='REMOVED' WHERE id=?", (entries[2]["id"],))
                if index == 2:
                    con.execute("UPDATE entries SET status='REMOVED' WHERE id=?", (entries[0]["id"],))
            self.store.update_tournament(tid, status=("FINISHED", "CANCELLED", "LOCKED", "DRAFT")[index])

        expected = []
        for entry in self.store.user_entries(1):
            tournament = self.store.get_tournament(entry["tournament_id"])
            rows = standings(self.store.list_entries(tournament["id"]), self.store.list_match_results(tournament["id"]))
            mine = next(row for row in rows if row["entry_id"] == entry["id"])
            expected.append({"tournament_id": tournament["id"], "tournament_name": tournament["name"], "status": tournament["status"],
                             "rank": mine["rank"], "points": mine["points"], "compile_status": entry["compile_status"]})
        selects = []
        original_tx = self.store.tx

        @contextmanager
        def traced_tx():
            with original_tx() as con:
                con.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)
                yield con

        with patch.object(self.store, "tx", traced_tx):
            _, payload = self.route("/users/player1", name="player1")
        self.assertEqual(payload["entries"], expected)
        self.assertEqual(len(expected), 4)
        self.assertEqual(len(selects), 6)

        with self.store.tx() as con:
            con.execute("UPDATE entries SET status='REMOVED' WHERE user_id=1")
        selects.clear()
        with patch.object(self.store, "tx", traced_tx):
            _, payload = self.route("/users/player1", name="player1")
        self.assertEqual(payload["entries"], [])
        self.assertEqual(len(selects), 4)

    def test_batched_standings_keep_one_snapshot_during_concurrent_updates(self):
        tournament = self.store.create_tournament("Snapshot Cup", 300, 1)
        entries = [self.store.create_entry(tournament["id"], n) for n in range(1, 4)]
        self.store.create_matches(tournament["id"], round_robin([e["id"] for e in entries]), 300000)
        with closing(self.store.iter_user_standings_inputs(1)) as inputs:
            first = next(inputs)
            target_tid = next(tid for tid in (tournament["id"], self.tournament["id"]) if tid != first["tournament_id"])
            original = self.store.get_tournament(target_tid)
            original_results = self.store.list_match_results(target_tid)
            with self.store.tx() as con:
                con.execute("UPDATE tournaments SET status='CANCELLED' WHERE id=?", (target_tid,))
                con.execute("UPDATE matches SET status='FINISHED',result='WHITE_WIN' WHERE tournament_id=?", (target_tid,))
                con.execute("UPDATE entries SET status='REMOVED' WHERE tournament_id=? AND user_id=1", (target_tid,))
            second = next(inputs)
            self.assertEqual(second["tournament_id"], target_tid)
            self.assertEqual(second["status"], original["status"])
            self.assertEqual(second["matches"], original_results)
            self.assertIn(second["entry_id"], {entry["id"] for entry in second["entries"]})
        self.assertEqual(self.store.get_tournament(target_tid)["status"], "CANCELLED")
        fresh = list(self.store.iter_user_standings_inputs(1))
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["tournament_id"], first["tournament_id"])

    def test_profile_failure_closes_batched_read_connection(self):
        with self.store.tx() as con:
            con.execute("UPDATE matches SET status='FINISHED',result='INVALID'")
        connections = []
        original_tx = self.store.tx

        @contextmanager
        def tracked_tx():
            with original_tx() as con:
                connections.append(con)
                yield con

        with patch.object(self.store, "tx", tracked_tx):
            with self.assertRaises(KeyError):
                self.route("/users/player1", name="player1")
        self.assertTrue(connections)
        for con in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def test_site_stats_empty_database_and_accounts_without_tournaments(self):
        empty = Store(Path(self.tmp.name) / "empty.db")
        expected = {"users": 0, "tournaments": 0, "running_tournaments": 0, "running_matches": 0}
        self.assertEqual(empty.site_stats(), expected)
        self.assertTrue(all(type(value) is int for value in empty.site_stats().values()))
        with empty.tx() as con:
            con.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES ('only_user','unused','CONTESTANT',0)")
        self.assertEqual(empty.site_stats(), expected | {"users": 1})

    def test_public_stats_preserve_paused_counts_with_one_query(self):
        running = self.store.create_tournament("Running Cup", 300, 1)
        finished = self.store.create_tournament("Finished Cup", 300, 1)
        self.store.update_tournament(running["id"], status="RUNNING")
        self.store.update_tournament(finished["id"], status="FINISHED")
        self.store.update_tournament(self.tournament["id"], status="CANCELLED")
        with self.store.tx() as con:
            for match, status in zip(self.store.list_matches(), ("RUNNING", "PAUSED", "FINISHED", "SCHEDULED", "RUNNING", "PAUSED")):
                con.execute("UPDATE matches SET status=? WHERE id=?", (status, match["id"]))
        tournaments = self.store.list_tournaments()
        expected = {"users": self.store.user_count(), "tournaments": len(tournaments),
                    "running_tournaments": sum(t["status"] == "RUNNING" for t in tournaments),
                    "running_matches": sum(t["matches_running"] for t in tournaments), "stream_clients": HUB.clients}
        self.assertEqual(expected["running_matches"], 4)
        selects = []
        original_tx = self.store.tx

        @contextmanager
        def traced_tx():
            with original_tx() as con:
                con.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)
                yield con

        with (
            patch.object(self.store, "tx", traced_tx),
            patch.object(self.store, "list_tournaments", side_effect=AssertionError("Unexpected tournament details")),
            patch.object(self.store, "user_count", side_effect=AssertionError("Unexpected separate user count")),
        ):
            status, payload = self.route("/stats")
        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        self.assertEqual(len(selects), 1)

    def test_public_stats_refresh_counters_and_stream_subscribers(self):
        clients = HUB.clients
        _, before = self.route("/stats")
        self.assertEqual(before, {"users": 3, "tournaments": 1, "running_tournaments": 0,
                                  "running_matches": 6, "stream_clients": clients})
        subscriber = HUB.subscribe()
        self.assertIsNotNone(subscriber)
        try:
            with self.store.tx() as con:
                con.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES ('new_user','unused','CONTESTANT',0)")
                con.execute("UPDATE matches SET status='FINISHED'")
            self.store.update_tournament(self.tournament["id"], status="RUNNING")
            _, after = self.route("/stats")
            self.assertEqual(after, {"users": 4, "tournaments": 1, "running_tournaments": 1,
                                    "running_matches": 0, "stream_clients": clients + 1})
        finally:
            HUB.unsubscribe(subscriber)
        _, after_disconnect = self.route("/stats")
        self.assertEqual(after_disconnect["stream_clients"], clients)

    def test_bot_template_response_is_plain_text(self):
        status, (content_type, source) = self.route("/bot-template")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        self.assertEqual(source, TEMPLATE_PATH.read_text(encoding="utf-8"))
        self.assertTrue(source.strip())

    def test_plain_text_bot_source_stays_owner_only(self):
        bid = "00000000-0000-0000-0000-000000000001"
        source = "// Mã nguồn riêng\nint main() { return 0; }\n"
        with self.store.tx() as con:
            con.execute(
                "INSERT INTO bot_submissions(id,tournament_id,user_id,language,source,source_sha256,source_size,compile_status,created_at) "
                "VALUES (?,?,1,'cpp17',?,?,?,'READY',0)",
                (bid, self.tournament["id"], source, "a" * 64, len(source.encode("utf-8"))),
            )
        handler = next(handler for method, regex, handler, _ in self.app.routes
                       if method == "GET" and regex.fullmatch(f"/bots/{bid}/source"))
        for user, expected_status in ((None, 401), ({"id": 2}, 403)):
            with self.subTest(user=user):
                with self.assertRaises(ApiError) as error:
                    handler(SimpleNamespace(user=user), bid)
                self.assertEqual(error.exception.status, expected_status)
        status, body = handler(SimpleNamespace(user={"id": 1}), bid)
        self.assertEqual(status, 200)
        self.assertEqual(body, ("text/plain; charset=utf-8", source))


if __name__ == "__main__":
    unittest.main()
