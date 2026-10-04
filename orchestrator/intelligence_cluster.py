from concurrent.futures import ThreadPoolExecutor
from agents.base.runtime import execute_agent
from agents.intelligence.intelligence_agents import DOMAINS, result
from core.schemas.contracts import DataSnapshot, IntelligenceBundle


class IntelligenceCluster:
    def execute(self, data: DataSnapshot, observer=None) -> IntelligenceBundle:
        bundle = IntelligenceBundle()

        def run(name):
            return name, execute_agent(
                f"intelligence.{name}",
                "INTELLIGENCE",
                lambda: DOMAINS[name](data),
                name,
                data.quality.status,
                observer=observer,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            for name, (output, record) in pool.map(
                run, [n for n in DOMAINS if n != "scenario"]
            ):
                bundle.results[name] = output or result(
                    name, "FAILED", {}, "Agent failed; see execution record"
                )
                bundle.executions.append(record)
        output, record = execute_agent(
            "intelligence.scenario",
            "INTELLIGENCE",
            lambda: DOMAINS["scenario"](data, bundle.results),
            "scenario",
            data.quality.status,
            observer=observer,
        )
        bundle.results["scenario"] = output or result(
            "scenario", "FAILED", {}, "Scenario evaluation failed"
        )
        bundle.executions.append(record)
        return bundle


intelligence_cluster = IntelligenceCluster()
