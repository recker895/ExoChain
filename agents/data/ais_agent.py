from typing import Any, Dict

from agents.base.agent import BaseAgent
from agents.base.context import AgentContext


class AISDataAgent(BaseAgent):

    def __init__(self) -> None:

        super().__init__(
            agent_id="data.ais",
            agent_type="AIS",
        )

    def execute(
        self,
        context: AgentContext,
    ) -> Dict[str, Any]:

        vessels = context.get_active_vessels()

        vessel_states = []

        for vessel in vessels:

            vessel_states.append(
                {
                    "mmsi": vessel.mmsi,
                    "vessel_name": vessel.vessel_name,
                    "latitude": vessel.position.latitude,
                    "longitude": vessel.position.longitude,
                    "speed_knots": vessel.position.speed_knots,
                    "course_over_ground": (
                        vessel.position.course_over_ground
                    ),
                    "heading": vessel.position.heading,
                    "timestamp": (
                        vessel.position.timestamp.isoformat()
                    ),
                }
            )

        return {
            "source": "AIS",
            "vessel_count": len(vessel_states),
            "vessels": vessel_states,
        }
