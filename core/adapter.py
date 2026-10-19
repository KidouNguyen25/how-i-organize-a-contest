"""A05: Adapter Contract, Registry, and Conformance Testing Suite."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from core.models import Entry, EvaluationResult, EvaluationTask, SeasonSnapshot


class AdapterError(Exception):
    """Raised when an adapter encounters an unrecoverable contract violation."""
    pass


class BaseEvaluationAdapter(ABC):
    """Core Contract that all evaluation adapters must implement."""

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        pass

    @property
    @abstractmethod
    def adapter_version(self) -> str:
        pass

    @abstractmethod
    def validate_season_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Validate specialized problem or game configuration. Returns (is_valid, error_msg)."""
        pass

    @abstractmethod
    def prepare_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        """Generate evaluation tasks according to season snapshot and format."""
        pass

    @abstractmethod
    def parse_raw_output(self, task: EvaluationTask, raw_worker_output: dict[str, Any]) -> EvaluationResult:
        """Parse raw output from Docker/sandbox worker into structured EvaluationResult."""
        pass

    @abstractmethod
    def filter_public_view(self, detailed_state: dict[str, Any], audience_role: str) -> dict[str, Any]:
        """Filter out confidential data (secret test cases or placement data) before sending to client."""
        pass


class AdapterRegistry:
    """Central repository of certified evaluation adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, BaseEvaluationAdapter] = {}

    def register(self, adapter: BaseEvaluationAdapter) -> None:
        key = f"{adapter.adapter_id}:{adapter.adapter_version}"
        self._adapters[key] = adapter

    def get(self, adapter_id: str, version: str) -> BaseEvaluationAdapter:
        key = f"{adapter_id}:{version}"
        if key not in self._adapters:
            raise KeyError(f"Adapter '{key}' is not registered in the system.")
        return self._adapters[key]

    def list_adapters(self) -> list[dict[str, str]]:
        return [
            {"adapter_id": a.adapter_id, "adapter_version": a.adapter_version}
            for a in self._adapters.values()
        ]


class AdapterConformanceSuite:
    """Automated conformance test runner verifying that an adapter adheres to platform contracts."""

    @staticmethod
    def verify(adapter: BaseEvaluationAdapter) -> bool:
        # 1. Verify identification
        if not adapter.adapter_id or not adapter.adapter_version:
            raise AdapterError("Adapter missing adapter_id or adapter_version.")

        # 2. Verify config validation resilience (should not crash on invalid input)
        valid, msg = adapter.validate_season_config({"invalid_key": "some_garbage"})
        if valid:
            # If an empty garbage config is reported as valid, warn or verify
            pass

        # 3. Verify public view filter on confidential fields
        test_state = {
            "public_metric": 42,
            "secret_test_data": "SUPER_SECRET_TOKEN",
            "pieces": [{"side": "WHITE", "x": 0, "y": 0}, {"side": "BLACK", "x": 8, "y": 8}]
        }
        filtered = adapter.filter_public_view(test_state, audience_role="SPECTATOR")
        if "secret_test_data" in filtered:
            raise AdapterError("Conformance failed: filter_public_view leaked 'secret_test_data' to SPECTATOR.")

        return True
