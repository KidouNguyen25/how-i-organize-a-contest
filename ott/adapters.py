"""Trusted adapters registered by the application, never loaded from submissions."""

from __future__ import annotations

from typing import Any, Protocol

from ott import engine


class BotAdapter(Protocol):
    adapter_id: str
    version: str

    def initial_state(self) -> dict[str, Any]: ...
    def actor(self, state: dict[str, Any]) -> str: ...
    def observation(self, state: dict[str, Any], actor: str, match_id: str, clocks: dict[str, int]) -> dict[str, Any]: ...
    def parse_action(self, line: str) -> dict[str, Any]: ...
    def validate_action(self, state: dict[str, Any], action: dict[str, Any]) -> bool: ...
    def apply_action(self, state: dict[str, Any], action: dict[str, Any]) -> dict[str, str] | None: ...
    def loss(self, actor: str, reason: str) -> dict[str, str]: ...
    def public_view(self, state: dict[str, Any] | None) -> dict[str, Any]: ...


class OTTAdapter:
    adapter_id = "ott"
    version = "1"
    initial_state = staticmethod(engine.new_state)
    observation = staticmethod(engine.observation)
    encode_observation = staticmethod(engine.encode_observation)
    parse_action = staticmethod(engine.parse_action)
    validate_action = staticmethod(engine.validate_action)
    apply_action = staticmethod(engine.apply_action)

    def actor(self, state: dict[str, Any]) -> str:
        return state["side"]

    def loss(self, actor: str, reason: str) -> dict[str, str]:
        return {"result": "BLACK_WIN" if actor == "white" else "WHITE_WIN", "reason": reason}

    def public_view(self, state: dict[str, Any] | None) -> dict[str, Any]:
        revealed = bool(state and state["phase"] == "play")
        return {
            "board": engine.public_board(state) if state else None,
            "placed": engine.placed_counts(state) if state else {"white": 0, "black": 0},
            "revealed": revealed,
            "placements": [h["action"] | {"side": h["side"]} for h in state["history"] if h["phase"] == "placement"] if revealed and state else None,
            "moves": state["moves_log"] if state else [],
            "move_count": state["move_count"] if state else 0,
        }


_REGISTRY: dict[tuple[str, str], BotAdapter] = {("ott", "1"): OTTAdapter()}


def get_adapter(game_id: str = "ott", game_version: str = "1") -> BotAdapter:
    try:
        return _REGISTRY[(game_id, game_version)]
    except KeyError:
        raise ValueError("Adapter version is not approved by the application.") from None
