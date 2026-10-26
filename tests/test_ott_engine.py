import copy
import json
import random
import unittest
from pathlib import Path

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


class PositionKeyTests(unittest.TestCase):
    def test_compact_key_is_order_independent_exact_and_fixed_width(self):
        pieces = {"a1": ("white", "scissors", None), "i9": ("black", "king", None)}
        first = play_state(pieces)
        reversed_board = play_state(dict(reversed(list(pieces.items()))))
        self.assertEqual(engine.position_key(first), engine.position_key(reversed_board))
        self.assertEqual(len(engine.position_key(first)), 82)

        variants = [
            play_state({"a1": (side, piece, promotion)})
            for side in ("white", "black")
            for piece in engine.PIECES
            for promotion in ((None,) if piece == "king" else (None, "rook", "bishop"))
        ]
        variants.extend((
            play_state({"b1": ("white", "scissors", None)}),
            play_state({"a1": ("white", "scissors", None)}),
        ))
        variants[-1]["side"] = "black"
        keys = {engine.position_key(state) for state in variants}
        self.assertEqual(len(keys), len(variants))
        self.assertTrue(all(len(key) == 82 for key in keys))


class PlacementBehaviorTests(unittest.TestCase):
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

    def test_blocked_first_piece_does_not_hide_other_legal_moves(self):
        state = play_state({"a1": ("white", "king", None), "e5": ("white", "paper", None),
                            "a2": ("black", "paper", None), "b1": ("black", "paper", None),
                            "b2": ("black", "paper", None), "e9": ("black", "king", None)}, side="black")
        outcome = engine.apply_action(state, {"type": "move", "from": "e9", "to": "e8"})
        self.assertIsNone(outcome)
        self.assertEqual(state["side"], "white")
        self.assertTrue(engine.legal_actions(state))

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


class ValidationTests(unittest.TestCase):
    def test_placement_validation_matches_all_generated_actions(self):
        rng = random.Random(71)
        squares = [file + rank for file in engine.FILES for rank in "123456789"]
        for placed in (0, 1, 7, 19):
            state = engine.new_state()
            for _ in range(placed):
                engine.apply_action(state, rng.choice(engine.legal_actions(state)))
            for side in ("white", "black"):
                state["side"] = side
                before = copy.deepcopy(state)
                legal = {(action["piece"], action["to"]) for action in engine.legal_actions(state)}
                for piece in engine.PIECES:
                    for dest in squares:
                        action = {"type": "place", "piece": piece, "to": dest}
                        self.assertEqual(engine.validate_action(state, action), (piece, dest) in legal,
                                         (placed, side, action))
                self.assertEqual(state, before)

    def test_move_validation_matches_generator_for_every_square_pair_and_promotion(self):
        states = [
            play_state({"e4": ("white", "king", None), "e5": ("black", "paper", None), "i9": ("black", "king", None)}),
            play_state({"a1": ("white", "scissors", "rook"), "a4": ("white", "hammer", None),
                        "d1": ("black", "paper", None), "i9": ("black", "king", None)}),
            play_state({"e5": ("white", "paper", "bishop"), "g7": ("white", "hammer", None),
                        "b2": ("black", "scissors", "rook"), "i9": ("black", "king", None)}),
            play_state({"e8": ("white", "paper", None), "d2": ("black", "hammer", None),
                        "a1": ("white", "king", None), "i9": ("black", "king", None)}),
        ]
        for seed in (17, 71):
            rng = random.Random(seed)
            state = engine.new_state()
            for _ in range(20):
                engine.apply_action(state, rng.choice(engine.legal_actions(state)))
            states.append(state)
        squares = [file + rank for file in engine.FILES for rank in "123456789"]
        for index, state in enumerate(states):
            for side in ("white", "black"):
                state["side"] = side
                before = copy.deepcopy(state)
                legal = moves(state)
                for origin in squares:
                    for dest in squares:
                        for promotion in (None, "rook", "bishop"):
                            action = {"type": "move", "from": origin, "to": dest}
                            if promotion is not None:
                                action["promotion"] = promotion
                            self.assertEqual(engine.validate_action(state, action), (origin, dest, promotion) in legal,
                                             (index, side, action))
                self.assertEqual(state, before)

    def test_validation_rejects_extra_fields_and_malformed_values(self):
        states = [engine.new_state(), play_state({"e8": ("white", "paper", None), "a1": ("white", "king", None),
                                                 "i9": ("black", "king", None)})]
        malformed = [None, [], "move", {}, {"type": "move", "from": [], "to": "a2"},
                     {"type": "move", "from": "a1", "to": {}}, {"type": "place", "piece": [], "to": "a1"},
                     {"type": "place", "piece": "king", "to": []}, {"type": "place", "piece": "king", "to": "a10"}]
        for state in states:
            for action in malformed:
                self.assertFalse(engine.validate_action(state, action), action)
            for action in engine.legal_actions(state):
                self.assertFalse(engine.validate_action(state, action | {"extra": True}))
                self.assertFalse(engine.validate_action(state, action | {"promotion": None}))
                self.assertFalse(engine.validate_action(state, action | {"promotion": "queen"}))

    def test_seeded_games_validate_without_changing_state_or_action(self):
        for seed in range(8):
            rng = random.Random(seed)
            state = engine.new_state()
            for _ in range(120):
                legal = engine.legal_actions(state)
                if not legal:
                    break
                action = rng.choice(legal)
                before, original = copy.deepcopy(state), dict(action)
                self.assertTrue(engine.validate_action(state, action), (seed, action))
                self.assertEqual(state, before)
                self.assertEqual(action, original)
                if engine.apply_action(state, action):
                    break


class ObservationTests(unittest.TestCase):
    @staticmethod
    def states():
        rng = random.Random(71)
        state = engine.new_state()
        for _ in range(10):
            engine.apply_action(state, rng.choice(engine.legal_actions(state)))
        placement = copy.deepcopy(state)
        for _ in range(10):
            engine.apply_action(state, rng.choice(engine.legal_actions(state)))
        playing = copy.deepcopy(state)
        for index in range(480):
            state["history"].append({"phase": "play", "side": "white" if index % 2 == 0 else "black",
                                     "action": {"type": "move", "from": "a1", "to": "a2"}})
        return placement, playing, state

    def test_observation_projects_protocol_for_both_phases_and_sides(self):
        clocks = {"white": 123456, "black": 234567}
        for state in self.states():
            for side in ("white", "black"):
                view = engine.observation(state, side, "observed", clocks)
                revealed = state["phase"] == "play"
                board = state["board"] if revealed else {square: piece for square, piece in state["board"].items() if piece["side"] == side}
                history = state["history"] if revealed else [entry for entry in state["history"] if entry["side"] == side]
                expected = {
                    "protocol_version": 1, "match_id": "observed", "phase": state["phase"], "side": side,
                    "board": board, "remaining_pieces": {piece: 0 for piece in engine.PIECES} if revealed else state["remaining"][side],
                    "clocks_ms": clocks, "legal_actions": engine.legal_actions(state, side), "history": history,
                }
                self.assertEqual(view, expected)
                self.assertEqual(json.dumps(view, separators=(",", ":")), json.dumps(expected, separators=(",", ":")))

    def test_observations_isolate_all_mutable_fields_in_both_directions(self):
        for template in self.states():
            for side in ("white", "black"):
                state = copy.deepcopy(template)
                before = copy.deepcopy(state)
                clocks = {"white": 123456, "black": 234567}
                view = engine.observation(state, side, "observed", clocks)
                other_view = engine.observation(state, side, "observed", clocks)
                expected = copy.deepcopy(other_view)
                square = next(iter(view["board"]))
                view["board"][square]["piece"] = "changed"
                view["board"].pop(square)
                view["history"][0]["action"]["to"] = "changed"
                view["history"][0]["side"] = "changed"
                view["history"].append({})
                view["remaining_pieces"]["king"] = 99
                view["clocks_ms"]["white"] = 0
                view["legal_actions"][0]["to"] = "changed"
                view["legal_actions"].clear()
                self.assertEqual(state, before)
                self.assertEqual(clocks, {"white": 123456, "black": 234567})
                self.assertEqual(other_view, expected)

                state["board"][square]["piece"] = "changed"
                entry = next(entry for entry in state["history"] if state["phase"] == "play" or entry["side"] == side)
                entry["action"]["to"] = "changed"
                state["history"].append({})
                state["remaining"][side]["king"] = 99
                clocks["white"] = 0
                self.assertEqual(other_view, expected)

    def test_public_board_stays_hidden_and_isolated(self):
        placement, playing, _ = self.states()
        self.assertIsNone(engine.public_board(placement))
        before = copy.deepcopy(playing)
        board = engine.public_board(playing)
        other_board = engine.public_board(playing)
        square = next(iter(board))
        board[square]["promotion"] = "changed"
        board.clear()
        self.assertEqual(playing, before)
        playing["board"][square]["piece"] = "changed"
        self.assertEqual(other_board, before["board"])

    def test_protocol_schema_keeps_piece_and_action_fields_scalar(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs" / "bot-protocol.schema.json").read_text(encoding="utf-8"))
        definitions = schema["$defs"]
        for name in ("boardPiece", "placeAction", "moveAction", "historyEntry"):
            definition = definitions[name]
            self.assertFalse(definition["additionalProperties"])
            for field, prop in definition["properties"].items():
                if name == "historyEntry" and field == "action":
                    self.assertEqual(prop, {"$ref": "#/$defs/action"})
                    continue
                if "$ref" in prop:
                    prop = definitions[prop["$ref"].rsplit("/", 1)[1]]
                if "type" in prop:
                    self.assertIn(prop["type"], ("string", "number", "integer", "boolean", "null"))
                elif "enum" in prop:
                    self.assertTrue(all(value is None or isinstance(value, (str, int, float, bool)) for value in prop["enum"]))
                else:
                    self.assertIn("const", prop)
                    self.assertTrue(prop["const"] is None or isinstance(prop["const"], (str, int, float, bool)))


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

    def test_head_to_head_counts_only_tied_opponents(self):
        entries = [{"id": name, "username": name} for name in "abcd"]
        matches = [
            {"status": "FINISHED", "result": "WHITE_WIN", "white_entry_id": white, "black_entry_id": black,
             "leg_no": 1, "round_no": index, "pairing_no": 1}
            for index, (white, black) in enumerate((("a", "b"), ("b", "c"), ("c", "a"), ("a", "d"), ("b", "d")))
        ]
        rows = standings(entries, list(reversed(matches)))
        self.assertEqual([row["entry_id"] for row in rows], list("abcd"))
        self.assertEqual([row["points"] for row in rows], [6, 6, 3, 0])
        self.assertEqual([row["h2h"] for row in rows], [3, 0, 0, 0])
        self.assertEqual([row["rank"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]["sheet"], ["w", "l", "w"])

    def test_unplayed_entries_share_rank_without_head_to_head_points(self):
        entries = [{"id": name, "username": name} for name in "cba"]
        rows = standings(entries, [])
        self.assertEqual([row["entry_id"] for row in rows], list("abc"))
        self.assertTrue(all(row["rank"] == 1 and row["h2h"] == 0 and row["played"] == 0 for row in rows))


if __name__ == "__main__":
    unittest.main()
