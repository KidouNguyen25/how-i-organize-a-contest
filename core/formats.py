"""A04: Tournament Format Schedule Generators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from core.models import Entry, EvaluationTask, Participant, SeasonSnapshot


class BaseFormatGenerator(ABC):
    @abstractmethod
    def generate_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        pass


class DoubleRoundRobinGenerator(BaseFormatGenerator):
    """Circle-method Double Round Robin tournament schedule generator."""

    def generate_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        entry_ids = [e.entry_id for e in entries]
        slots: list[str | None] = list(entry_ids)
        if len(slots) % 2 != 0:
            slots.append(None)  # Add bye placeholder
        
        n = len(slots)
        rounds: list[list[tuple[str | None, str | None]]] = []
        rotation = slots[:]
        for _ in range(n - 1):
            rounds.append([(rotation[i], rotation[n - 1 - i]) for i in range(n // 2)])
            rotation = [rotation[0], rotation[-1]] + rotation[1:-1]

        order = {eid: i for i, eid in enumerate(entry_ids)}
        tasks: list[EvaluationTask] = []
        task_counter = 0

        for leg in (1, 2):
            for r_idx, pairs in enumerate(rounds, start=1):
                pairing = 0
                for a, b in pairs:
                    if a is None or b is None:
                        continue  # Bye round
                    pairing += 1
                    task_counter += 1
                    first, second = (a, b) if order[a] < order[b] else (b, a)
                    white_id, black_id = (first, second) if leg == 1 else (second, first)

                    # Lookup selected submissions
                    white_entry = next(e for e in entries if e.entry_id == white_id)
                    black_entry = next(e for e in entries if e.entry_id == black_id)

                    tasks.append(EvaluationTask(
                        task_id=f"{snapshot.season_id}-match-L{leg}-R{r_idx}-P{pairing}",
                        season_id=snapshot.season_id,
                        snapshot_hash=snapshot.compute_hash(),
                        adapter_id=snapshot.adapter_id,
                        adapter_version=snapshot.adapter_version,
                        attempt_no=1,
                        participants=[
                            Participant(
                                entry_id=white_id,
                                submission_id=white_entry.selected_submission_id or "sub-white",
                                source_hash="hash-white",
                                side="WHITE"
                            ),
                            Participant(
                                entry_id=black_id,
                                submission_id=black_entry.selected_submission_id or "sub-black",
                                source_hash="hash-black",
                                side="BLACK"
                            )
                        ],
                        task_payload={
                            "leg_no": leg,
                            "round_no": r_idx,
                            "pairing_no": pairing,
                            "white_entry_id": white_id,
                            "black_entry_id": black_id
                        },
                        seed=1000 * leg + 100 * r_idx + pairing
                    ))
        return tasks


class DirectTaskQueueGenerator(BaseFormatGenerator):
    """Direct queue generator for test-suite algorithmic problems."""

    def generate_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        tasks: list[EvaluationTask] = []
        for idx, entry in enumerate(entries, start=1):
            if not entry.selected_submission_id:
                continue
            tasks.append(EvaluationTask(
                task_id=f"{snapshot.season_id}-eval-task-{idx}",
                season_id=snapshot.season_id,
                snapshot_hash=snapshot.compute_hash(),
                adapter_id=snapshot.adapter_id,
                adapter_version=snapshot.adapter_version,
                attempt_no=1,
                participants=[
                    Participant(
                        entry_id=entry.entry_id,
                        submission_id=entry.selected_submission_id,
                        source_hash="source-hash",
                        side="CONTESTANT"
                    )
                ],
                task_payload={
                    "problem_id": snapshot.adapter_custom_config.get("problem_id", "default-problem"),
                    "entry_id": entry.entry_id
                },
                seed=42
            ))
        return tasks
