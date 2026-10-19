"""OTT v1 rules engine (9x9, Kéo/Búa/Bao/Vua). Pure functions on a JSON-serialisable state dict."""

from __future__ import annotations

import copy
import json
from typing import Any

FILES = "abcdefghi"
PIECES = ("scissors", "hammer", "paper", "king")
BEATS = {"scissors": "paper", "paper": "hammer", "hammer": "scissors"}
START_COUNTS = {"scissors": 3, "hammer": 3, "paper": 3, "king": 1}
MAX_MOVES = 500
REPETITION_LIMIT = 3
ORTHO = ((1, 0), (-1, 0), (0, 1), (0, -1))
DIAG = ((1, 1), (1, -1), (-1, 1), (-1, -1))
EIGHT = ORTHO + DIAG

Action = dict[str, Any]


def sq(f: int, r: int) -> str:
    return f"{FILES[f]}{r + 1}"


def parse_sq(s: str) -> tuple[int, int]:
    return FILES.index(s[0]), int(s[1]) - 1


def in_board(f: int, r: int) -> bool:
    return 0 <= f < 9 and 0 <= r < 9


def other(side: str) -> str:
    return "black" if side == "white" else "white"


def new_state() -> dict[str, Any]:
    return {
        "phase": "placement",
        "side": "white",
        "board": {},
        "remaining": {"white": dict(START_COUNTS), "black": dict(START_COUNTS)},
        "history": [],  # bot-protocol history entries
        "moves_log": [],  # public move log with capture info
        "move_count": 0,
        "positions": {},
    }


def home_rows(side: str) -> tuple[int, int]:
    return (0, 1) if side == "white" else (7, 8)


def last_rank(side: str) -> int:
    return 8 if side == "white" else 0


def _can_capture(attacker: dict[str, Any], target: dict[str, Any]) -> bool:
    if target["piece"] == "king":
        return True
    if BEATS.get(attacker["piece"]) == target["piece"]:
        return True
    return bool(attacker["promotion"]) and attacker["piece"] == target["piece"]


def _piece_moves(board: dict[str, Any], origin: str, pc: dict[str, Any]) -> list[Action]:
    side = pc["side"]
    f, r = parse_sq(origin)
    fwd = 1 if side == "white" else -1
    out: list[Action] = []

    def emit(nf: int, nr: int, can_capture: bool) -> bool:
        """Add destination; return True when the square was empty (slide may continue)."""
        if not in_board(nf, nr):
            return False
        dest = sq(nf, nr)
        target = board.get(dest)
        if target is None:
            _add(out, origin, dest, pc, nr)
            return True
        if target["side"] != side and can_capture and _can_capture(pc, target):
            _add(out, origin, dest, pc, nr)
        return False

    if pc["piece"] == "king":
        for dx, dy in EIGHT:
            nf, nr = f + dx, r + dy
            if in_board(nf, nr) and sq(nf, nr) not in board:
                _add(out, origin, sq(nf, nr), pc, nr)
    elif not pc["promotion"]:
        emit(f, r + fwd, True)
        emit(f - 1, r + fwd, True)
        emit(f + 1, r + fwd, True)
        emit(f - 1, r - fwd, False)
        emit(f + 1, r - fwd, False)
    else:
        slide = ORTHO if pc["promotion"] == "rook" else DIAG
        for dx, dy in slide:
            nf, nr = f + dx, r + dy
            while emit(nf, nr, True):
                nf += dx
                nr += dy
        for dx, dy in EIGHT:
            if (dx, dy) not in slide:
                emit(f + dx, r + dy, True)
    return out


def _add(out: list[Action], origin: str, dest: str, pc: dict[str, Any], dest_rank: int) -> None:
    if pc["piece"] != "king" and not pc["promotion"] and dest_rank == last_rank(pc["side"]):
        out.append({"type": "move", "from": origin, "to": dest, "promotion": "rook"})
        out.append({"type": "move", "from": origin, "to": dest, "promotion": "bishop"})
    else:
        out.append({"type": "move", "from": origin, "to": dest})


def legal_actions(state: dict[str, Any], side: str | None = None) -> list[Action]:
    side = side or state["side"]
    board = state["board"]
    if state["phase"] == "placement":
        cells = [sq(f, r) for r in home_rows(side) for f in range(9) if sq(f, r) not in board]
        remaining = state["remaining"][side]
        return [
            {"type": "place", "piece": piece, "to": cell}
            for piece in PIECES
            if remaining[piece] > 0
            for cell in cells
        ]
    out: list[Action] = []
    for origin, pc in sorted(board.items()):
        if pc["side"] == side:
            out.extend(_piece_moves(board, origin, pc))
    return out


def _canon(action: Action) -> tuple:
    return tuple(sorted(action.items()))


def position_key(state: dict[str, Any]) -> str:
    pieces = sorted((k, v["side"], v["piece"], v["promotion"] or "") for k, v in state["board"].items())
    return state["side"] + "|" + json.dumps(pieces, separators=(",", ":"))


def apply_action(state: dict[str, Any], action: Action) -> dict[str, str] | None:
    """Mutate state with a pre-validated action. Return outcome {'result','reason'} or None."""
    side = state["side"]
    if state["phase"] == "placement":
        state["board"][action["to"]] = {"side": side, "piece": action["piece"], "promotion": None}
        state["remaining"][side][action["piece"]] -= 1
        state["history"].append({"phase": "placement", "side": side, "action": dict(action)})
        if all(v == 0 for s in state["remaining"].values() for v in s.values()):
            state["phase"] = "play"
            state["side"] = "white"
            state["positions"][position_key(state)] = 1
        else:
            state["side"] = other(side)
        return None

    board = state["board"]
    mover = dict(board.pop(action["from"]))
    captured = board.get(action["to"])
    if action.get("promotion"):
        mover["promotion"] = action["promotion"]
    board[action["to"]] = mover
    state["move_count"] += 1
    state["history"].append({"phase": "play", "side": side, "action": dict(action)})
    state["moves_log"].append(
        {
            "n": state["move_count"],
            "side": side,
            "from": action["from"],
            "to": action["to"],
            "piece": mover["piece"],
            "promotion": action.get("promotion"),
            "captured": captured["piece"] if captured else None,
        }
    )
    state["side"] = other(side)
    win = {"result": "WHITE_WIN" if side == "white" else "BLACK_WIN"}
    if captured and captured["piece"] == "king":
        return {**win, "reason": "KING_CAPTURED"}
    if not any(p["side"] == other(side) for p in board.values()):
        return {**win, "reason": "ALL_CAPTURED"}
    key = position_key(state)
    state["positions"][key] = state["positions"].get(key, 0) + 1
    if state["positions"][key] >= REPETITION_LIMIT:
        return {"result": "DRAW", "reason": "REPETITION"}
    if state["move_count"] >= MAX_MOVES:
        return {"result": "DRAW", "reason": "MOVE_LIMIT"}
    if not legal_actions(state):
        return {**win, "reason": "NO_LEGAL_MOVES"}
    return None


def validate_action(state: dict[str, Any], action: Action) -> bool:
    legal = {_canon(a) for a in legal_actions(state)}
    return _canon(action) in legal


def observation(state: dict[str, Any], side: str, match_id: str, clocks_ms: dict[str, int]) -> dict[str, Any]:
    if state["phase"] == "placement":
        board = {k: v for k, v in state["board"].items() if v["side"] == side}
        history = [h for h in state["history"] if h["side"] == side]
        remaining = dict(state["remaining"][side])
        phase = "placement"
    else:
        board = state["board"]
        history = state["history"]
        remaining = {p: 0 for p in PIECES}
        phase = "play"
    return {
        "protocol_version": 1,
        "match_id": match_id,
        "phase": phase,
        "side": side,
        "board": copy.deepcopy(board),
        "remaining_pieces": remaining,
        "clocks_ms": {"white": int(clocks_ms["white"]), "black": int(clocks_ms["black"])},
        "legal_actions": legal_actions(state, side),
        "history": copy.deepcopy(history),
    }


def parse_action(line: str) -> Action:
    """Parse one JSON action line. Reject duplicate keys, extra keys and wrong shapes."""

    def no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(pairs)

    data = json.loads(line, object_pairs_hook=no_dupes)
    if not isinstance(data, dict):
        raise ValueError("action must be an object")
    kind = data.get("type")
    coord = lambda v: isinstance(v, str) and len(v) == 2 and v[0] in FILES and v[1] in "123456789"  # noqa: E731
    if kind == "place":
        if set(data) != {"type", "piece", "to"} or data["piece"] not in PIECES or not coord(data["to"]):
            raise ValueError("bad place action")
    elif kind == "move":
        extra = set(data) - {"type", "from", "to", "promotion"}
        if extra or not {"type", "from", "to"} <= set(data) or not coord(data["from"]) or not coord(data["to"]):
            raise ValueError("bad move action")
        if "promotion" in data and data["promotion"] not in ("rook", "bishop"):
            raise ValueError("bad promotion")
    else:
        raise ValueError("unknown action type")
    return data


def public_board(state: dict[str, Any]) -> dict[str, Any] | None:
    """Board visible to spectators; None while placements are secret."""
    return copy.deepcopy(state["board"]) if state["phase"] == "play" else None


def placed_counts(state: dict[str, Any]) -> dict[str, int]:
    return {s: 10 - sum(state["remaining"][s].values()) for s in ("white", "black")}
