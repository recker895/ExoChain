"""Control tower API. Mutations require independently configured roles."""

from __future__ import annotations
import asyncio
import copy
import hashlib
import hmac
import json
import subprocess
import threading
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from config.settings import settings
from core.schemas.contracts import BusinessInputs, RunRequest, ApprovalDecision, utcnow
from core.state.run_store import RunStore
from services.providers.registry import ProviderRegistry
from services.feature_engineering.environment import join_environment
from services.health import HealthService
from services.observability.logging import configure_logging
from services.approval_service import decide, execute, audit
from orchestrator.supervisor import build_exochain_graph, initial_state
from orchestrator.data_cluster import DataCluster
from core.events.event_bus import OutboxPublisher
from services.reconciliation import reconcile
from services.providers.business import (
    configured_business_provider,
    fetch_business,
    snapshot_identity,
)
from services.providers.arcnautical_adapter import adapt_arcnautical_route, compute_arcnautical_route

logger = configure_logging()


class ArcNauticalRouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_locode: str
    destination_locode: str
    shipment_id: str
    assumed_cost_usd: float | None = None
    assumed_fuel_litres: float | None = None
    assumed_risk_score: float | None = None
    assumed_vessel_draft_m: float | None = None
    assumed_max_speed_knots: float | None = None
    assumption_source: str | None = None


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: RunRequest
    business_inputs: BusinessInputs | None = None
    arcnautical_route: ArcNauticalRouteInput | None = None


class WhatIf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: RunRequest


def principal(authorization, roles):
    token = (
        authorization[7:]
        if authorization and authorization.startswith("Bearer ")
        else ""
    )
    candidates = {
        "operator": settings.OPERATOR_API_TOKEN.get_secret_value(),
        "approver": settings.APPROVER_API_TOKEN.get_secret_value(),
    }
    if not any(candidates[r] for r in roles):
        raise HTTPException(503, "Required role is not configured")
    for role in roles:
        if candidates[role] and token and hmac.compare_digest(token, candidates[role]):
            return role + ":" + hashlib.sha256(token.encode()).hexdigest()[:12]
    raise HTTPException(
        401, "Valid role credentials required", headers={"WWW-Authenticate": "Bearer"}
    )


def operator(authorization: Annotated[str | None, Header()] = None):
    return principal(authorization, ["operator"])


def approver(authorization: Annotated[str | None, Header()] = None):
    return principal(authorization, ["approver"])


def reader(authorization: Annotated[str | None, Header()] = None):
    return principal(authorization, ["operator", "approver"])


def create_app(store=None, registry=None, cluster=None):
    store = store or RunStore(settings.RUN_DB_PATH)
    registry = registry or ProviderRegistry()
    health = HealthService(registry, store)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exochain-runs")
    graph = build_exochain_graph(store, cluster or DataCluster(registry))
    outbox = OutboxPublisher(store)
    telemetry = {"vessels": [], "status": "NOT_CHECKED", "timestamp": None}
    jobs = {}
    jobs_lock = threading.Lock()
    worker_owner = str(uuid4())

    async def workflow_watchdog():
        while True:
            try:
                await asyncio.to_thread(store.heartbeat_workflows, worker_owner)
                await asyncio.to_thread(store.recover_interrupted_workflows)
            except Exception as exc:
                logger.error("Workflow watchdog unavailable: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def background():
        while True:
            try:
                value = await asyncio.to_thread(registry.providers["ais"].fetch)
                telemetry.update(
                    {
                        "vessels": join_environment(
                            {
                                "ais": value,
                                "weather": registry.providers["weather"].cached,
                                "ocean": registry.providers["ocean"].cached,
                            }
                        ),
                        "status": value.quality.value,
                        "timestamp": value.received_at.isoformat(),
                        "errors": value.errors,
                    }
                )
                telemetry["vessels"] = [
                    {k: v for k, v in vessel.items() if k != "history"}
                    for vessel in telemetry["vessels"]
                ]
                await asyncio.to_thread(outbox.flush)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Background dependency unavailable: %s", type(exc).__name__
                )
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(background())
        watchdog = asyncio.create_task(workflow_watchdog())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(executor.shutdown, wait=True)
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(outbox.close)

    app = FastAPI(
        title=settings.PROJECT_NAME, version=settings.VERSION, lifespan=lifespan
    )
    app.state.store = store
    app.state.registry = registry
    if settings.ENVIRONMENT == "production":
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.TRUSTED_HOSTS.split(",")
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS.split(","),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def limits(request, call_next):
        if (
            settings.ENVIRONMENT == "production"
            and request.method != "OPTIONS"
            and (request.url.path.startswith("/api/") or request.url.path == "/health")
        ):
            try:
                principal(
                    request.headers.get("authorization"), ["operator", "approver"]
                )
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail}, exc.status_code, headers=exc.headers
                )
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, 400)
        if length > 20_000_000:
            return JSONResponse({"detail": "Request too large"}, 413)
        if request.method in {"POST", "PUT", "PATCH"}:
            total = 0
            chunks = []
            async for chunk in request.stream():
                total += len(chunk)
                if total > 20_000_000:
                    return JSONResponse({"detail": "Request too large"}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        safe_codes = (
            "BUSINESS_SOURCE_",
            "BUSINESS_SNAPSHOT_",
            "AUTHORITATIVE_BUSINESS_",
            "INVENTORY_VERSION_ALREADY_COMMITTED",
            "SUPPLIER_CAPACITY_ALREADY_COMMITTED",
        )
        return JSONResponse(
            {
                "detail": str(exc)
                if str(exc).startswith(safe_codes)
                else "Request violates state or authorization constraints"
            },
            409,
        )

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Run not found"}, 404)

    @app.get("/")
    async def root():
        return {
            "engine": settings.PROJECT_NAME,
            "version": settings.VERSION,
            "health": "/health",
            "docs": "/docs",
        }

    @app.get("/health")
    async def health_endpoint():
        return await asyncio.to_thread(health.check)

    @app.get("/livez")
    async def live():
        return {"status": "ALIVE"}

    @app.get("/readyz")
    async def ready(actor=Depends(reader)):
        result = await asyncio.to_thread(health.readiness)
        return JSONResponse(result, 200 if result["status"] == "READY" else 503)

    @app.get("/api/v1/operations")
    async def operations(actor=Depends(reader)):
        def inspect():
            with store.connect() as db:
                runs = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT state FROM runs WHERE json_extract(state,'$.status') IN ('FAILED','BLOCKED') ORDER BY updated_at DESC LIMIT 100"
                    )
                ]
                unresolved = [
                    dict(row)
                    for row in db.execute(
                        "SELECT idempotency_key,run_id,status FROM executions WHERE status IN ('UNKNOWN','IN_PROGRESS','FAILED') LIMIT 100"
                    )
                ]
            return {
                "failed_runs": [
                    r["run_id"]
                    for r in runs
                    if r.get("status") in {"FAILED", "BLOCKED"}
                ],
                "unresolved_executions": unresolved,
                "health": health.readiness(),
            }

        return await asyncio.to_thread(inspect)

    @app.get("/api/v1/telemetry")
    async def get_telemetry():
        return telemetry

    @app.get("/api/v1/contracts/business")
    async def contract():
        return BusinessInputs.model_json_schema()

    @app.post("/api/v1/business/validate")
    async def business_validate(payload: BusinessInputs, actor=Depends(operator)):
        from services.providers.business import production_source_errors

        errors = production_source_errors(payload)
        if errors:
            return JSONResponse({"status": "INVALID", "errors": errors}, 422)
        return {
            "status": "VALIDATED",
            "records": {
                k: len(v) if isinstance(v, list) else bool(v)
                for k, v in payload.model_dump().items()
            },
        }

    @app.get("/api/v1/business/source")
    async def business_source(actor=Depends(reader)):
        provider = configured_business_provider()
        result = await asyncio.to_thread(fetch_business, provider)
        return {
            "source": result.source,
            "quality": result.quality,
            "errors": result.errors,
            "identity": snapshot_identity(
                result.payload, result.source, provider
            ).model_dump(mode="json")
            if result.payload is not None
            else None,
        }

    @app.get("/api/v1/feedback")
    async def feedback(actor=Depends(reader)):
        return [
            f.model_dump(mode="json") for f in await asyncio.to_thread(store.feedback)
        ]

    def run_job(run_id, actor):
        def claim(db, state):
            if state["status"] != "DRAFT":
                return None, None
            state["status"] = "RUNNING"
            db.execute(
                "INSERT INTO workflow_workers VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat",
                (run_id, worker_owner, utcnow().isoformat()),
            )
            return state, audit(
                state, "workflow", "RUNNING", actor, "Workflow worker claimed draft"
            )

        state = store.transaction(run_id, claim)
        if state is None:
            return
        try:
            result = graph.invoke(state)
            logger.info(
                "Run completed: %s",
                result["status"],
                extra={"run_id": state["run_id"], "trace_id": state["trace_id"]},
            )
        except Exception as exc:
            error_kind = type(exc).__name__
            logger.exception("Workflow failed for run %s", run_id)

            def fail(db, current):
                # Never overwrite a concurrently published approval/execution.
                if current.get("approval"):
                    return None, None
                current["status"] = "FAILED"
                current["errors"].append({"stage": "workflow", "error": error_kind})
                return None, audit(
                    current,
                    "workflow",
                    "FAILED",
                    "system",
                    "Workflow failed; execution blocked",
                )

            store.transaction(run_id, fail)
        finally:
            with store.connect() as db:
                db.execute(
                    "DELETE FROM workflow_workers WHERE run_id=? AND owner=?",
                    (run_id, worker_owner),
                )

    def schedule(run_id, actor, state=None):
        with jobs_lock:
            if run_id in jobs:
                raise HTTPException(409, "Run is already scheduled")
            if len(jobs) >= 4:
                raise HTTPException(429, "Run worker capacity reached")
            if state is not None:
                event = audit(
                    state,
                    "request",
                    "DRAFT",
                    actor,
                    "Run accepted for automatic workflow execution",
                )
                store.save(state, event)
            elif store.get(run_id)["status"] != "DRAFT":
                raise HTTPException(
                    409,
                    "Only an unstarted DRAFT can be started; create a new run otherwise",
                )
            future = executor.submit(run_job, run_id, actor)
            jobs[run_id] = future

        def finished(done):
            with jobs_lock:
                jobs.pop(run_id, None)

        future.add_done_callback(finished)
        return {
            "run_id": run_id,
            "status": "DRAFT",
            "scheduled": True,
            "status_url": f"/api/v1/runs/{run_id}",
        }

    def submit(state, actor):
        return schedule(state["run_id"], actor, state)

    @app.post("/api/v1/runs", status_code=202)
    async def create_run(payload: CreateRun, actor=Depends(operator)):
        if payload.request.what_if or payload.request.parent_run_id:
            raise HTTPException(422, "Use the what-if endpoint for snapshot replay")
        if payload.request.demo_mode and settings.ENVIRONMENT == "production":
            raise HTTPException(422, "Transport demo mode is disabled in production")
        business = payload.business_inputs
        reference = payload.request.vessel_reference
        if reference is not None and business is not None:
            matching = [s for s in business.shipments if s.id == reference.shipment_id]
            if len(matching) != 1 or matching[0].vessel_id not in (None, reference.mmsi):
                raise HTTPException(422, "Vessel reference conflicts with shipment identity")
        if payload.arcnautical_route is not None:
            route = payload.arcnautical_route
            if payload.request.operation != "TRANSPORT" or set(payload.request.required_components) != {"route"} or payload.request.shipment_ids != [route.shipment_id]:
                raise HTTPException(422, "ArcNautical input requires one matching route-only transport shipment")
            if business is not None and (business.route_network is not None or len(business.shipments) != 1
                                         or business.shipments[0].id != route.shipment_id
                                         or business.shipments[0].origin_id != route.origin_locode.upper()
                                         or business.shipments[0].destination_id != route.destination_locode.upper()):
                raise HTTPException(422, "Business input must contain one matching shipment and no route network")
            try:
                computed = await run_in_threadpool(compute_arcnautical_route, route.origin_locode, route.destination_locode)
                alternatives = []
                crossed = set(computed["result"].get("hazard_zones_crossed", []))
                if {"Red Sea", "Suez Canal"} & crossed:
                    try:
                        alternatives.append(await run_in_threadpool(
                            compute_arcnautical_route, route.origin_locode,
                            route.destination_locode, via_cape=True,
                        ))
                    except (OSError, subprocess.SubprocessError, ValueError):
                        # A failed optional comparison must not erase the primary route.
                        pass
                generated = adapt_arcnautical_route(
                    computed, shipment_id=route.shipment_id,
                    alternative_payloads=alternatives,
                    cost_usd=route.assumed_cost_usd,
                    fuel_litres=route.assumed_fuel_litres,
                    risk_score=route.assumed_risk_score,
                    vessel_draft_m=route.assumed_vessel_draft_m,
                    max_speed_knots=route.assumed_max_speed_knots,
                    assumption_source=route.assumption_source,
                )
                if business is None:
                    business = generated
                else:
                    business = business.model_copy(update={"route_network": generated.route_network})
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                raise HTTPException(422, f"ArcNautical route cannot be adapted: {exc}") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise HTTPException(503, f"ArcNautical route computation unavailable: {type(exc).__name__}") from exc
        return submit(initial_state(payload.request, business), actor)

    @app.post("/api/v1/runs/{run_id}/start", status_code=202)
    async def start_run(run_id: str, actor=Depends(operator)):
        """Recover an unstarted draft; /execute is exclusively approved ERP execution."""
        return schedule(run_id, actor)

    @app.get("/api/v1/runs")
    async def runs(actor=Depends(reader)):
        return [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "request": r["request"],
                "timestamps": r["timestamps"],
            }
            for r in await asyncio.to_thread(store.list)
        ]

    @app.get("/api/v1/runs/{run_id}")
    async def run(run_id: str, actor=Depends(reader)):
        state = await asyncio.to_thread(store.get, run_id)
        state["audit"] = await asyncio.to_thread(store.events, run_id, 0, 10000)
        state["feedback"] = [
            f.model_dump(mode="json")
            for f in await asyncio.to_thread(store.feedback)
            if f.run_id == run_id
        ]
        return state

    @app.post("/api/v1/runs/{run_id}/what-if", status_code=202)
    async def what_if(run_id: str, payload: WhatIf, actor=Depends(operator)):
        parent = store.get(run_id)
        if not parent.get("data"):
            raise HTTPException(409, "Input snapshot is not available yet")
        request = payload.request.model_copy(
            update={"what_if": True, "parent_run_id": run_id, "simulation": False}
        )
        if (
            request.operation != parent["request"]["operation"]
            or request.shipment_ids != parent["request"]["shipment_ids"]
            or request.inventory_ids != parent["request"]["inventory_ids"]
        ):
            raise HTTPException(422, "What-if must preserve operation and entity scope")
        state = initial_state(
            request, BusinessInputs.model_validate(parent["business_inputs"])
        )
        state["data"] = copy.deepcopy(parent["data"])
        return submit(state, actor)

    @app.post("/api/v1/runs/{run_id}/approval")
    async def approval(run_id: str, payload: ApprovalDecision, actor=Depends(approver)):
        return await asyncio.to_thread(decide, store, run_id, payload, actor)

    @app.post("/api/v1/runs/{run_id}/execute")
    async def execution(run_id: str, actor=Depends(operator)):
        return await asyncio.to_thread(execute, store, run_id, actor)

    @app.post("/api/v1/runs/{run_id}/reconcile")
    async def reconciliation(run_id: str, actor=Depends(operator)):
        return await asyncio.to_thread(reconcile, store, run_id, actor)

    @app.get("/api/v1/events")
    async def events(
        request: Request,
        run_id: str | None = None,
        after: int = 0,
        actor=Depends(reader),
    ):
        async def stream():
            cursor = after
            heartbeat = 0
            while not await request.is_disconnected():
                events = await asyncio.to_thread(store.events, run_id, cursor)
                for event in events:
                    cursor = event["sequence"]
                    yield "id: " + str(cursor) + "\ndata: " + json.dumps(event) + "\n\n"
                heartbeat += 1
                if heartbeat % 15 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/telemetry/stream")
    async def telemetry_stream(request: Request):
        async def stream():
            last = None
            while not await request.is_disconnected():
                if telemetry["timestamp"] != last:
                    last = telemetry["timestamp"]
                    yield "data: " + json.dumps(telemetry) + "\n\n"
                await asyncio.sleep(5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("dashboard.backend.main:app", host="127.0.0.1", port=8000)
