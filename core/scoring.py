"""A04: Modular Scoring Policies and Deterministic Tie-breaking Strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from array import array
from typing import Any
from core.models import Entry, EvaluationResult, StandingRow, Standings, Verdict


class BaseScorer(ABC):
    """Abstract Strategy interface for calculating contest standings."""

    @abstractmethod
    def calculate_standings(
        self,
        season_id: str,
        entries: list[Entry],
        results: list[EvaluationResult],
        config: dict[str, Any],
        revision: int = 1,
        timestamp: int = 0
    ) -> Standings:
        pass


class TestSuiteWeightedSumScorer(BaseScorer):
    """Scoring policy for classic algorithmic test-suite problems."""

    def calculate_standings(
        self,
        season_id: str,
        entries: list[Entry],
        results: list[EvaluationResult],
        config: dict[str, Any],
        revision: int = 1,
        timestamp: int = 0
    ) -> Standings:
        # Map latest/best result per entry
        results_by_entry: dict[str, EvaluationResult] = {}
        for res in results:
            entry_id = res.raw_metrics.get("entry_id")
            if entry_id:
                # Keep highest score, tiebreak by lowest execution time
                curr = results_by_entry.get(entry_id)
                new_score = res.raw_metrics.get("score", 0.0)
                if curr is None:
                    results_by_entry[entry_id] = res
                    continue
                curr_score = curr.raw_metrics.get("score", 0.0)
                if (new_score > curr_score or
                        (new_score == curr_score and
                         res.resource_usage.get("cpu_time_ms", 0) < curr.resource_usage.get("cpu_time_ms", 0))):
                    results_by_entry[entry_id] = res

        rows: list[StandingRow] = []
        for entry in entries:
            res = results_by_entry.get(entry.entry_id)
            score = float(res.raw_metrics.get("score", 0.0)) if res else 0.0
            cpu_time = res.resource_usage.get("cpu_time_ms", 0) if res else 0
            sub_time = int(res.raw_metrics.get("received_at", 0)) if res else 0

            rows.append(StandingRow(
                rank=0,
                entry_id=entry.entry_id,
                team_name=entry.team_name,
                points=score,
                penalty_time=cpu_time,
                tie_break_values={
                    "total_score": score,
                    "cpu_time_ms": cpu_time,
                    "submission_time": sub_time,
                    "entry_id": entry.entry_id
                }
            ))

        # Multi-level tiebreak: Score (desc) -> CPU Time (asc) -> Submission Time (asc) -> Entry ID (asc)
        rows.sort(key=lambda r: (
            -r.points,
            r.tie_break_values["cpu_time_ms"],
            r.tie_break_values["submission_time"],
            r.entry_id
        ))

        # Assign ranks
        prev_key = None
        prev_rank = 0
        for idx, row in enumerate(rows, start=1):
            key = (row.points, row.tie_break_values["cpu_time_ms"])
            if key == prev_key:
                row.rank = prev_rank
            else:
                row.rank = idx
                prev_rank = idx
                prev_key = key

        return Standings(
            season_id=season_id,
            standings_revision=revision,
            updated_at=timestamp,
            rows=rows
        )


class BotArenaMatchPointsScorer(BaseScorer):
    """Scoring policy for turn-based bot match arenas (OTT double round-robin)."""

    def calculate_standings(
        self,
        season_id: str,
        entries: list[Entry],
        results: list[EvaluationResult],
        config: dict[str, Any],
        revision: int = 1,
        timestamp: int = 0
    ) -> Standings:
        pts_win = config.get("points_win", 3.0)
        pts_draw = config.get("points_draw", 1.0)
        pts_loss = config.get("points_loss", 0.0)

        entry_map = {e.entry_id: e for e in entries}
        entry_ids = list(entry_map)
        entry_index = {entry_id: index for index, entry_id in enumerate(entry_ids)}
        entry_count = len(entry_ids)
        max_h2h_points = max(len(results), 1)
        integer_scoring = all(
            (isinstance(value, int) or (isinstance(value, float) and value.is_integer()))
            and abs(value) * max_h2h_points <= 2**53
            for value in (pts_win, pts_draw, pts_loss)
        )
        # Integer awards keep matrix summation exact; fractional scoring keeps insertion order.
        dense_h2h = (
            32 <= entry_count <= 2048
            and len(results) >= entry_count * (entry_count - 1) // 4
            and integer_scoring
        )
        stats: list[dict[str, Any]] = [
            {
                "points": 0.0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "played": 0,
                **({} if dense_h2h else {"h2h": {}}),
            }
            for _ in entry_ids
        ]
        h2h_matrix = array("d", [0.0]) * (entry_count * entry_count) if dense_h2h else array("d")

        # Process each match result
        for res in results:
            if res.status != "SUCCEEDED" and res.status.name != "SUCCEEDED":
                continue
            white_id = res.raw_metrics.get("white_entry_id")
            black_id = res.raw_metrics.get("black_entry_id")
            white_index = entry_index.get(white_id)
            black_index = entry_index.get(black_id)
            if not white_id or not black_id or white_index is None or black_index is None:
                continue
            white_stats, black_stats = stats[white_index], stats[black_index]

            v = res.verdict.name if hasattr(res.verdict, "name") else str(res.verdict)
            if v == "WHITE_WIN":
                pw, pb = pts_win, pts_loss
                white_stats["wins"] += 1
                black_stats["losses"] += 1
            elif v == "BLACK_WIN":
                pw, pb = pts_loss, pts_win
                black_stats["wins"] += 1
                white_stats["losses"] += 1
            else:  # DRAW
                pw, pb = pts_draw, pts_draw
                white_stats["draws"] += 1
                black_stats["draws"] += 1

            white_stats["points"] += pw
            black_stats["points"] += pb
            white_stats["played"] += 1
            black_stats["played"] += 1

            if dense_h2h:
                h2h_matrix[white_index * entry_count + black_index] += pw
                h2h_matrix[black_index * entry_count + white_index] += pb
            else:
                white_stats["h2h"][black_index] = white_stats["h2h"].get(black_index, 0.0) + pw
                black_stats["h2h"][white_index] = black_stats["h2h"].get(white_index, 0.0) + pb

        # Calculate Head-to-Head points for tied groups and build rows
        rows: list[StandingRow] = []
        for index, e_id in enumerate(entry_ids):
            s = stats[index]
            entry = entry_map[e_id]
            diff = float(s["wins"] - s["losses"])
            rows.append(StandingRow(
                rank=0,
                entry_id=e_id,
                team_name=entry.team_name,
                points=s["points"],
                wins=s["wins"],
                draws=s["draws"],
                losses=s["losses"],
                played=s["played"],
                score_diff=diff,
                tie_break_values={
                    "registered_at": entry.registered_at,
                    "entry_id": e_id
                }
            ))

        # Sum played head-to-head matches within each points group.
        if dense_h2h:
            tied_groups: dict[float, list[int]] = {}
            for index, row in enumerate(rows):
                tied_groups.setdefault(row.points, []).append(index)
            for index, row in enumerate(rows):
                total = 0.0
                for opponent_index in tied_groups[row.points]:
                    if opponent_index != index:
                        total += h2h_matrix[index * entry_count + opponent_index]
                row.head_to_head_points = total
        else:
            for index, state in enumerate(stats):
                for opponent_index, points in state["h2h"].items():
                    if opponent_index != index and stats[opponent_index]["points"] == state["points"]:
                        rows[index].head_to_head_points += points

        # Multi-level tiebreak: Points (desc) -> H2H (desc) -> Diff (desc) -> Registration Time (asc) -> Entry ID (asc)
        rows.sort(key=lambda r: (
            -r.points,
            -r.head_to_head_points,
            -r.score_diff,
            r.tie_break_values["registered_at"],
            r.entry_id
        ))

        # Assign ranks
        prev_key = None
        prev_rank = 0
        for idx, row in enumerate(rows, start=1):
            key = (row.points, row.head_to_head_points, row.score_diff)
            if key == prev_key:
                row.rank = prev_rank
            else:
                row.rank = idx
                prev_rank = idx
                prev_key = key

        return Standings(
            season_id=season_id,
            standings_revision=revision,
            updated_at=timestamp,
            rows=rows
        )


class CustomMetricScorer(BaseScorer):
    """Scoring policy for optimization problems on continuous metrics."""

    def calculate_standings(
        self,
        season_id: str,
        entries: list[Entry],
        results: list[EvaluationResult],
        config: dict[str, Any],
        revision: int = 1,
        timestamp: int = 0
    ) -> Standings:
        direction = config.get("direction", "MAX").upper()  # MAX or MIN
        metric_key = config.get("metric_key", "score")

        results_by_entry: dict[str, float] = {}
        for res in results:
            e_id = res.raw_metrics.get("entry_id")
            if e_id:
                val = float(res.raw_metrics.get(metric_key, 0.0))
                if e_id not in results_by_entry:
                    results_by_entry[e_id] = val
                else:
                    results_by_entry[e_id] = max(results_by_entry[e_id], val) if direction == "MAX" else min(results_by_entry[e_id], val)

        rows: list[StandingRow] = []
        for entry in entries:
            score = results_by_entry.get(entry.entry_id, 0.0)
            rows.append(StandingRow(
                rank=0,
                entry_id=entry.entry_id,
                team_name=entry.team_name,
                points=score,
                tie_break_values={"entry_id": entry.entry_id}
            ))

        multiplier = -1.0 if direction == "MAX" else 1.0
        rows.sort(key=lambda r: (multiplier * r.points, r.entry_id))

        for idx, row in enumerate(rows, start=1):
            row.rank = idx

        return Standings(
            season_id=season_id,
            standings_revision=revision,
            updated_at=timestamp,
            rows=rows
        )
