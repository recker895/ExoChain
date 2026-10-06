"""Canonical, JSON-safe contracts for the decision operating system.

Business facts are supplied, never defaulted. Defaults below are policy or empty
collections; a missing observation is not a zero-valued observation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, validate_assignment=True
    )


class Quality(str, Enum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class Provenance(Contract):
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    observed_at: AwareDatetime
    transformation: str | None = None


class Entity(Contract):
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    created_at: AwareDatetime
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    version: str = Field(min_length=1)
    data_quality: Quality
    provenance: list[Provenance] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.valid_until <= self.observed_at:
            raise ValueError("valid_until must follow observed_at")
        return self


T = TypeVar("T")


class ProviderEnvelope(Contract, Generic[T]):
    source: str
    entity_id: str
    observed_at: AwareDatetime | None = None
    received_at: AwareDatetime = Field(default_factory=utcnow)
    valid_until: AwareDatetime | None = None
    quality: Quality
    confidence: float | None = Field(default=None, ge=0, le=1)
    payload: T | None = None
    errors: list[str] = Field(default_factory=list)
    latency_ms: float = 0
    last_known_data: bool = False
    impact: str | None = None


class DataQualityReport(Contract):
    status: Quality
    missing: list[str] = Field(default_factory=list)
    stale: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Coordinate(Contract):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class SKU(Entity):
    unit: str
    description: str | None = None


class Order(Entity):
    sku_id: str
    quantity: int = Field(gt=0)
    destination_id: str
    due_at: AwareDatetime


class ShipmentRequirement(Entity):
    sku_id: str
    quantity: int = Field(gt=0)
    origin_id: str
    destination_id: str
    departure_at: AwareDatetime
    due_at: AwareDatetime
    vessel_id: str | None = None
    draft_m: float | None = Field(default=None, gt=0)
    max_speed_knots: float | None = Field(default=None, gt=0)


class Shipment(ShipmentRequirement):
    order_ids: list[str] = Field(default_factory=list)


class FuelCurvePoint(Contract):
    speed_knots: float = Field(gt=0)
    fuel_litres_per_hour: float = Field(gt=0)


class VesselProfile(Entity):
    vessel_name: str | None = None
    vessel_type: str | None = None
    deadweight_tonnes: float | None = Field(default=None, gt=0)
    draft_m: float | None = Field(default=None, gt=0)
    max_speed_knots: float | None = Field(default=None, gt=0)
    engine_power_kw: float | None = Field(default=None, gt=0)
    fuel_type: str | None = None
    fuel_consumption_curve: list[FuelCurvePoint] = Field(default_factory=list)
    fuel_curve_classification: Literal["AUTHORITATIVE", "MODEL_DERIVED"] | None = None
    max_significant_wave_height_m: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def curve_provenance(self):
        speeds = [point.speed_knots for point in self.fuel_consumption_curve]
        if len(speeds) != len(set(speeds)):
            raise ValueError("duplicate speed in fuel curve")
        if self.fuel_consumption_curve and self.fuel_curve_classification is None:
            raise ValueError("fuel curve classification required")
        return self


class InventoryPosition(Entity):
    sku_id: str
    location_id: str
    current_inventory_units: int = Field(ge=0)
    expected_demand_units: int = Field(ge=0)
    demand_reference: str = Field(min_length=1)
    horizon_days: float = Field(gt=0)
    lead_time_days: float = Field(ge=0)
    safety_stock_units: int = Field(ge=0)
    service_level: float = Field(gt=0, le=1)
    max_stock_units: int = Field(ge=0)
    unit_cost_usd: float = Field(gt=0)
    holding_cost_per_unit_usd: float = Field(ge=0)
    ordering_cost_usd: float = Field(ge=0)
    shortage_penalty_usd: float = Field(gt=0)


class Supplier(Entity):
    name: str
    location_id: str
    approved: bool
    quality_score: float = Field(ge=0, le=1)
    reliability: float = Field(ge=0, le=1)


class SupplierQuote(Entity):
    supplier_id: str
    sku_id: str
    location_id: str
    capacity_units: int = Field(gt=0)
    unit_cost_usd: float = Field(gt=0)
    lead_time_days: float = Field(ge=0)
    risk_score: float = Field(ge=0, le=1)


class Carrier(Entity):
    name: str
    modes: list[Literal["SEA", "AIR", "ROAD", "RAIL", "MULTIMODAL"]]


class ModalCandidate(Entity):
    shipment_id: str
    carrier_id: str
    mode: Literal["SEA", "AIR", "ROAD", "RAIL", "MULTIMODAL"]
    capacity_units: int = Field(gt=0)
    cost_per_unit_usd: float = Field(gt=0)
    duration_hours: float = Field(gt=0)
    risk_score: float = Field(ge=0, le=1)


class DemandObservation(Entity):
    sku_id: str
    location_id: str
    quantity: int = Field(ge=0)
    period_start: AwareDatetime
    period_end: AwareDatetime


class Disruption(Entity):
    event_type: str
    severity: float = Field(ge=0, le=1)
    affected_entities: list[str] = Field(min_length=1)
    explanation: str
    supply_effect: Literal["UNAVAILABLE"] | None = None
    supplier_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(default_factory=list)
    quote_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def actionable_scope(self):
        if self.supply_effect and not (
            self.supplier_ids or self.location_ids or self.quote_ids
        ):
            raise ValueError(
                "Supply effects require explicit supplier, location or quote identities"
            )
        return self


class Port(Entity):
    name: str
    position: Coordinate | None = None
    activity: dict[str, float | None] = Field(default_factory=dict)
    physical_waiting_hours: float | None = Field(default=None, ge=0)


class PortQueueObservation(Entity):
    port_locode: str = Field(min_length=5, max_length=5)
    queued_vessels: int | None = Field(default=None, ge=0)
    observed_waiting_hours: float | None = Field(default=None, ge=0)


class NavigationNode(Contract):
    id: str
    position: Coordinate


class NavigationEdge(Entity):
    planning_label: str | None = None
    origin_id: str
    destination_id: str
    geometry: list[Coordinate] = Field(min_length=2)
    distance_km: float = Field(gt=0)
    duration_hours: float = Field(gt=0)
    cost_usd: float | None = Field(default=None, gt=0)
    fuel_litres: float | None = Field(default=None, gt=0)
    risk_score: float | None = Field(default=None, ge=0, le=1)
    weather_penalty: float | None = Field(default=None, ge=0)
    current_penalty: float | None = Field(default=None, ge=0)
    max_draft_m: float | None = Field(default=None, gt=0)
    max_speed_knots: float | None = Field(default=None, gt=0)
    open: bool
    allowed_vessel_ids: list[str] = Field(default_factory=list)
    environmental_references: list[str] = Field(default_factory=list)
    hazard_zones_crossed: list[str] = Field(default_factory=list)
    available_depth_m: float | None = Field(default=None, gt=0)
    depth_evidence_reference: str | None = None
    restrictions_verified: bool = False


class NavigationNetwork(Entity):
    navigational_authority: str = Field(min_length=1)
    planning_classification: Literal["GEOGRAPHIC_PLANNING_ONLY", "NAVIGATION_AUTHORITY"] = "NAVIGATION_AUTHORITY"
    hazard_zones_crossed: list[str] = Field(default_factory=list)
    nodes: list[NavigationNode] = Field(min_length=2)
    edges: list[NavigationEdge] = Field(min_length=1)

    @model_validator(mode="after")
    def topology(self):
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes) or len({e.id for e in self.edges}) != len(
            self.edges
        ):
            raise ValueError("duplicate navigation identity")
        for edge in self.edges:
            if edge.origin_id not in nodes or edge.destination_id not in nodes:
                raise ValueError("unknown edge endpoint")
            if (
                edge.geometry[0] != nodes[edge.origin_id].position
                or edge.geometry[-1] != nodes[edge.destination_id].position
            ):
                raise ValueError("edge geometry must terminate at its network nodes")
        return self


class BusinessInputs(Contract):
    orders: list[Order] = Field(default_factory=list)
    shipments: list[Shipment] = Field(default_factory=list)
    vessel_profiles: list[VesselProfile] = Field(default_factory=list)
    port_queues: list[PortQueueObservation] = Field(default_factory=list)
    skus: list[SKU] = Field(default_factory=list)
    demand: list[DemandObservation] = Field(default_factory=list)
    inventory: list[InventoryPosition] = Field(default_factory=list)
    suppliers: list[Supplier] = Field(default_factory=list)
    supplier_quotes: list[SupplierQuote] = Field(default_factory=list)
    carriers: list[Carrier] = Field(default_factory=list)
    carrier_options: list[ModalCandidate] = Field(default_factory=list)
    disruptions: list[Disruption] = Field(default_factory=list)
    route_network: NavigationNetwork | None = None

    @model_validator(mode="after")
    def identities(self):
        for field in type(self).model_fields:
            records = getattr(self, field)
            if isinstance(records, list) and len({r.id for r in records}) != len(
                records
            ):
                raise ValueError(f"duplicate id in {field}")
        return self


class ObjectiveWeights(Contract):
    distance: float = Field(default=0, ge=0)
    cost: float = Field(default=1, ge=0)
    time: float = Field(default=1, ge=0)
    fuel: float = Field(default=1, ge=0)
    risk: float = Field(default=1, ge=0)
    weather: float = Field(default=1, ge=0)
    current: float = Field(default=1, ge=0)
    wave: float = Field(default=0, ge=0)
    port: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def nonzero(self):
        if not any(self.model_dump().values()):
            raise ValueError("at least one objective weight must be positive")
        return self


class ScenarioAssumptions(Contract):
    source: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    iterations: int = Field(default=5000, ge=100, le=100000)
    seed: int = 42
    cost_std_fraction: float = Field(ge=0, le=2)
    duration_std_fraction: float = Field(ge=0, le=2)
    disruption_probability: float = Field(ge=0, le=1)
    disruption_cost_multiplier: float = Field(ge=1)
    disruption_time_multiplier: float = Field(ge=1)


class VesselReference(Contract):
    """Operator-selected vessel identity; not proof of a current AIS observation."""

    shipment_id: str = Field(min_length=1)
    mmsi: str = Field(pattern=r"^[0-9]{9}$")
    imo: str | None = Field(default=None, pattern=r"^[0-9]{7}$")
    vessel_name: str | None = None


class RunRequest(Contract):
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    operation: Literal["TRANSPORT", "REPLENISHMENT", "ASSESSMENT"] = "ASSESSMENT"
    shipment_ids: list[str] = Field(default_factory=list)
    vessel_reference: VesselReference | None = None
    demo_mode: bool = False
    use_ai_explanation: bool = False
    inventory_ids: list[str] = Field(default_factory=list)
    required_components: list[Literal["route", "modal", "inventory", "procurement"]] = (
        Field(default_factory=list)
    )
    budget_usd: float | None = Field(default=None, gt=0)
    max_duration_hours: float | None = Field(default=None, gt=0)
    max_risk: float = Field(default=0.5, ge=0, le=1)
    warehouse_capacity_units: int | None = Field(default=None, gt=0)
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    scenarios: ScenarioAssumptions | None = None
    require_human_approval: bool = True
    simulation: bool = False
    what_if: bool = False
    parent_run_id: str | None = None

    @model_validator(mode="after")
    def vessel_scope(self):
        if self.vessel_reference and (
            self.operation != "TRANSPORT"
            or self.shipment_ids != [self.vessel_reference.shipment_id]
        ):
            raise ValueError("Vessel reference requires one matching transport shipment")
        return self


class DecisionCandidate(Contract):
    candidate_id: str
    decision_type: str
    entity_id: str
    action: str
    parameters: dict[str, Any]
    constraints: dict[str, Any]
    expected_effect: dict[str, Any]
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: list[Provenance]
    requires_optimization: bool = True
    requires_human_approval: bool = True


class RouteCandidate(Contract):
    candidate_id: str
    shipment_id: str
    network_id: str
    edge_ids: list[str]
    geometry: list[Coordinate]
    distance_km: float
    duration_hours: float
    cost_usd: float | None = None
    fuel_litres: float | None = None
    risk_score: float | None = None
    weather_penalty: float | None = None
    current_penalty: float | None = None
    wave_penalty: float | None = None
    port_penalty: float | None = None
    planning_label: str | None = None
    metric_evidence: dict[str, Any] = Field(default_factory=dict)
    provenance: list[Provenance]
    segments: list[NavigationEdge]
    voyage_assessment: dict[str, Any] = Field(default_factory=dict)


class IntelligenceResult(Contract):
    agent: str
    status: str
    input_references: list[str] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)
    model_source: str
    model_version: str = "1"
    timestamp: AwareDatetime = Field(default_factory=utcnow)
    data_quality: Quality
    explanation: str
    provenance: list[Provenance] = Field(default_factory=list)


class RiskAssessment(Contract):
    entity_id: str
    score: float | None = Field(default=None, ge=0, le=1)
    components: dict[str, float | None]
    missing_inputs: list[str]
    method: str


class DemandForecast(Contract):
    sku_id: str
    location_id: str
    prediction: float = Field(ge=0)
    horizon_days: float = Field(gt=0)
    method: str
    input_references: list[str]


class AgentExecution(Contract):
    agent_id: str
    domain: Literal["DATA", "INTELLIGENCE", "DECISION"]
    status: str
    started_at: AwareDatetime
    completed_at: AwareDatetime
    latency_ms: float
    input_quality: Quality
    confidence: float | None = None
    source: str
    output: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class OptimizationRequest(Contract):
    run_id: str
    request: RunRequest
    business_inputs: BusinessInputs
    candidates: list[DecisionCandidate]


class ComponentSolution(Contract):
    component: str
    status: str
    solver: str | None = None
    solver_version: str | None = None
    solve_time_ms: float = 0
    objective_value: float | None = None
    total_cost_usd: float | None = None
    candidate_count: int = 0
    selected: list[dict[str, Any]] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    constraint_violations: list[str] = Field(default_factory=list)
    explanation: dict[str, Any] = Field(default_factory=dict)


class OptimizationSolution(Contract):
    plan_id: str = Field(default_factory=lambda: str(uuid4()))
    plan_version: int = 1
    run_id: str
    status: str
    components: dict[str, ComponentSolution] = Field(default_factory=dict)
    total_cost_usd: float | None = None
    selected_actions: list[dict[str, Any]] = Field(default_factory=list)
    constraint_violations: list[str] = Field(default_factory=list)
    scenario: dict[str, Any] = Field(default_factory=dict)
    source_references: list[str] = Field(default_factory=list)
    explanation: dict[str, Any] = Field(default_factory=dict)


POLICY_VERSION = "2"


class ValidationResult(Contract):
    valid: bool
    status: str
    reasons: list[str]
    checked_at: AwareDatetime = Field(default_factory=utcnow)
    plan_hash: str
    policy_version: str = POLICY_VERSION


class ApprovalRequest(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    cost_usd: float | None = None
    selected_actions: list[dict[str, Any]]
    status: Literal[
        "DRAFT",
        "VALIDATING",
        "PENDING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "EXPIRED",
        "EXECUTED",
        "FAILED",
    ]
    created_at: AwareDatetime = Field(default_factory=utcnow)
    expires_at: AwareDatetime
    approver: str | None = None
    decided_at: AwareDatetime | None = None
    mode: Literal["HUMAN", "POLICY"] = "HUMAN"
    policy_reason: str | None = None


class ApprovalDecision(Contract):
    plan_hash: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=2000)


class PurchaseOrderAction(Contract):
    type: Literal["PURCHASE_ORDER"] = "PURCHASE_ORDER"
    quote_id: str = Field(min_length=1)
    supplier_id: str = Field(min_length=1)
    sku_id: str = Field(min_length=1)
    location_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit_cost_usd: float = Field(gt=0)
    total_cost_usd: float = Field(gt=0)
    lead_time_days: float = Field(ge=0)
    quote_version: str = Field(min_length=1)


class ExecutionCommand(Contract):
    run_id: str
    plan_id: str
    plan_hash: str
    idempotency_key: str
    approval_id: str
    actions: list[dict[str, Any]]
    total_cost_usd: float = Field(gt=0)
    request_id: str | None = None
    business_snapshot_hash: str | None = None
    source_versions: dict[str, str] = Field(default_factory=dict)
    purchase_cost_usd: float | None = Field(default=None, ge=0)
    ordering_cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def purchase_order_contract(self):
        if not self.actions:
            raise ValueError("Purchase-order lines required")
        for action in self.actions:
            PurchaseOrderAction.model_validate(action)
        return self


class ExecutionResult(Contract):
    status: Literal[
        "EXECUTED",
        "EXECUTION_SIMULATED",
        "ERP_UNAVAILABLE",
        "FAILED",
        "REJECTED",
        "IN_PROGRESS",
        "UNKNOWN",
    ]
    idempotency_key: str
    external_reference: str | None = None
    simulation: bool = False
    message: str
    timestamp: AwareDatetime = Field(default_factory=utcnow)
    acknowledged_cost_usd: float | None = Field(default=None, ge=0)
    actual_lead_time_days: float | None = Field(default=None, ge=0)
    response_metadata: dict[str, str | int] = Field(default_factory=dict)
    plan_hash: str | None = None
    acknowledged_actions: list[dict[str, Any]] | None = None


class BusinessSnapshotIdentity(Contract):
    source_id: str
    adapter: str
    snapshot_hash: str
    captured_at: AwareDatetime = Field(default_factory=utcnow)
    versions: dict[str, str]
    configured: bool


class ReplenishmentRestrictions(Contract):
    supplier_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(default_factory=list)
    quote_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class ExecutionLifecycle(Contract):
    stage: Literal[
        "PLAN_CREATED",
        "EXECUTION_SIMULATED",
        "PENDING_APPROVAL",
        "APPROVED",
        "EXECUTION_IN_PROGRESS",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "REJECTED",
        "FAILED",
        "UNKNOWN",
        "REQUIRES_RECONCILIATION",
        "RECONCILED",
    ]
    updated_at: AwareDatetime = Field(default_factory=utcnow)
    command: ExecutionCommand | None = None
    acknowledgement: ExecutionResult | None = None
    reconciliation: ExecutionResult | None = None
    error: str | None = None
    requires_reconciliation: bool = False
    history: list[dict[str, str]] = Field(default_factory=list)


class ExecutionFeedback(Contract):
    id: str
    run_id: str
    plan_hash: str
    idempotency_key: str
    erp_po_id: str
    quote_id: str
    supplier_id: str
    sku_id: str
    location_id: str
    ordered_quantity: int = Field(gt=0)
    planned_cost_usd: float = Field(gt=0)
    acknowledged_order_cost_usd: float | None = Field(default=None, ge=0)
    planned_lead_time_days: float = Field(ge=0)
    actual_order_lead_time_days: float | None = Field(default=None, ge=0)
    status: Literal["ACKNOWLEDGED", "RECONCILED"]
    observed_at: AwareDatetime
    provenance: list[Provenance] = Field(min_length=1)


class AuditEvent(Contract):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    trace_id: str
    timestamp: AwareDatetime = Field(default_factory=utcnow)
    stage: str
    status: str
    actor: str
    reason: str
    references: list[str] = Field(default_factory=list)


class ReservationSnapshot(Contract):
    inventory_ids: list[str] = Field(default_factory=list)
    quote_units: dict[str, int] = Field(default_factory=dict)
    captured_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def nonnegative(self):
        if any(v < 0 for v in self.quote_units.values()):
            raise ValueError("Reserved units cannot be negative")
        return self


class DataSnapshot(Contract):
    sources: dict[str, ProviderEnvelope] = Field(default_factory=dict)
    vessels: list[dict[str, Any]] = Field(default_factory=list)
    route_context: dict[str, Any] = Field(default_factory=dict)
    business: BusinessInputs = Field(default_factory=BusinessInputs)
    quality: DataQualityReport
    executions: list[AgentExecution] = Field(default_factory=list)
    business_identity: BusinessSnapshotIdentity | None = None
    feedback: list[ExecutionFeedback] = Field(default_factory=list)
    reservations: ReservationSnapshot = Field(default_factory=ReservationSnapshot)


class IntelligenceBundle(Contract):
    results: dict[str, IntelligenceResult] = Field(default_factory=dict)
    executions: list[AgentExecution] = Field(default_factory=list)


class DecisionBundle(Contract):
    candidates: list[DecisionCandidate] = Field(default_factory=list)
    routes: list[RouteCandidate] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    explanation: dict[str, Any] = Field(default_factory=dict)
    executions: list[AgentExecution] = Field(default_factory=list)
