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
        snapshot_hash: str | None = None
        slots: list[int | None] = list(range(len(entries)))
        if len(slots) % 2 != 0:
            slots.append(None)  # Add bye placeholder
        
        n = len(slots)
        tasks: list[EvaluationTask] = []

        for leg in (1, 2):
            rotation = slots[:]
            for r_idx in range(1, n):
                pairing = 0
                for i in range(n // 2):
                    a, b = rotation[i], rotation[n - 1 - i]
                    if a is None or b is None:
                        continue  # Bye round
                    pairing += 1
                    first, second = (a, b) if a < b else (b, a)
                    white_idx, black_idx = (first, second) if leg == 1 else (second, first)
                    white_id, black_id = entry_ids[white_idx], entry_ids[black_idx]

                    # Lookup selected submissions
                    white_entry = entries[white_idx]
                    black_entry = entries[black_idx]
                    if snapshot_hash is None:
                        snapshot_hash = snapshot.compute_hash()

                    tasks.append(EvaluationTask(
                        task_id=f"{snapshot.season_id}-match-L{leg}-R{r_idx}-P{pairing}",
                        season_id=snapshot.season_id,
                        snapshot_hash=snapshot_hash,
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
                rotation.insert(1, rotation.pop())
        return tasks


class DirectTaskQueueGenerator(BaseFormatGenerator):
    """Direct queue generator for test-suite algorithmic problems."""

    def generate_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        tasks: list[EvaluationTask] = []
        snapshot_hash: str | None = None
        for idx, entry in enumerate(entries, start=1):
            if not entry.selected_submission_id:
                continue
            if snapshot_hash is None:
                snapshot_hash = snapshot.compute_hash()
            tasks.append(EvaluationTask(
                task_id=f"{snapshot.season_id}-eval-task-{idx}",
                season_id=snapshot.season_id,
                snapshot_hash=snapshot_hash,
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
