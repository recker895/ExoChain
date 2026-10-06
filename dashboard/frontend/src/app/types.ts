export type Coordinate = { latitude: number; longitude: number };
export type Vessel = {
  mmsi: string;
  vessel_name?: string;
  position: Coordinate & {
    speed_knots?: number;
    course_over_ground?: number;
    heading?: number;
    timestamp: string;
  };
  destination?: string;
  eta?: string;
  age_seconds?: number;
  quality?: string;
  source?: string;
  weather?: Record<string, unknown>;
  ocean?: Record<string, unknown>;
  history?: (Coordinate & { timestamp?: string })[];
};
export type Agent = {
  agent_id: string;
  domain: string;
  status: string;
  latency_ms: number;
  input_quality: string;
  confidence?: number;
  source: string;
  started_at: string;
  completed_at: string;
  output: Record<string, unknown>;
  errors: string[];
};
export type Segment = {
  id: string;
  planning_label?: string | null;
  hazard_zones_crossed?: string[];
  geometry: Coordinate[];
  distance_km: number;
  duration_hours: number;
  fuel_litres: number;
  cost_usd: number;
  risk_score: number;
  weather_penalty: number;
  current_penalty: number;
  max_draft_m: number;
  open: boolean;
  source: string;
  observed_at: string;
};
export type Route = {
  planning_label?: string | null;
  weather_penalty?: number | null;
  current_penalty?: number | null;
  wave_penalty?: number | null;
  metric_evidence?: Record<string, unknown>;
  candidate_id: string;
  shipment_id: string;
  geometry: Coordinate[];
  distance_km: number;
  duration_hours: number;
  fuel_litres: number | null;
  cost_usd: number | null;
  risk_score: number | null;
  objective_value: number;
  objective_components: Record<string, number>;
  objective_percentages: Record<string, number>;
  segments: Segment[];
};
export type Component = {
  component: string;
  status: string;
  solver?: string;
  solver_version?: string;
  solve_time_ms: number;
  objective_value?: number;
  total_cost_usd?: number;
  candidate_count: number;
  selected: Record<string, unknown>[];
  alternatives: Record<string, unknown>[];
  rejected: Record<string, unknown>[];
  constraint_violations: string[];
  explanation: Record<string, unknown>;
};
export type RequestSpec = {
  operation: string;
  demo_mode?: boolean;
  use_ai_explanation?: boolean;
  shipment_ids: string[];
  vessel_reference?: {
    shipment_id: string;
    mmsi: string;
    imo?: string | null;
    vessel_name?: string | null;
  } | null;
  inventory_ids: string[];
  required_components: string[];
  budget_usd: number | null;
  max_duration_hours: number | null;
  max_risk: number;
  warehouse_capacity_units?: number | null;
  weights: Record<string, number>;
  scenarios?: Record<string, unknown> | null;
  require_human_approval: boolean;
  simulation: boolean;
  what_if: boolean;
  parent_run_id?: string | null;
};
export type Audit = {
  id: string;
  sequence?: number;
  run_id: string;
  timestamp: string;
  stage: string;
  status: string;
  actor: string;
  reason: string;
};
export type Source = {
  source: string;
  quality: string;
  observed_at?: string;
  received_at: string;
  valid_until?: string;
  errors: string[];
  latency_ms: number;
  last_known_data: boolean;
  payload?: Record<string, unknown>;
};
export type Run = {
  feedback?: Record<string, unknown>[];
  run_id: string;
  trace_id: string;
  status: string;
  request: RequestSpec;
  timestamps: Record<string, string>;
  business_inputs: Record<string, unknown>;
  data?: {
    route_context?: { routes?: Record<string, unknown>; condition_mode?: string;
      routing?: { planning_speed_knots?: number; origin_locode?: string; destination_locode?: string };
      selection?: { verified_at: string; coordinate_note: string; coordinate_source: string; vessel_note: string;
        departure?: { locode: string; name: string; lat: number; lon: number; source: string; coordinate_source?: string };
        destination?: { locode: string; name: string; lat: number; lon: number; source: string; coordinate_source?: string };
        vessel?: { name: string; imo: string; mmsi: string; source: string } } };
    business_identity?: {
      source_id: string;
      adapter: string;
      snapshot_hash: string;
      captured_at: string;
      versions: Record<string, string>;
      configured: boolean;
    };
    feedback?: Record<string, unknown>[];
    vessels: Vessel[];
    sources: Record<string, Source>;
    executions: Agent[];
    quality: { missing: string[]; stale: string[]; status: string };
  };
  intelligence?: {
    executions: Agent[];
    results: Record<
      string,
      {
        status: string;
        output: Record<string, unknown>;
        explanation: string;
        confidence?: number;
        timestamp?: string;
        model_source?: string;
        input_references?: string[];
      }
    >;
  };
  decisions?: {
    routes?: Route[];
    candidates?: Candidate[];
    executions: Agent[];
    missing: string[];
    explanation: Record<string, unknown>;
  };
  optimization?: {
    status: string;
    components: Record<string, Component>;
    total_cost_usd: number | null;
    constraint_violations: string[];
    scenario: Record<string, unknown>;
    explanation: Record<string, unknown>;
  };
  validation?: { valid: boolean; reasons: string[]; plan_hash: string };
  approval?: {
    mode?: string;
    policy_reason?: string;
    status: string;
    plan_hash: string;
    expires_at: string;
    approver?: string;
    cost_usd: number | null;
  };
  execution?: {
    status: string;
    message: string;
    simulation: boolean;
    external_reference?: string;
  };
  execution_lifecycle?: {
    stage: string;
    updated_at: string;
    requires_reconciliation: boolean;
    error?: string;
    command?: Record<string, unknown>;
    acknowledgement?: Record<string, unknown>;
    reconciliation?: Record<string, unknown>;
    history: { stage: string; timestamp: string }[];
  };
  audit: Audit[];
  errors: Record<string, unknown>[];
};
export type RunSummary = Pick<
  Run,
  "run_id" | "status" | "request" | "timestamps"
>;
export type Health = {
  status: string;
  dependencies: Record<string, string>;
  checked_at: string;
  environment?: string;
  latency_ms?: number;
};
export type Candidate = {
  candidate_id: string;
  decision_type: string;
  entity_id: string;
  action: string;
  parameters: Record<string, unknown>;
  constraints: Record<string, unknown>;
  expected_effect: Record<string, unknown>;
  confidence?: number;
  provenance: Record<string, unknown>[];
  requires_optimization: boolean;
  requires_human_approval: boolean;
};
