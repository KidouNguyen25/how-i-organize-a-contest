"""Round-robin scheduling, standings, and the match dispatcher."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ott.runner import MatchRunner
from ott.store import Store

LOG = logging.getLogger("ott.scheduler")
POINTS = {"win": 3, "draw": 1, "loss": 0}


def round_robin(entry_ids: list[str]) -> list[dict[str, Any]]:
    """Circle-method double round robin. Roster order decides colours; leg 2 swaps them."""
    slots: list[str | None] = list(entry_ids)
    if len(slots) % 2:
        slots.append(None)
    n = len(slots)
    rows: list[dict[str, Any]] = []
    rotation = slots[:]
    rounds: list[list[tuple[str | None, str | None]]] = []
    for _ in range(n - 1):
        rounds.append([(rotation[i], rotation[n - 1 - i]) for i in range(n // 2)])
        rotation = [rotation[0], rotation[-1]] + rotation[1:-1]
    order = {eid: i for i, eid in enumerate(entry_ids)}
    for leg in (1, 2):
        for r_idx, pairs in enumerate(rounds, start=1):
            pairing = 0
            for a, b in pairs:
                if a is None or b is None:
                    continue
                pairing += 1
                first, second = (a, b) if order[a] < order[b] else (b, a)
                white, black = (first, second) if leg == 1 else (second, first)
                rows.append({"round_no": r_idx, "leg_no": leg, "pairing_no": pairing, "white": white, "black": black})
    return rows


def byes(entry_ids: list[str], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(entry_ids) % 2 == 0:
        return []
    out = []
    seen = {(m["leg_no"], m["round_no"]): set() for m in matches}
    for m in matches:
        seen[(m["leg_no"], m["round_no"])] |= {m["white_entry_id"], m["black_entry_id"]}
    for (leg, rnd), present in sorted(seen.items()):
        for eid in entry_ids:
            if eid not in present:
                out.append({"leg_no": leg, "round_no": rnd, "entry_id": eid})
    return out


def standings(entries: list[dict[str, Any]], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_entry = {
        e["id"]: {"entry_id": e["id"], "username": e["username"], "points": 0, "wins": 0, "draws": 0, "losses": 0, "played": 0, "sheet": []}
        for e in entries
    }
    finished = sorted((m for m in matches if m["status"] == "FINISHED"), key=lambda m: (m["leg_no"], m["round_no"], m["pairing_no"]))
    h2h: dict[tuple[str, str], int] = {}
    for m in finished:
        w, b = by_entry.get(m["white_entry_id"]), by_entry.get(m["black_entry_id"])
        if not w or not b:
            continue
        res = m["result"]
        pw, pb = {"WHITE_WIN": (3, 0), "BLACK_WIN": (0, 3), "DRAW": (1, 1)}[res]
        for row, pts, other in ((w, pw, b), (b, pb, w)):
            row["points"] += pts
            row["played"] += 1
            key = "wins" if pts == 3 else "draws" if pts == 1 else "losses"
            row[key] += 1
            row["sheet"].append("w" if pts == 3 else "d" if pts == 1 else "l")
            h2h[(row["entry_id"], other["entry_id"])] = h2h.get((row["entry_id"], other["entry_id"]), 0) + pts
    rows = list(by_entry.values())
    for row in rows:
        group = [r["entry_id"] for r in rows if r["points"] == row["points"]]
        row["h2h"] = sum(h2h.get((row["entry_id"], o), 0) for o in group if o != row["entry_id"])
        row["diff"] = row["wins"] - row["losses"]
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

    def _launch(self, match: dict[str, Any]) -> None:
        if (self.data_dir / ".maintenance").exists() or self.stop_event.is_set():
            return
        lease = self.store.claim_match(match["id"], self.owner, max_running=self.workers)
        if lease is None:
            return
        runner = MatchRunner(self.store, self.data_dir, match["id"], lease=lease, stop_event=self.stop_event)
        thread = threading.Thread(target=runner.run, daemon=True, name=f"match-{match['id'][:8]}")
        self.active[match["id"]] = (thread, match["white_entry_id"], match["black_entry_id"])
        thread.start()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if (self.data_dir / ".maintenance").exists():
                    self.stop_event.wait(0.4)
                    continue
                self.store.recover_running()
                for mid in [m for m, (t, _, _) in self.active.items() if not t.is_alive()]:
                    del self.active[mid]
                if len(self.active) < self.workers:
                    busy = {e for _, w, b in self.active.values() for e in (w, b)}
                    running = {t["id"] for t in self.store.list_tournaments() if t["status"] == "RUNNING"}
                    for m in self.store.list_matches(statuses=("SCHEDULED",)):
                        if len(self.active) >= self.workers:
                            break
                        if m["tournament_id"] not in running or m["id"] in self.active:
                            continue
                        if m["white_entry_id"] in busy or m["black_entry_id"] in busy:
                            continue
                        busy |= {m["white_entry_id"], m["black_entry_id"]}
                        self._launch(m)
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
