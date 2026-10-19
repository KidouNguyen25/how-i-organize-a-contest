import unittest

from ott import engine
from ott.tournament import byes, round_robin, standings


def play_state(pieces: dict, side: str = "white") -> dict:
    state = engine.new_state()
    state["phase"] = "play"
    state["side"] = side
    state["board"] = {
        sq: {"side": s, "piece": p, "promotion": pr} for sq, (s, p, pr) in pieces.items()
    }
    return state


def moves(state: dict) -> set[tuple[str, str, str | None]]:
    return {(a["from"], a["to"], a.get("promotion")) for a in engine.legal_actions(state)}


class PlacementTests(unittest.TestCase):
    def test_initial_legal_actions_cover_own_home_rows(self):
        state = engine.new_state()
        actions = engine.legal_actions(state)
        self.assertEqual(len(actions), 4 * 18)
        self.assertTrue(all(a["to"][1] in "12" for a in actions))
        state["side"] = "black"
        self.assertTrue(all(a["to"][1] in "89" for a in engine.legal_actions(state)))

    def test_full_placement_switches_to_play_with_white_first(self):
        state = engine.new_state()
        for _ in range(20):
            action = engine.legal_actions(state)[0]
            self.assertTrue(engine.validate_action(state, action))
            engine.apply_action(state, action)
        self.assertEqual((state["phase"], state["side"]), ("play", "white"))
        self.assertEqual(len(state["board"]), 20)

    def test_observation_hides_opponent_placement(self):
        state = engine.new_state()
        engine.apply_action(state, {"type": "place", "piece": "king", "to": "e1"})
        obs = engine.observation(state, "black", "m", {"white": 1, "black": 1})
        self.assertEqual(obs["board"], {})
        self.assertEqual(obs["history"], [])
        self.assertIsNone(engine.public_board(state))


class MoveTests(unittest.TestCase):
    def test_capture_relations(self):
        for attacker, victim, ok in (("scissors", "paper", True), ("paper", "hammer", True), ("hammer", "scissors", True),
                                     ("scissors", "hammer", False), ("scissors", "scissors", False)):
            state = play_state({"e4": ("white", attacker, None), "e5": ("black", victim, None), "a9": ("black", "king", None)})
            self.assertEqual(("e4", "e5", None) in moves(state), ok, (attacker, victim))

    def test_any_capture_takes_king(self):
        state = play_state({"e4": ("white", "hammer", None), "e5": ("black", "king", None)})
        self.assertIn(("e4", "e5", None), moves(state))
        outcome = engine.apply_action(state, {"type": "move", "from": "e4", "to": "e5"})
        self.assertEqual(outcome, {"result": "WHITE_WIN", "reason": "KING_CAPTURED"})

    def test_backward_diagonal_never_captures(self):
        state = play_state({"e4": ("white", "scissors", None), "d3": ("black", "paper", None), "a9": ("black", "king", None)})
        self.assertNotIn(("e4", "d3", None), moves(state))
        self.assertNotIn(("e4", "e3", None), moves(state))  # no straight backward move
        state["board"].pop("d3")
        self.assertIn(("e4", "d3", None), moves(state))

    def test_king_moves_but_never_captures(self):
        state = play_state({"e4": ("white", "king", None), "e5": ("black", "paper", None), "d4": ("white", "hammer", None)})
        got = moves(state)
        self.assertNotIn(("e4", "e5", None), got)
        self.assertNotIn(("e4", "d4", None), got)
        self.assertIn(("e4", "f3", None), got)

    def test_promotion_is_required_on_last_rank(self):
        state = play_state({"e8": ("white", "paper", None), "a1": ("black", "king", None)})
        got = moves(state)
        self.assertEqual({m for m in got if m[1] == "e9"}, {("e8", "e9", "rook"), ("e8", "e9", "bishop")})
        self.assertFalse(engine.validate_action(state, {"type": "move", "from": "e8", "to": "e9"}))
        engine.apply_action(state, {"type": "move", "from": "e8", "to": "e9", "promotion": "rook"})
        self.assertEqual(state["board"]["e9"]["promotion"], "rook")

    def test_rook_slides_and_is_blocked(self):
        state = play_state({"a1": ("white", "scissors", "rook"), "a4": ("white", "hammer", None), "d1": ("black", "paper", None), "i9": ("black", "king", None)})
        got = {m[1] for m in moves(state) if m[0] == "a1"}
        self.assertIn("a3", got)
        self.assertNotIn("a4", got)  # own piece blocks
        self.assertNotIn("a5", got)
        self.assertIn("d1", got)  # scissors capture paper
        self.assertNotIn("e1", got)  # cannot jump past the captured piece
        self.assertIn("b2", got)  # one-step diagonal

    def test_promoted_piece_captures_same_type(self):
        state = play_state({"a1": ("white", "paper", "rook"), "a5": ("black", "paper", None), "i9": ("black", "king", None)})
        self.assertIn(("a1", "a5", None), moves(state))

    def test_no_legal_moves_loses(self):
        state = play_state({"a1": ("white", "king", None), "a2": ("black", "paper", None), "b1": ("black", "paper", None),
                            "b2": ("black", "paper", None), "e9": ("black", "king", None)}, side="black")
        # black moves; after that white has only blocked king moves? white king at a1 has no empty neighbour once black fills them
        outcome = engine.apply_action(state, {"type": "move", "from": "e9", "to": "e8"})
        self.assertEqual(outcome, {"result": "BLACK_WIN", "reason": "NO_LEGAL_MOVES"})

    def test_threefold_repetition_is_draw(self):
        state = play_state({"a1": ("white", "king", None), "i9": ("black", "king", None)})
        state["positions"][engine.position_key(state)] = 1
        cycle = [("a1", "a2"), ("i9", "i8"), ("a2", "a1"), ("i8", "i9")]
        outcome = None
        for _ in range(3):
            for frm, to in cycle:
                outcome = engine.apply_action(state, {"type": "move", "from": frm, "to": to})
        self.assertEqual(outcome, {"result": "DRAW", "reason": "REPETITION"})

    def test_parse_action_rejects_bad_input(self):
        for bad in ('{"type":"place","piece":"king","to":"a1","x":1}', '{"type":"move","from":"a1","to":"a2","to":"a3"}',
                    '{"type":"move","from":"z1","to":"a2"}', '[]', 'nope', '{"type":"move","from":"a1","to":"a2","promotion":"queen"}'):
            with self.assertRaises(ValueError, msg=bad):
                engine.parse_action(bad)
        self.assertEqual(engine.parse_action('{"type":"move","from":"a1","to":"a2"}')["to"], "a2")


class TournamentTests(unittest.TestCase):
    def test_double_round_robin_even(self):
        rows = round_robin(["a", "b", "c", "d"])
        self.assertEqual(len(rows), 12)
        pairs = {}
        for r in rows:
            pairs.setdefault(frozenset((r["white"], r["black"])), []).append((r["leg_no"], r["white"]))
        self.assertEqual(len(pairs), 6)
        for games in pairs.values():
            self.assertEqual(len(games), 2)
            self.assertNotEqual(games[0][1], games[1][1])  # colours swap in leg 2
        # earlier registered player is White in leg 1
        self.assertTrue(all(r["white"] < r["black"] for r in rows if r["leg_no"] == 1))

    def test_odd_roster_has_one_bye_per_round(self):
        ids = ["a", "b", "c", "d", "e"]
        rows = round_robin(ids)
        self.assertEqual(len(rows), 20)
        matches = [{"leg_no": r["leg_no"], "round_no": r["round_no"], "white_entry_id": r["white"], "black_entry_id": r["black"]} for r in rows]
        self.assertEqual(len(byes(ids, matches)), 10)

    def test_standings_head_to_head_tiebreak(self):
        entries = [{"id": i, "username": i} for i in "abc"]

        def m(w, b, res, n):
            return {"status": "FINISHED", "result": res, "white_entry_id": w, "black_entry_id": b, "leg_no": 1, "round_no": n, "pairing_no": 1}

        matches = [m("a", "b", "BLACK_WIN", 1), m("b", "c", "BLACK_WIN", 2), m("c", "a", "BLACK_WIN", 3),
                   m("a", "b", "WHITE_WIN", 4), m("b", "c", "WHITE_WIN", 5), m("c", "a", "WHITE_WIN", 6)]
        rows = standings(entries, matches)
        self.assertEqual([r["points"] for r in rows], [6, 6, 6])
        self.assertEqual({r["rank"] for r in rows}, {1})  # perfect cycle: shared rank


if __name__ == "__main__":
    unittest.main()
