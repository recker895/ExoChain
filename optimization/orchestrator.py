"""Canonical optimizer entry point; legacy dictionary orchestration is retired."""
from optimization.engine import optimize
class OptimizationOrchestrator:
    def execute(self, run_id, request, data, decisions):
        return optimize(run_id, request, data, decisions)
optimization_orchestrator=OptimizationOrchestrator()
