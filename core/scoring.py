"""A04: Modular Scoring Policies and Deterministic Tie-breaking Strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
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
                if curr is None or new_score > curr.raw_metrics.get("score", 0.0):
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
        stats: dict[str, dict[str, Any]] = {
            e.entry_id: {
                "points": 0.0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "played": 0,
                "h2h": {}  # (other_entry_id) -> points won against them
            }
            for e in entries
        }

        # Process each match result
        for res in results:
            if res.status != "SUCCEEDED" and res.status.name != "SUCCEEDED":
                continue
            white_id = res.raw_metrics.get("white_entry_id")
            black_id = res.raw_metrics.get("black_entry_id")
            if not white_id or not black_id or white_id not in stats or black_id not in stats:
                continue

            v = res.verdict.name if hasattr(res.verdict, "name") else str(res.verdict)
            if v == "WHITE_WIN":
                pw, pb = pts_win, pts_loss
                stats[white_id]["wins"] += 1
                stats[black_id]["losses"] += 1
            elif v == "BLACK_WIN":
                pw, pb = pts_loss, pts_win
                stats[black_id]["wins"] += 1
                stats[white_id]["losses"] += 1
            else:  # DRAW
                pw, pb = pts_draw, pts_draw
                stats[white_id]["draws"] += 1
                stats[black_id]["draws"] += 1

            stats[white_id]["points"] += pw
            stats[black_id]["points"] += pb
            stats[white_id]["played"] += 1
            stats[black_id]["played"] += 1

            stats[white_id]["h2h"][black_id] = stats[white_id]["h2h"].get(black_id, 0.0) + pw
            stats[black_id]["h2h"][white_id] = stats[black_id]["h2h"].get(white_id, 0.0) + pb

        # Calculate Head-to-Head points for tied groups and build rows
        rows: list[StandingRow] = []
        for e_id, s in stats.items():
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

        # Compute head-to-head within groups of identical points
        for row in rows:
            tied_peer_ids = [r.entry_id for r in rows if r.points == row.points and r.entry_id != row.entry_id]
            h2h_sum = sum(stats[row.entry_id]["h2h"].get(peer_id, 0.0) for peer_id in tied_peer_ids)
            row.head_to_head_points = h2h_sum

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
