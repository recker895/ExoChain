import threading
import time
from concurrent.futures import ThreadPoolExecutor
from kafka import KafkaAdminClient
from config.settings import settings
from core.schemas.contracts import utcnow


class HealthService:
    def __init__(self, registry, store):
        self.registry, self.store = registry, store
        self.cached = None
        self.cached_at = 0
        self.lock = threading.Lock()

    def readiness(self):
        from services.providers.business import (
            configured_business_provider,
            fetch_business,
        )
        import requests

        result = dict(self.check())
        dependencies = dict(result["dependencies"])
        business = fetch_business(configured_business_provider())
        dependencies["business"] = business.quality.value
        if (
            settings.ERP_MODE == "http"
            and settings.ERP_URL
            and settings.ERP_TOKEN.get_secret_value()
            and settings.ERP_HEALTH_PATH.startswith("/")
            and not settings.ERP_HEALTH_PATH.startswith("//")
        ):
            try:
                response = requests.get(
                    settings.ERP_URL.rstrip("/") + settings.ERP_HEALTH_PATH,
                    headers={
                        "Authorization": "Bearer "
                        + settings.ERP_TOKEN.get_secret_value()
                    },
                    timeout=settings.PROVIDER_TIMEOUT_SECONDS,
                    allow_redirects=False,
                )
                dependencies["erp"] = (
                    "HEALTHY" if response.status_code == 200 else "UNAVAILABLE"
                )
            except requests.RequestException:
                dependencies["erp"] = "UNAVAILABLE"
        required = ("database", "optimization", "redis", "kafka", "business", "erp")
        result.update(
            dependencies=dependencies,
            business_errors=business.errors,
            status="READY"
            if all(dependencies[k] in {"HEALTHY", "VALID"} for k in required)
            else "NOT_READY",
        )
        return result

    def check(self):
        with self.lock:
            if self.cached and time.monotonic() - self.cached_at < 15:
                return self.cached
            started = time.perf_counter()
            dependencies = {}

            def redis():
                try:
                    return "HEALTHY" if self.registry.store.ping() else "UNAVAILABLE"
                except Exception:
                    return "UNAVAILABLE"

            def kafka():
                try:
                    client = KafkaAdminClient(
                        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                        request_timeout_ms=3000,
                    )
                    topics = set(client.list_topics())
                    groups = client.describe_groups(
                        ["exochain-maritime-state", "exochain-ais-history"]
                    )
                    client.close()
                    required = {
                        settings.KAFKA_TOPIC_MARITIME,
                        settings.KAFKA_TOPIC_AVIATION,
                        settings.KAFKA_TOPIC_GRAPH_EVENTS,
                        "agent-decision-logs",
                    }
                    if not required <= topics:
                        return "TOPICS_MISSING"
                    return (
                        "HEALTHY"
                        if all(
                            g.get("group_state") == "Stable"
                            and g.get("members")
                            and not g.get("error")
                            for g in groups.values()
                        )
                        and len(groups) == 2
                        else "CONSUMERS_UNAVAILABLE"
                    )
                except Exception:
                    return "UNAVAILABLE"

            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(lambda f: f(), [redis, kafka]))
            dependencies.update(dict(zip(("redis", "kafka"), values)))
            for name, provider in self.registry.providers.items():
                value = getattr(provider, "last_result", None) or provider.cached
                dependencies[name] = (
                    (
                        "STALE"
                        if value.valid_until and value.valid_until < utcnow()
                        else value.quality.value
                    )
                    if value
                    else "NOT_CHECKED"
                )
            try:
                from ortools.sat.python import cp_model

                model = cp_model.CpModel()
                flag = model.NewBoolVar("health_probe")
                model.Add(flag == 1)
                probe = cp_model.CpSolver()
                probe.parameters.max_time_in_seconds = 1
                dependencies["optimization"] = (
                    "HEALTHY"
                    if probe.Solve(model) == cp_model.OPTIMAL
                    else "UNAVAILABLE"
                )
            except Exception:
                dependencies["optimization"] = "UNAVAILABLE"
            dependencies["erp"] = (
                "SIMULATION"
                if settings.ERP_MODE == "simulation"
                else "UNAVAILABLE"
                if settings.ERP_MODE == "unavailable"
                or not settings.ERP_URL
                or not settings.ERP_TOKEN.get_secret_value()
                else "CONFIGURED_NOT_VERIFIED"
            )
            try:
                with self.store.connect() as db:
                    db.execute("SELECT run_id FROM runs LIMIT 1").fetchone()
                dependencies["database"] = "HEALTHY"
            except Exception:
                dependencies["database"] = "UNAVAILABLE"
            dependencies["business"] = (
                "CONFIGURED_NOT_VERIFIED"
                if settings.BUSINESS_DATA_PATH or settings.BUSINESS_API_URL
                else "UNAVAILABLE"
            )
            self.cached = {
                "environment": settings.ENVIRONMENT,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "status": "HEALTHY"
                if all(
                    v in {"HEALTHY", "VALID", "AVAILABLE"}
                    for v in dependencies.values()
                )
                else "DEGRADED",
                "dependencies": dependencies,
                "checked_at": utcnow().isoformat(),
            }
            self.cached_at = time.monotonic()
            return self.cached
