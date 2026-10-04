from concurrent.futures import ThreadPoolExecutor
from agents.base.runtime import execute_agent
from agents.decision.decision_agents import DOMAINS
from core.schemas.contracts import DecisionBundle, DecisionCandidate, RouteCandidate


class DecisionCluster:
    def execute(self, data, intelligence, request, observer=None):
        bundle = DecisionBundle()
        peers = {}

        def run(name):
            return name, execute_agent(
                f"decision.{name}",
                "DECISION",
                lambda: DOMAINS[name](data, intelligence, request),
                name,
                data.quality.status,
                observer=observer,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            for name, (output, record) in pool.map(
                run,
                [n for n in DOMAINS if n not in {"executive", "disruption_response"}],
            ):
                peers[name] = output or {
                    "status": "FAILED",
                    "missing": [f"{name.upper()}_AGENT_FAILED"],
                }
                bundle.executions.append(record)
                bundle.candidates.extend(
                    DecisionCandidate.model_validate(c)
                    for c in peers[name].get("candidates", [])
                )
                bundle.routes.extend(
                    RouteCandidate.model_validate(c)
                    for c in peers[name].get("routes", [])
                )
                bundle.missing.extend(peers[name].get("missing", []))
        output, record = execute_agent(
            "decision.disruption_response",
            "DECISION",
            lambda: DOMAINS["disruption_response"](data, intelligence, request, peers),
            "disruption-evidence",
            data.quality.status,
            observer=observer,
        )
        peers["disruption_response"] = output or {
            "status": "FAILED",
            "missing": ["DISRUPTION_AGENT_FAILED"],
        }
        bundle.executions.append(record)
        bundle.candidates.extend(
            DecisionCandidate.model_validate(c)
            for c in peers["disruption_response"].get("candidates", [])
        )
        bundle.missing.extend(peers["disruption_response"].get("missing", []))
        output, record = execute_agent(
            "decision.executive",
            "DECISION",
            lambda: DOMAINS["executive"](data, intelligence, request, peers),
            "evidence-synthesis",
            data.quality.status,
            observer=observer,
        )
        bundle.executions.append(record)
        bundle.explanation = (output or {}).get("explanation", {})
        bundle.explanation["route_generation"] = peers["route"].get("metadata", {})
        return bundle


decision_cluster = DecisionCluster()
