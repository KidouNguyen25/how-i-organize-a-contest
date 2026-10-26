import json
import unittest
from unittest.mock import patch

from ott.api import (MATCH_VIEW_CACHE_MAX_BYTES, MATCH_VIEW_CACHE_MAX_ENTRIES,
                     MATCH_VIEW_CACHE_MAX_STATE, App, MatchViewCache)


def match_view_data(state_version=1):
    move = {"n": 1, "side": "white", "from": "a1", "to": "b1", "piece": "king",
            "promotion": None, "captured": None}
    state = {
        "phase": "play",
        "board": {"b1": {"side": "white", "piece": "king", "promotion": None}},
        "remaining": {
            "white": {"scissors": 3, "hammer": 3, "paper": 3, "king": 0},
            "black": {"scissors": 3, "hammer": 3, "paper": 3, "king": 1},
        },
        "history": [{"phase": "placement", "side": "white", "action": {"type": "place", "piece": "king", "to": "a1"}}],
        "moves_log": [move], "move_count": 1,
    }
    return {
        "id": "match-1", "tournament_id": "tournament-1", "tournament_name": "Cup",
        "round_no": 1, "leg_no": 1, "pairing_no": 1,
        "white_entry_id": "white-entry", "black_entry_id": "black-entry",
        "white_name": "white", "black_name": "black", "status": "FINISHED",
        "result": "DRAW", "result_reason": "MOVE_LIMIT", "phase": "FINISHED",
        "move_count": 1, "side_to_act": None, "state_json": json.dumps(state),
        "white_clock_ms": 0, "black_clock_ms": 0, "clock_seconds": 300,
        "game_id": "ott", "game_version": "1", "state_version": state_version,
    }


class MatchViewCacheTests(unittest.TestCase):
    def test_detail_reuses_public_view_without_mutating_cached_moves(self):
        app = object.__new__(App)
        app.match_view_cache = MatchViewCache()
        item = match_view_data()
        with patch("ott.api.json.loads", wraps=json.loads) as loads:
            compact = app.detail(item, with_moves=False)
            full = app.detail(item, with_moves=True)
            item["white_clock_ms"] = 1234
            updated_clock = app.detail(item, with_moves=True)
        self.assertNotIn("moves", compact)
        self.assertEqual(full["moves"], json.loads(item["state_json"])["moves_log"])
        self.assertEqual(updated_clock["clocks_ms"]["white"], 1234)
        self.assertEqual(loads.call_count, 2)

    def test_state_version_invalidates_view_and_older_race_cannot_replace_newer(self):
        app = object.__new__(App)
        app.match_view_cache = MatchViewCache()
        item = match_view_data()
        app.detail(item)
        state = json.loads(item["state_json"])
        state["moves_log"].append({"n": 2, "side": "black", "from": "i9", "to": "h9",
                                    "piece": "king", "promotion": None, "captured": None})
        state["move_count"] = 2
        item["state_json"] = json.dumps(state)
        item["state_version"] = 2
        self.assertEqual(len(app.detail(item)["moves"]), 2)

        newest = {"moves": [2]}
        older = {"moves": [1]}
        cache = MatchViewCache()
        cache.put("match", "ott", "1", 2, newest, 10)
        cache.put("match", "ott", "1", 1, older, 10)
        self.assertIs(cache.get("match", "ott", "1", 2), newest)

    def test_cache_memory_and_entry_count_stay_bounded(self):
        cache = MatchViewCache()
        for index in range(MATCH_VIEW_CACHE_MAX_ENTRIES + 8):
            cache.put(f"match-{index}", "ott", "1", 1, {"moves": []}, 1024)
        cache.put("large", "ott", "1", 1, {"moves": []}, MATCH_VIEW_CACHE_MAX_STATE + 1)
        self.assertLessEqual(len(cache._entries), MATCH_VIEW_CACHE_MAX_ENTRIES)
        self.assertLessEqual(cache._size, MATCH_VIEW_CACHE_MAX_BYTES)
        self.assertIsNone(cache.get("large", "ott", "1", 1))


if __name__ == "__main__":
    unittest.main()
