from typing import Any, Dict, Optional

from core.state.state_store import StateStore


class AgentContext:

    def __init__(
        self,
        state_store: StateStore,
        telemetry: Optional[Dict[str, Any]] = None,
        intelligence: Optional[Dict[str, Any]] = None,
        decisions: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:

        self.state_store = state_store
        self.telemetry = telemetry or {}
        self.intelligence = intelligence or {}
        self.decisions = decisions or {}
        self.config = config or {}

    def get_vessel(self, mmsi: str):

        return self.state_store.get_vessel(mmsi)

    def get_active_vessels(self, limit=None):

        return self.state_store.get_active_vessels(
            limit=limit
        )

    def get_vessel_history(
        self,
        mmsi: str,
        limit: int = 12,
    ):

        return self.state_store.get_vessel_history(
            mmsi,
            limit=limit,
        )
