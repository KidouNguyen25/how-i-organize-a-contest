"""Round-robin scheduling, standings, and the match dispatcher."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator

from ott.runner import MatchRunner
from ott.store import Lease, Store

LOG = logging.getLogger("ott.scheduler")
POINTS = {"win": 3, "draw": 1, "loss": 0}
_MATCH_POINTS = {"WHITE_WIN": (3, 0), "BLACK_WIN": (0, 3), "DRAW": (1, 1)}
ScheduleRow = tuple[int, int, int, str, str]


def iter_round_robin_rows(entry_ids: list[str]) -> Iterator[ScheduleRow]:
    """Yield compact circle-method rows without allocating one dict per match."""
    slots: list[int | None] = list(range(len(entry_ids)))
    if len(slots) % 2:
        slots.append(None)
    n = len(slots)
    for leg in (1, 2):
        rotation = slots[:]
        for r_idx in range(1, n):
            pairing = 0
            for i in range(n // 2):
                a, b = rotation[i], rotation[n - 1 - i]
                if a is None or b is None:
                    continue
                pairing += 1
                first, second = (a, b) if a < b else (b, a)
                white, black = (first, second) if leg == 1 else (second, first)
                white_id, black_id = entry_ids[white], entry_ids[black]
                yield r_idx, leg, pairing, white_id, black_id
            rotation.insert(1, rotation.pop())


def iter_round_robin(entry_ids: list[str]) -> Iterator[dict[str, Any]]:
    """Yield dictionary rows for callers that need named fields."""
    for round_no, leg_no, pairing_no, white_id, black_id in iter_round_robin_rows(entry_ids):
        yield {"round_no": round_no, "leg_no": leg_no, "pairing_no": pairing_no,
               "white": white_id, "black": black_id}


def round_robin(entry_ids: list[str]) -> list[dict[str, Any]]:
    """Circle-method double round robin. Roster order decides colours; leg 2 swaps them."""
    return list(iter_round_robin(entry_ids))


def byes(entry_ids: list[str], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    team_count = len(entry_ids)
    if team_count % 2 == 0 or not matches:
        return []

    # create_matches stores the full double round-robin atomically. In that case,
    # circle rotation gives one bye per team, in roster order, on each leg.
    if len(matches) == team_count * (team_count - 1):
        return [
            {"leg_no": leg, "round_no": round_no, "entry_id": entry_id}
            for leg in (1, 2)
            for round_no, entry_id in enumerate(entry_ids, start=1)
        ]

    # Preserve participant-based behavior for partial schedules.
    out = []
    seen = {(m["leg_no"], m["round_no"]): set() for m in matches}
    for m in matches:
        seen[(m["leg_no"], m["round_no"])] |= {m["white_entry_id"], m["black_entry_id"]}
    for (leg, rnd), present in sorted(seen.items()):
        for eid in entry_ids:
            if eid not in present:
                out.append({"leg_no": leg, "round_no": rnd, "entry_id": eid})
    return out


def standings(entries: list[dict[str, Any]], matches: Iterable[Any], *,
              matches_in_schedule_order: bool = False,
              matches_are_finished: bool = False,
              matches_count: int | None = None,
              include_sheet: bool = True) -> list[dict[str, Any]]:
    def new_row(entry: dict[str, Any]) -> dict[str, Any]:
        row = {"entry_id": entry["id"], "username": entry["username"], "points": 0,
               "wins": 0, "draws": 0, "losses": 0, "played": 0}
        if include_sheet:
            row["sheet"] = []
        return row

    by_entry = {entry["id"]: new_row(entry) for entry in entries}
    rows = list(by_entry.values())
    entry_index = {row["entry_id"]: index for index, row in enumerate(rows)}
    if matches_are_finished:
        finished = matches if matches_in_schedule_order else list(matches)
    else:
        finished = [m for m in matches if m["status"] == "FINISHED"]
    if not matches_in_schedule_order:
        finished.sort(key=lambda m: (m["leg_no"], m["round_no"], m["pairing_no"]))
    entry_count = len(rows)
    finished_count = matches_count if matches_count is not None else len(finished)
    dense_h2h = entry_count >= 32 and finished_count >= entry_count * (entry_count - 1) // 4
    if dense_h2h:
        # OTT double round-robin pair can score at most six points per direction.
        h2h_matrix = [bytearray(entry_count) for _ in rows]
        h2h_scores: dict[int, int] = {}
    else:
        h2h_matrix = []
        h2h_scores = {}
    for m in finished:
        white_index = entry_index.get(m["white_entry_id"])
        black_index = entry_index.get(m["black_entry_id"])
        if white_index is None or black_index is None:
            continue
        w, b = rows[white_index], rows[black_index]
        pw, pb = _MATCH_POINTS[m["result"]]
        w["points"] += pw
        b["points"] += pb
        w["played"] += 1
        b["played"] += 1
        if pw == 3:
            w["wins"] += 1
            b["losses"] += 1
        elif pw == 1:
            w["draws"] += 1
            b["draws"] += 1
        else:
            w["losses"] += 1
            b["wins"] += 1
        if include_sheet:
            w["sheet"].append("w" if pw == 3 else "d" if pw == 1 else "l")
            b["sheet"].append("w" if pb == 3 else "d" if pb == 1 else "l")
        if dense_h2h:
            h2h_matrix[white_index][black_index] += pw
            h2h_matrix[black_index][white_index] += pb
        else:
            white_key = white_index * entry_count + black_index
            black_key = black_index * entry_count + white_index
            h2h_scores[white_key] = h2h_scores.get(white_key, 0) + pw
            h2h_scores[black_key] = h2h_scores.get(black_key, 0) + pb
    for row in rows:
        row["h2h"] = 0
        row["diff"] = row["wins"] - row["losses"]
    if dense_h2h:
        tied_groups: dict[int, list[int]] = {}
        for index, row in enumerate(rows):
            tied_groups.setdefault(row["points"], []).append(index)
        for index, row in enumerate(rows):
            total = 0
            for opponent_index in tied_groups[row["points"]]:
                if opponent_index != index:
                    total += h2h_matrix[index][opponent_index]
            row["h2h"] = total
    else:
        for key, points in h2h_scores.items():
            index = key // entry_count
            opponent_index = key % entry_count
            row, opponent = rows[index], rows[opponent_index]
            if index != opponent_index and row["points"] == opponent["points"]:
                row["h2h"] += points
    rows.sort(key=lambda r: (-r["points"], -r["h2h"], -r["diff"], r["username"]))
    prev_key, prev_rank = None, 0
    for i, row in enumerate(rows, start=1):
        key = (row["points"], row["h2h"], row["diff"])
        row["rank"] = prev_rank if key == prev_key else i
        prev_key, prev_rank = key, row["rank"]
    return rows


class Dispatcher(threading.Thread):
    """Starts SCHEDULED matches of RUNNING tournaments, at most `workers` at once and one per bot."""

    def __init__(self, store: Store, data_dir: Path, workers: int = 1) -> None:
        super().__init__(daemon=True, name="match-dispatcher")
        self.store, self.data_dir, self.workers = store, data_dir, workers
        self.active: dict[str, tuple[threading.Thread, str, str]] = {}
        self.stop_event = threading.Event()
        self.owner = uuid.uuid4().hex

    def _launch(self, match: dict[str, Any], lease: Lease) -> None:
        if (self.data_dir / ".maintenance").exists() or self.stop_event.is_set():
            self.store.pause_match(match["id"], "INFRA: DISPATCH_STOPPED", lease=lease, retry=True)
            return
        try:
            runner = MatchRunner(self.store, self.data_dir, match["id"], lease=lease, stop_event=self.stop_event)
            thread = threading.Thread(target=runner.run, daemon=True, name=f"match-{match['id'][:8]}")
            self.active[match["id"]] = (thread, match["white_entry_id"], match["black_entry_id"])
            thread.start()
        except Exception:
            self.active.pop(match["id"], None)
            self.store.pause_match(match["id"], "INFRA: RUNNER_START_FAILED", lease=lease, retry=True)
            raise

    def run(self) -> None:
        self.store.enable_thread_connection_reuse()
        try:
            self._run_loop()
        finally:
            self.store.close_thread_connection()

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if (self.data_dir / ".maintenance").exists():
                    self.stop_event.wait(0.4)
                    continue
                self.store.recover_running()
                for mid in [m for m, (t, _, _) in self.active.items() if not t.is_alive()]:
                    del self.active[mid]
                while len(self.active) < self.workers and not self.stop_event.is_set():
                    if (self.data_dir / ".maintenance").exists():
                        break
                    claimed = self.store.claim_next_match(self.owner, max_running=self.workers)
                    if claimed is None:
                        break
                    match, lease = claimed
                    self._launch(match, lease)
            except Exception:  # noqa: BLE001
                LOG.exception("dispatcher tick failed")
            self.stop_event.wait(0.4)

    def shutdown(self, timeout: float = 15.0) -> None:
        """Keep the runtime data lock held until every writer has stopped."""
        self.stop_event.set()
        while self.is_alive():
            self.join(timeout=1)
        for thread, _, _ in list(self.active.values()):
            while thread.is_alive():
                thread.join(timeout=1)
