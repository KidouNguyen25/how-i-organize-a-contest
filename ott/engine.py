"""OTT v1 rules engine (9x9, Kéo/Búa/Bao/Vua). Pure functions on a JSON-serialisable state dict."""

from __future__ import annotations

import json
from typing import Any, Callable

try:
    import orjson
except ImportError:
    orjson = None

FILES = "abcdefghi"
_SQUARES = tuple(tuple(f"{FILES[file]}{rank + 1}" for file in range(9)) for rank in range(9))
_SQUARE_COORDINATES = {square: (file, rank) for rank, row in enumerate(_SQUARES) for file, square in enumerate(row)}
_HOME_SQUARES = {"white": _SQUARES[0] + _SQUARES[1], "black": _SQUARES[7] + _SQUARES[8]}
PIECES = ("scissors", "hammer", "paper", "king")
BEATS = {"scissors": "paper", "paper": "hammer", "hammer": "scissors"}
START_COUNTS = {"scissors": 3, "hammer": 3, "paper": 3, "king": 1}
MAX_MOVES = 500
REPETITION_LIMIT = 3
ORTHO = ((1, 0), (-1, 0), (0, 1), (0, -1))
DIAG = ((1, 1), (1, -1), (-1, 1), (-1, -1))
EIGHT = ORTHO + DIAG
_PAWN_STEPS = {
    1: ((0, 1, True), (-1, 1, True), (1, 1, True), (-1, -1, False), (1, -1, False)),
    -1: ((0, -1, True), (-1, -1, True), (1, -1, True), (-1, 1, False), (1, 1, False)),
}


def _build_move_tables():
    king_targets = {}
    pawn_targets = {direction: {} for direction in _PAWN_STEPS}
    promoted_rays = {"rook": {}, "bishop": {}}
    promoted_steps = {"rook": {}, "bishop": {}}
    for origin, (file, rank) in _SQUARE_COORDINATES.items():
        king_targets[origin] = tuple(
            _SQUARES[rank + dy][file + dx]
            for dx, dy in EIGHT
            if 0 <= file + dx < 9 and 0 <= rank + dy < 9
        )
        for direction, steps in _PAWN_STEPS.items():
            pawn_targets[direction][origin] = tuple(
                (_SQUARES[rank + dy][file + dx], rank + dy, can_capture)
                for dx, dy, can_capture in steps
                if 0 <= file + dx < 9 and 0 <= rank + dy < 9
            )
        for promotion, directions in (("rook", ORTHO), ("bishop", DIAG)):
            rays = []
            for dx, dy in directions:
                ray = []
                next_file, next_rank = file + dx, rank + dy
                while 0 <= next_file < 9 and 0 <= next_rank < 9:
                    ray.append(_SQUARES[next_rank][next_file])
                    next_file += dx
                    next_rank += dy
                rays.append(tuple(ray))
            promoted_rays[promotion][origin] = tuple(rays)
            promoted_steps[promotion][origin] = tuple(
                _SQUARES[rank + dy][file + dx]
                for dx, dy in EIGHT
                if (dx, dy) not in directions and 0 <= file + dx < 9 and 0 <= rank + dy < 9
            )
    return king_targets, pawn_targets, promoted_rays, promoted_steps


_KING_TARGETS, _PAWN_TARGETS, _PROMOTED_RAYS, _PROMOTED_STEPS = _build_move_tables()
_POSITION_ALPHABET = ".0123456789abcdefghijklmn"
_POSITION_PIECE_INDEX = {piece: index for index, piece in enumerate(PIECES)}
_POSITION_PROMOTION_INDEX = {None: 0, "rook": 1, "bishop": 2}
_POSITION_SQUARE_INDEX = {
    square: rank * 9 + file
    for rank, row in enumerate(_SQUARES)
    for file, square in enumerate(row)
}
_POSITION_SYMBOLS = {
    side: {
        piece: {
            promotion: _POSITION_ALPHABET[
                1 + (12 if side == "black" else 0)
                + 3 * _POSITION_PIECE_INDEX[piece]
                + _POSITION_PROMOTION_INDEX[promotion]
            ]
            for promotion in _POSITION_PROMOTION_INDEX
        }
        for piece in PIECES
    }
    for side in ("white", "black")
}
_PLACE_ACTION_KEYS = frozenset(("type", "piece", "to"))
_MOVE_ACTION_KEYS = frozenset(("type", "from", "to"))
_PROMOTION_ACTION_KEYS = _MOVE_ACTION_KEYS | {"promotion"}

Action = dict[str, Any]


def sq(f: int, r: int) -> str:
    return f"{FILES[f]}{r + 1}"


def parse_sq(s: str) -> tuple[int, int]:
    coordinates = _SQUARE_COORDINATES.get(s)
    if coordinates is not None:
        return coordinates
    return FILES.index(s[0]), int(s[1]) - 1


def _is_square(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 2 and value[0] in FILES and value[1] in "123456789"


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


def _piece_moves(board: dict[str, Any], origin: str, pc: dict[str, Any],
                 append: Callable[[Action], None]) -> None:
    side = pc["side"]

    if pc["piece"] == "king":
        for dest in _KING_TARGETS[origin]:
            if dest not in board:
                append({"type": "move", "from": origin, "to": dest})
    elif not pc["promotion"]:
        promotion_rank = 8 if side == "white" else 0
        for dest, nr, can_capture in _PAWN_TARGETS[1 if side == "white" else -1][origin]:
            target = board.get(dest)
            if target is None:
                if nr == promotion_rank:
                    append({"type": "move", "from": origin, "to": dest, "promotion": "rook"})
                    append({"type": "move", "from": origin, "to": dest, "promotion": "bishop"})
                else:
                    append({"type": "move", "from": origin, "to": dest})
            elif target["side"] != side and can_capture and _can_capture(pc, target):
                if nr == promotion_rank:
                    append({"type": "move", "from": origin, "to": dest, "promotion": "rook"})
                    append({"type": "move", "from": origin, "to": dest, "promotion": "bishop"})
                else:
                    append({"type": "move", "from": origin, "to": dest})
    else:
        promotion = "rook" if pc["promotion"] == "rook" else "bishop"
        for ray in _PROMOTED_RAYS[promotion][origin]:
            for dest in ray:
                target = board.get(dest)
                if target is None:
                    append({"type": "move", "from": origin, "to": dest})
                    continue
                if target["side"] != side and _can_capture(pc, target):
                    append({"type": "move", "from": origin, "to": dest})
                break
        for dest in _PROMOTED_STEPS[promotion][origin]:
            target = board.get(dest)
            if target is None or (target["side"] != side and _can_capture(pc, target)):
                append({"type": "move", "from": origin, "to": dest})


def _piece_has_move(board: dict[str, Any], origin: str, pc: dict[str, Any]) -> bool:
    """Check terminal state without allocating every legal action for a piece."""
    side = pc["side"]
    f, r = parse_sq(origin)
    if pc["piece"] == "king":
        for dx, dy in EIGHT:
            nf, nr = f + dx, r + dy
            if in_board(nf, nr) and _SQUARES[nr][nf] not in board:
                return True
        return False
    if not pc["promotion"]:
        for dx, dy, can_capture in _PAWN_STEPS[1 if side == "white" else -1]:
            nf, nr = f + dx, r + dy
            if not in_board(nf, nr):
                continue
            target = board.get(_SQUARES[nr][nf])
            if target is None or (target["side"] != side and can_capture and _can_capture(pc, target)):
                return True
        return False

    slide = ORTHO if pc["promotion"] == "rook" else DIAG
    for dx, dy in slide:
        nf, nr = f + dx, r + dy
        while in_board(nf, nr):
            target = board.get(_SQUARES[nr][nf])
            if target is None:
                return True
            if target["side"] != side and _can_capture(pc, target):
                return True
            break
    for dx, dy in EIGHT:
        if (dx, dy) in slide:
            continue
        nf, nr = f + dx, r + dy
        if not in_board(nf, nr):
            continue
        target = board.get(_SQUARES[nr][nf])
        if target is None or (target["side"] != side and _can_capture(pc, target)):
            return True
    return False


def _piece_can_move_to(board: dict[str, Any], origin: str, pc: dict[str, Any], dest: str,
                       destination: tuple[int, int]) -> bool:
    side = pc["side"]
    f, r = parse_sq(origin)
    nf, nr = destination
    dx, dy = nf - f, nr - r
    if dx == 0 and dy == 0:
        return False
    target = board.get(dest)
    if pc["piece"] == "king":
        return (dx, dy) in EIGHT and target is None
    if not pc["promotion"]:
        for step_f, step_r, can_capture in _PAWN_STEPS[1 if side == "white" else -1]:
            if step_f == dx and step_r == dy:
                return target is None or (
                    can_capture and target["side"] != side and _can_capture(pc, target)
                )
        return False

    slide = ORTHO if pc["promotion"] == "rook" else DIAG
    step = (0 if dx == 0 else (1 if dx > 0 else -1), 0 if dy == 0 else (1 if dy > 0 else -1))
    if step in slide:
        if dx and dy and abs(dx) != abs(dy):
            return False
        for offset in range(1, max(abs(dx), abs(dy))):
            if _SQUARES[r + step[1] * offset][f + step[0] * offset] in board:
                return False
        return target is None or (target["side"] != side and _can_capture(pc, target))
    return (dx, dy) in EIGHT and (dx, dy) not in slide and (
        target is None or (target["side"] != side and _can_capture(pc, target))
    )


def legal_actions(state: dict[str, Any], side: str | None = None) -> list[Action]:
    side = side or state["side"]
    board = state["board"]
    if state["phase"] == "placement":
        cells = [square for square in _HOME_SQUARES[side] if square not in board]
        remaining = state["remaining"][side]
        return [
            {"type": "place", "piece": piece, "to": cell}
            for piece in PIECES
            if remaining[piece] > 0
            for cell in cells
        ]
    out: list[Action] = []
    append = out.append
    for origin in sorted(board):
        pc = board[origin]
        if pc["side"] == side:
            _piece_moves(board, origin, pc, append)
    return out


def position_key(state: dict[str, Any]) -> str:
    """Encode exact board occupancy in 81 cells plus side-to-move."""
    cells = ["."] * 81
    symbols = _POSITION_SYMBOLS
    for square, piece in state["board"].items():
        by_promotion = symbols[piece["side"]][piece["piece"]]
        cells[_POSITION_SQUARE_INDEX[square]] = by_promotion[piece["promotion"]]
    return ("w" if state["side"] == "white" else "b") + "".join(cells)


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
    if captured and not any(p["side"] == other(side) for p in board.values()):
        return {**win, "reason": "ALL_CAPTURED"}
    key = position_key(state)
    state["positions"][key] = state["positions"].get(key, 0) + 1
    if state["positions"][key] >= REPETITION_LIMIT:
        return {"result": "DRAW", "reason": "REPETITION"}
    if state["move_count"] >= MAX_MOVES:
        return {"result": "DRAW", "reason": "MOVE_LIMIT"}
    if not any(
        _piece_has_move(board, origin, pc)
        for origin, pc in board.items() if pc["side"] == state["side"]
    ):
        return {**win, "reason": "NO_LEGAL_MOVES"}
    return None


def validate_action(state: dict[str, Any], action: Action) -> bool:
    if not isinstance(action, dict):
        return False
    side, board = state["side"], state["board"]
    if state["phase"] == "placement":
        if set(action) != {"type", "piece", "to"} or action["type"] != "place":
            return False
        piece, dest = action["piece"], action["to"]
        return (
            isinstance(piece, str) and piece in PIECES and _is_square(dest)
            and parse_sq(dest)[1] in home_rows(side) and dest not in board
            and state["remaining"][side][piece] > 0
        )
    origin = action.get("from")
    if action.get("type") != "move" or not isinstance(origin, str):
        return False
    piece = board.get(origin)
    if (not piece or piece["side"] != side or action.keys() not in (_MOVE_ACTION_KEYS, _PROMOTION_ACTION_KEYS)
            or not _is_square(action["to"])):
        return False
    dest = action["to"]
    destination = parse_sq(dest)
    nr = destination[1]
    promotes = piece["piece"] != "king" and not piece["promotion"] and nr == last_rank(side)
    if promotes:
        if "promotion" not in action or action["promotion"] not in ("rook", "bishop"):
            return False
    elif "promotion" in action:
        return False
    return _piece_can_move_to(board, origin, piece, dest, destination)


def _observation(state: dict[str, Any], side: str, match_id: str, clocks_ms: dict[str, int], *,
                isolate_mutable_fields: bool) -> dict[str, Any]:
    if state["phase"] == "placement":
        board = {k: v for k, v in state["board"].items() if v["side"] == side}
        history = [h for h in state["history"] if h["side"] == side]
        remaining = state["remaining"][side]
        phase = "placement"
    else:
        board = state["board"]
        history = state["history"]
        remaining = {p: 0 for p in PIECES}
        phase = "play"
    if isolate_mutable_fields:
        board = {square: dict(piece) for square, piece in board.items()}
        history = [entry | {"action": dict(entry["action"])} for entry in history]
        if phase == "placement":
            remaining = dict(remaining)
    return {
        "protocol_version": 1,
        "match_id": match_id,
        "phase": phase,
        "side": side,
        "board": board,
        "remaining_pieces": remaining,
        "clocks_ms": {"white": int(clocks_ms["white"]), "black": int(clocks_ms["black"])},
        "legal_actions": legal_actions(state, side),
        "history": history,
    }


def observation(state: dict[str, Any], side: str, match_id: str, clocks_ms: dict[str, int]) -> dict[str, Any]:
    """Return an isolated protocol view for callers that may mutate it."""
    return _observation(state, side, match_id, clocks_ms, isolate_mutable_fields=True)


def encode_observation(state: dict[str, Any], side: str, match_id: str, clocks_ms: dict[str, int]) -> bytes:
    """Serialize protocol view without cloning state fields used only for immediate encoding."""
    view = _observation(state, side, match_id, clocks_ms, isolate_mutable_fields=False)
    if orjson is not None:
        try:
            return orjson.dumps(view)
        except TypeError:
            pass
    return json.dumps(view, separators=(",", ":"), check_circular=False).encode("utf-8")


def parse_action(line: str) -> Action:
    """Parse one JSON action line. Reject duplicate keys, extra keys and wrong shapes."""

    def no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate key")
            data[key] = value
        return data

    data = json.loads(line, object_pairs_hook=no_dupes)
    if not isinstance(data, dict):
        raise ValueError("action must be an object")
    kind = data.get("type")
    keys = data.keys()
    if kind == "place":
        if keys != _PLACE_ACTION_KEYS or data["piece"] not in PIECES or not _is_square(data["to"]):
            raise ValueError("bad place action")
    elif kind == "move":
        if (
            (keys != _MOVE_ACTION_KEYS and keys != _PROMOTION_ACTION_KEYS)
            or not _is_square(data["from"])
            or not _is_square(data["to"])
        ):
            raise ValueError("bad move action")
        if "promotion" in data and data["promotion"] not in ("rook", "bishop"):
            raise ValueError("bad promotion")
    else:
        raise ValueError("unknown action type")
    return data


def public_board(state: dict[str, Any]) -> dict[str, Any] | None:
    """Board visible to spectators; None while placements are secret."""
    return {square: dict(piece) for square, piece in state["board"].items()} if state["phase"] == "play" else None


def placed_counts(state: dict[str, Any]) -> dict[str, int]:
    return {s: 10 - sum(state["remaining"][s].values()) for s in ("white", "black")}
