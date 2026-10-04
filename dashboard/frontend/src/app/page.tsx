"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import {
  Activity,
  Anchor,
  ArrowRight,
  Check,
  Clock,
  Database,
  Download,
  ExternalLink,
  FileCheck,
  Globe2,
  Layers3,
  LockKeyhole,
  MapPin,
  Play,
  Radio,
  RefreshCw,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  TriangleAlert,
  X,
} from "lucide-react";
import {
  DecisionBrief,
  IntelligenceView,
  AgentsView,
  DecisionsView,
  ScenarioView,
  SystemView,
  RouteComparison,
} from "./Workspaces";
import { api, subscribe, API } from "./api";
import Replenishment from "./Replenishment";
import type {
  Agent,
  Audit,
  Health,
  RequestSpec,
  Route,
  Run,
  RunSummary,
  Segment,
  Vessel,
} from "./types";
const OperationsMap = dynamic(() => import("./OperationsMap"), {
  ssr: false,
  loading: () => (
    <div className="map-loading">Loading geographic workspace</div>
  ),
});
const stages = [
  "data",
  "intelligence",
  "decisions",
  "optimization",
  "validation",
  "approval",
  "execution",
];
const expectedAgents = {
  DATA: ["maritime", "aviation", "environment", "ports", "market", "business"],
  INTELLIGENCE: [
    "risk",
    "demand",
    "forecast",
    "disruption",
    "anomaly",
    "port",
    "scenario",
  ],
  DECISION: [
    "route",
    "modal",
    "inventory",
    "supplier",
    "disruption_response",
    "executive",
  ],
};
const money = (n: number | null | undefined) =>
  n == null
    ? "Unavailable"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 0,
      }).format(n);
const number = (n: number | null | undefined, digits = 1) =>
  n == null
    ? "Unavailable"
    : n.toLocaleString("en-US", { maximumFractionDigits: digits });
const time = (value: string | undefined) =>
  value ? new Date(value).toLocaleString() : "Not observed";
const tone = (status: string | undefined) =>
  /^(SUCCESS|VALID|HEALTHY|AVAILABLE|OPTIMAL|FEASIBLE|APPROVED|EXECUTED)$/.test(
    status || "",
  )
    ? "good"
    : /FAILED|REJECTED|INVALID|INFEASIBLE/.test(status || "")
      ? "bad"
      : /PENDING|PARTIAL|STALE|BLOCKED|UNAVAILABLE|REQUIRED|EXPIRED/.test(
            status || "",
          )
        ? "warn"
        : "neutral";
function Badge({ status }: { status?: string }) {
  return (
    <span className={`badge ${tone(status)}`}>
      <i />
      {(status || "NOT RUN").replaceAll("_", " ")}
    </span>
  );
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Layers3 size={22} />
      </div>
      <strong>{title}</strong>
      <p>{children}</p>
    </div>
  );
}
function Metric({
  label,
  value,
  unit,
}: {
  label: string;
  value: string;
  unit?: string;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>
        {value}
        <small>{unit}</small>
      </strong>
    </div>
  );
}
const defaultRequest: RequestSpec = {
  operation: "ASSESSMENT",
  demo_mode: false,
  shipment_ids: [],
  inventory_ids: [],
  required_components: [],
  budget_usd: null,
  max_duration_hours: null,
  max_risk: 0.5,
  weights: { cost: 1, time: 1, fuel: 1, risk: 1, weather: 1, current: 1 },
  require_human_approval: true,
  simulation: false,
  what_if: false,
};

export default function ControlTower() {
  const [token, setToken] = useState("");
  const [approvalToken, setApprovalToken] = useState("");
  const [authOpen, setAuthOpen] = useState(false);
  const [health, setHealth] = useState<Health>();
  const [vessels, setVessels] = useState<Vessel[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [run, setRun] = useState<Run>();
  const [selectedId, setSelectedId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [telemetryAt, setTelemetryAt] = useState<string>();
  const [telemetryStatus, setTelemetryStatus] = useState("UNAVAILABLE");
  const [now, setNow] = useState<Date>();
  useEffect(() => {
    const tick = () => setNow(new Date());
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, []);
  const [view, setView] = useState("Command Center");
  const [drawer, setDrawer] = useState(false);
  const [agent, setAgent] = useState<Agent>();
  const [vessel, setVessel] = useState<Vessel>();
  const [segment, setSegment] = useState<Segment>();
  const [routeIndex, setRouteIndex] = useState(0);
  const [liveMap, setLiveMap] = useState(true);
  const [request, setRequest] = useState<RequestSpec>(defaultRequest);
  const [businessText, setBusinessText] = useState("");
  const [originLocode, setOriginLocode] = useState("SGSIN");
  const [destinationLocode, setDestinationLocode] = useState("NLRTM");
  const [whatIf, setWhatIf] = useState(false);
  const [approvalReason, setApprovalReason] = useState("");
  const refreshRuns = useCallback(async () => {
    if (!token && !approvalToken) return;
    try {
      const items = await api<RunSummary[]>(
        "/api/v1/runs",
        token || approvalToken,
      );
      setRuns(items);
      setSelectedId((current) => current || items[0]?.run_id || "");
    } catch (e) {
      setError((e as Error).message);
    }
  }, [token, approvalToken]);
  const refreshRun = useCallback(async () => {
    if (!selectedId || (!token && !approvalToken)) return;
    try {
      setRun(
        await api<Run>(`/api/v1/runs/${selectedId}`, token || approvalToken),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selectedId, token, approvalToken]);
  useEffect(() => {
    let active = true;
    const fetchHealth = () =>
      api<Health>("/health", token || approvalToken)
        .then((h) => {
          if (active) setHealth(h);
        })
        .catch(() => {
          if (active) setHealth(undefined);
        });
    fetchHealth();
    const timer = setInterval(fetchHealth, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [token, approvalToken]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delay = 1000;
    const connect = () =>
      subscribe(
        "/api/v1/telemetry/stream",
        token || approvalToken,
        controller.signal,
        (event) => {
          const packet = event as {
            vessels: Vessel[];
            timestamp?: string;
            status?: string;
          };
          setVessels(packet.vessels || []);
          setTelemetryAt(packet.timestamp);
          setTelemetryStatus(packet.status || "UNAVAILABLE");
          setConnected(true);
          delay = 1000;
        },
      )
        .then(() => {
          if (!controller.signal.aborted) {
            setConnected(false);
            timer = setTimeout(connect, delay);
          }
        })
        .catch(() => {
          if (!controller.signal.aborted) {
            setConnected(false);
            timer = setTimeout(connect, delay);
            delay = Math.min(delay * 2, 30000);
          }
        });
    api<{ vessels: Vessel[] }>("/api/v1/telemetry", token || approvalToken)
      .then((r) => setVessels(r.vessels))
      .catch(() => {});
    connect();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [token, approvalToken]);
  useEffect(() => {
    if (!token && !approvalToken) return;
    const controller = new AbortController();
    fetch(API + "/api/v1/runs", {
      headers: { Authorization: `Bearer ${token || approvalToken}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok)
          throw new Error(
            `Run access failed (${response.status}). Check access credentials.`,
          );
        const items: RunSummary[] = await response.json();
        setRuns(items);
        setSelectedId((current) => current || items[0]?.run_id || "");
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [token, approvalToken]);
  useEffect(() => {
    if (!selectedId || (!token && !approvalToken)) return;
    const controller = new AbortController();
    fetch(API + `/api/v1/runs/${selectedId}`, {
      headers: { Authorization: `Bearer ${token || approvalToken}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok)
          throw new Error(`Run unavailable (${response.status})`);
        setRun(await response.json());
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [selectedId, token, approvalToken]);
  useEffect(() => {
    if (!token && !approvalToken) return;
    const controller = new AbortController();
    let refreshTimer: ReturnType<typeof setTimeout>;
    let reconnect: ReturnType<typeof setTimeout>;
    let cursor = 0;
    const connect = () =>
      subscribe(
        `/api/v1/events?after=${cursor}${selectedId ? `&run_id=${selectedId}` : ""}`,
        token || approvalToken,
        controller.signal,
        (event) => {
          const audit = event as Audit;
          cursor = audit.sequence || cursor;
          // Agent start/finish events are numerous. Their durable cluster
          // snapshot is published by the stage event; reloading a multi-MB
          // run for every agent event can overwhelm the browser tab.
          if (
            ![
              "workflow",
              "data",
              "intelligence",
              "decisions",
              "optimization",
              "validation",
              "approval",
              "execution",
              "reconciliation",
            ].includes(audit.stage) ||
            audit.status === "RUNNING" && audit.stage !== "workflow"
          ) return;
          clearTimeout(refreshTimer);
          refreshTimer = setTimeout(() => {
            refreshRuns();
            refreshRun();
          }, 200);
        },
      )
        .then(() => {
          if (!controller.signal.aborted) reconnect = setTimeout(connect, 5000);
        })
        .catch(() => {
          if (!controller.signal.aborted) reconnect = setTimeout(connect, 5000);
        });
    connect();
    return () => {
      controller.abort();
      clearTimeout(refreshTimer);
      clearTimeout(reconnect);
    };
  }, [token, approvalToken, selectedId, refreshRun, refreshRuns]);
  const agents = useMemo(
    () => [
      ...(run?.data?.executions || []),
      ...(run?.intelligence?.executions || []),
      ...(run?.decisions?.executions || []),
    ],
    [run],
  );
  const counts = {
    successful: agents.filter((a) => a.status === "SUCCESS").length,
    unavailable: agents.filter((a) =>
      /UNAVAILABLE|REQUIRED|STALE/.test(a.status),
    ).length,
    partial: agents.filter((a) => a.status === "PARTIAL").length,
    errors: agents.filter((a) => a.status === "FAILED").length,
  };
  const components = run?.optimization?.components || {};
  const routeComponent = components.route;
  const recommended = useMemo(
    () =>
      new Set(
        (routeComponent?.selected || []).map((r) => String(r.candidate_id)),
      ),
    [routeComponent],
  );
  const routes = useMemo(
    () =>
      [
        ...(routeComponent?.selected || []),
        ...(routeComponent?.alternatives || []),
      ].filter((r) => Array.isArray(r.geometry)) as unknown as Route[],
    [routeComponent],
  );
  const routeNetwork = run?.business_inputs?.route_network as
    | { planning_classification?: string; source?: string; hazard_zones_crossed?: string[]; edges?: Segment[] }
    | undefined;
  const planningOnly = run?.request.operation === "TRANSPORT" && !run?.request.demo_mode &&
    routeNetwork?.planning_classification === "GEOGRAPHIC_PLANNING_ONLY";
  const demoGeographic = run?.request.demo_mode &&
    routeNetwork?.planning_classification === "GEOGRAPHIC_PLANNING_ONLY";
  const planningEdges = useMemo(() => planningOnly
    ? [...(routeNetwork?.edges || [])].sort((a, b) =>
        a.duration_hours - b.duration_hours || a.distance_km - b.distance_km)
    : [], [planningOnly, routeNetwork]);
  const planningEdge = planningEdges[routeIndex] || planningEdges[0];
  const mapRecommended = useMemo(() => planningEdges.length && !routes.length
    ? new Set([`geographic-planning-preview:${planningEdges[0].id}`])
    : recommended, [planningEdges, routes.length, recommended]);
  const mapRoutes = useMemo(() => {
    if (routes.length || !routeNetwork?.edges?.length) return routes;
    return planningEdges.map((edge) => ({
      candidate_id: `geographic-planning-preview:${edge.id}`,
      shipment_id: run?.request.shipment_ids?.[0] || "Planning preview",
      geometry: edge.geometry,
      distance_km: edge.distance_km,
      duration_hours: edge.duration_hours,
      segments: [edge],
    } as Route));
  }, [routes, routeNetwork, planningEdges, run?.request.shipment_ids]);
  const routeGeneration = (run?.decisions?.explanation?.route_generation || {}) as Record<
    string,
    { voyage_assessment?: {
      summary?: Record<string, string>;
      checks_at?: string;
      checks?: Record<string, { execution?: string; lookup?: string; reason?: string | null }>;
      fuel?: { total_fuel_litres?: number };
      vessel_state?: { status?: string; observed_at?: string; identity_checks?: Record<string, string> };
      draft?: { reason?: string };
      samples?: { weather?: { status?: string }; waves?: { status?: string }; current?: { status?: string } }[];
    } }
  >;
  const previewAssessment = Object.values(routeGeneration).find(
    (item) => item.voyage_assessment,
  )?.voyage_assessment;
  const selectedRoute = routes[routeIndex] || routes[0];
  const voyageAssessment = (selectedRoute as Route & { voyage_assessment?: typeof previewAssessment })?.voyage_assessment || previewAssessment;
  const ports = (run?.intelligence?.results?.port?.output?.ports ||
    []) as Record<string, unknown>[];
  const mapVessels = !liveMap && run?.data ? run.data.vessels || [] : vessels;
  const reasons = run?.validation?.reasons || [];
  async function startRun() {
    setBusy(true);
    setError("");
    try {
      const input = {
        ...request,
        vessel_reference:
          request.operation === "TRANSPORT" && request.vessel_reference?.mmsi
            ? { ...request.vessel_reference, shipment_id: request.shipment_ids[0] || "" }
            : null,
        inventory_ids:
          request.operation === "REPLENISHMENT"
            ? request.inventory_ids.flatMap((value) =>
                value
                  .split(",")
                  .map((id) => id.trim())
                  .filter(Boolean),
              )
            : request.inventory_ids,
        what_if: whatIf,
        parent_run_id: whatIf ? selectedId : null,
      };
      const body = whatIf
        ? { request: input }
        : {
            request: input,
            ...(request.operation === "TRANSPORT" && request.demo_mode &&
            request.required_components[0] !== "modal"
              ? { arcnautical_route: {
                  origin_locode: originLocode.trim().toUpperCase(),
                  destination_locode: destinationLocode.trim().toUpperCase(),
                  shipment_id: request.shipment_ids[0],
                } }
              : {}),
            ...(!request.demo_mode && businessText.trim()
              ? { business_inputs: JSON.parse(businessText) }
              : {}),
          };
      const result = await api<{ run_id: string }>(
        whatIf ? `/api/v1/runs/${selectedId}/what-if` : "/api/v1/runs",
        token,
        body,
      );
      setSelectedId(result.run_id);
      setRun(undefined);
      setDrawer(false);
      setRouteIndex(0);
      refreshRuns();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function decide(decision: string) {
    if (!run?.approval) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/runs/${run.run_id}/approval`, approvalToken, {
        decision,
        plan_hash: run.approval.plan_hash,
        reason: approvalReason,
      });
      await refreshRun();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function execute() {
    if (!run) return;
    setBusy(true);
    try {
      await api(`/api/v1/runs/${run.run_id}/execute`, token, {});
      await refreshRun();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function reconcileExecution() {
    if (!run) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/runs/${run.run_id}/reconcile`, token, {});
      await refreshRun();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function download() {
    if (!run) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(run, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `exochain-${run.run_id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand-mark">
          E<span>+</span>
        </div>
        <div className="rail-links">
          {[
            { name: "Command Center", icon: Layers3 },
            { name: "Live Operations", icon: Globe2 },
            { name: "Intelligence", icon: Activity },
            { name: "Decisions", icon: FileCheck },
            { name: "Optimization", icon: SlidersHorizontal },
            { name: "Scenarios / What-If", icon: Settings2 },
            { name: "Agents", icon: Radio },
            { name: "Audit Trail", icon: Clock },
            { name: "Data Sources", icon: Database },
            { name: "System Health", icon: ShieldCheck },
          ].map(({ name, icon: Icon }) => (
            <button
              key={name}
              aria-label={name}
              title={name}
              onClick={() => setView(name)}
              className={view === name ? "active" : ""}
            >
              <Icon size={17} />
              <span>{name}</span>
            </button>
          ))}
        </div>
        <button
          title="Access credentials"
          aria-label="Access credentials"
          onClick={() => setAuthOpen(true)}
        >
          <LockKeyhole size={19} />
        </button>
        <div className="rail-version">
          OS
          <br />
          01
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="brand">
            <span>EXOCHAIN</span>
            <span className="brand-divider" />
            <span className="product-name">CONTROL TOWER</span>
            <span className="workspace-tag">
              {health?.environment?.toUpperCase() || "ENV UNAVAILABLE"}
            </span>
          </div>
          <div className="top-actions">
            <span className="utc-clock">
              {now ? now.toISOString().slice(11, 19) + " UTC" : "?"}
              <small>{now?.toLocaleTimeString()} local</small>
            </span>
            <span className={`stream ${connected ? "online" : ""}`}>
              <Radio size={13} />
              {connected ? "Telemetry connected" : "Telemetry reconnecting"}
            </span>
            <button
              className="icon-button"
              aria-label="Access settings"
              onClick={() => setAuthOpen(true)}
            >
              <Settings2 size={17} />
            </button>
            <div className="avatar">OP</div>
          </div>
        </header>
        <div className="healthbar">
          <span className="tiny-label">SYSTEM HEALTH</span>
          {[
            "ais",
            "kafka",
            "redis",
            "ocean",
            "weather",
            "ports",
            "optimization",
            "erp",
          ].map((key) => (
            <div
              className="health-item"
              key={key}
              title={`${key}: ${health?.dependencies[key] || "NOT CHECKED"}`}
            >
              <i className={tone(health?.dependencies[key])} />
              <span>
                {key === "ais"
                  ? "AIS"
                  : key === "erp"
                    ? "ERP"
                    : key[0].toUpperCase() + key.slice(1)}
              </span>
              <small>
                {(health?.dependencies[key] || "NOT CHECKED").replaceAll(
                  "_",
                  " ",
                )}
              </small>
            </div>
          ))}
          <span className="health-time">
            {health
              ? new Date(health.checked_at).toLocaleTimeString()
              : "Backend not connected"}
          </span>
        </div>
        <main className={`view-${view.toLowerCase().replaceAll(" ", "-")}`}>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                GLOBAL NETWORK / {view.toUpperCase()}
              </div>
              <h1>{view}</h1>
              <p>Evidence first. Every recommendation traceable.</p>
            </div>
            <div className="heading-actions">
              <button
                className="secondary"
                onClick={() => {
                  refreshRuns();
                  refreshRun();
                }}
              >
                <RefreshCw size={14} /> Refresh
              </button>
              <button
                className="primary"
                onClick={() => {
                  setRequest(defaultRequest);
                  setBusinessText("");
                  setWhatIf(false);
                  setDrawer(true);
                }}
              >
                <Play size={14} /> New decision run
              </button>
            </div>
          </div>
          {error && (
            <div className="error-banner">
              <TriangleAlert size={17} />
              <span>{error}</span>
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          <div className="runbar">
            <div>
              <span className="tiny-label">CURRENT RUN</span>
              <select
                aria-label="Current run"
                value={selectedId}
                onChange={(e) => {
                  if (e.target.value === selectedId) return;
                  setRun(undefined);
                  setSelectedId(e.target.value);
                  setRouteIndex(0);
                }}
              >
                <option value="">Select a recorded run</option>
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.run_id.slice(0, 8)} / {r.request.operation} / {r.run_id === selectedId && planningOnly && r.status === "BLOCKED" ? "PREVIEW AVAILABLE (EXECUTION BLOCKED)" : r.status}
                  </option>
                ))}
              </select>
            </div>
            <Badge status={planningOnly && run?.status === "BLOCKED" ? "PREVIEW_AVAILABLE" : run?.status} />
            {planningOnly && run?.status === "BLOCKED" && <Badge status="EXECUTION_BLOCKED" />}
            {run?.execution_lifecycle?.stage === "RECONCILED" && (
              <Badge status="RECONCILED" />
            )}
            {run?.request.what_if && <Badge status="SIMULATION" />}
            {run?.request.simulation && <Badge status="TEST_SCENARIO" />}
            <span className="run-id">{run?.run_id || "No run selected"}</span>
            <span className="run-updated">
              <Clock size={13} />{" "}
              {run
                ? time(Object.values(run.timestamps).at(-1))
                : "Awaiting input snapshot"}
            </span>
            <button
              className="icon-button"
              aria-label="Download immutable run record"
              disabled={!run}
              onClick={download}
            >
              <Download size={16} />
            </button>
          </div>
          <div className="command-metrics">
            <Metric
              label="Telemetry / freshness"
              value={
                telemetryAt && now
                  ? `${Math.max(0, Math.floor((now.getTime() - new Date(telemetryAt).getTime()) / 1000))}s`
                  : "Unavailable"
              }
            />
            <Metric
              label="Fresh vessels / displayed sample"
              value={
                telemetryAt
                  ? String(vessels.filter((v) => v.quality === "VALID").length)
                  : "Unavailable"
              }
            />
            <Metric
              label="Aircraft / run snapshot"
              value={
                Array.isArray(run?.data?.sources?.aviation?.payload?.aircraft)
                  ? String(run.data.sources.aviation.payload.aircraft.length)
                  : "Unavailable"
              }
            />
            <Metric
              label="Agent health / selected run"
              value={
                run?.data
                  ? `${counts.successful} healthy / ${agents.length} reported`
                  : "Not assessed"
              }
            />
            <div className="metric">
              <span>System / telemetry quality</span>
              <Badge status={health?.status || "UNAVAILABLE"} />
              <Badge
                status={
                  telemetryAt &&
                  now &&
                  now.getTime() - new Date(telemetryAt).getTime() > 30000
                    ? "STALE"
                    : telemetryStatus
                }
              />
            </div>
          </div>
          {!token && !approvalToken && (
            <div className="access-prompt">
              <LockKeyhole size={16} />
              <span>
                Connect workspace credentials to access protected telemetry and
                inspect decisions and run an assessment.
              </span>
              <button className="text-button" onClick={() => setAuthOpen(true)}>
                Connect workspace &rarr;
              </button>
            </div>
          )}
          {view === "Intelligence" && <IntelligenceView run={run} />}
          {view === "Decisions" && (
            <>
              <DecisionsView run={run} />
              <Replenishment
                run={run}
                canReconcile={!!token}
                busy={busy}
                onReconcile={reconcileExecution}
              />
            </>
          )}
          {view === "Agents" && <AgentsView run={run} onAgent={setAgent} />}
          {view === "System Health" && <SystemView health={health} run={run} />}
          {view === "Scenarios / What-If" && (
            <ScenarioView
              run={run}
              token={token || approvalToken}
              onConfigure={() => {
                if (run) {
                  setRequest(run.request);
                  setWhatIf(true);
                  setDrawer(true);
                }
              }}
            />
          )}
          {view === "Optimization" && <RouteComparison routes={routes} />}
          {["Command Center", "Live Operations", "Optimization"].includes(
            view,
          ) && (
            <>
              <div className="decision-grid">
                <section className="panel map-panel">
                  <div className="panel-title">
                    <div>
                      <Globe2 size={16} />
                      <h2>Network overview</h2>
                      <span className="subtle">
                        {mapVessels.length.toLocaleString()} observed vessels in
                        view
                      </span>
                    </div>
                    <div className="map-controls">
                      <button
                        className={liveMap ? "selected" : ""}
                        onClick={() => setLiveMap(true)}
                      >
                        Live observations
                      </button>
                      <button
                        className={!liveMap ? "selected" : ""}
                        onClick={() => setLiveMap(false)}
                        disabled={!run?.data}
                      >
                        Run snapshot
                      </button>
                    </div>
                  </div>
                  <OperationsMap
                    vessels={mapVessels}
                    routes={mapRoutes}
                    recommended={mapRecommended}
                    ports={ports}
                    risks={
                      (run?.intelligence?.results?.risk?.output?.assessments ||
                        []) as { entity_id: string; score?: number | null }[]
                    }
                    trajectory={
                      run?.data?.vessels?.find((v) => v.mmsi === vessel?.mmsi)
                        ?.history
                    }
                    onVessel={(v) => {
                      setVessel(v);
                      setSegment(undefined);
                    }}
                    onSegment={(s) => {
                      setSegment(s);
                      setVessel(undefined);
                    }}
                  />
                  <div className="map-legend">
                    <span>
                      <i className="dot good" /> Fresh vessel
                    </span>
                    <span>
                      <i className="dot warn" /> Stale observation
                    </span>
                    <span>
                      <i className="legend-line" /> Recommended
                    </span>
                    <span>
                      <i className="legend-line alternative" /> Alternative
                    </span>
                    <span className="legend-note">
                      Route geometry requires a navigational network
                    </span>
                  </div>
                  {(vessel || segment) && (
                    <div className="map-inspector">
                      <button
                        className="close"
                        aria-label="Close map details"
                        onClick={() => {
                          setVessel(undefined);
                          setSegment(undefined);
                        }}
                      >
                        <X size={16} />
                      </button>
                      {vessel ? (
                        <>
                          <span className="eyebrow">VESSEL OBSERVATION</span>
                          <h3>{vessel.vessel_name || "Unnamed vessel"}</h3>
                          <p className="footnote">
                            Trajectory: selected run observations only.{" "}
                            {run?.data?.vessels?.find(
                              (v) => v.mmsi === vessel.mmsi,
                            )?.history?.length || "No"}{" "}
                            recorded positions.
                          </p>
                          <Badge status={vessel.quality} />
                          <dl>
                            <dt>MMSI</dt>
                            <dd>{vessel.mmsi}</dd>
                            <dt>Position</dt>
                            <dd>
                              {number(vessel.position.latitude, 4)},{" "}
                              {number(vessel.position.longitude, 4)}
                            </dd>
                            <dt>Speed / heading</dt>
                            <dd>
                              {number(vessel.position.speed_knots)} kn /{" "}
                              {number(vessel.position.heading, 0)} deg
                            </dd>
                            <dt>Destination</dt>
                            <dd>
                              {vessel.destination || "Destination unavailable"}
                            </dd>
                            <dt>ETA</dt>
                            <dd>
                              {vessel.eta ? time(vessel.eta) : "Unavailable"}
                            </dd>
                            <dt>Observed</dt>
                            <dd>{time(vessel.position.timestamp)}</dd>
                            <dt>Source</dt>
                            <dd>{vessel.source || "Unavailable"}</dd>
                            <dt>Risk</dt>
                            <dd>
                              {String(
                                (
                                  run?.intelligence?.results?.risk?.output
                                    ?.assessments as
                                    | { entity_id: string; score?: number }[]
                                    | undefined
                                )?.find((a) => a.entity_id === vessel.mmsi)
                                  ?.score ?? "Incomplete evidence",
                              )}
                            </dd>
                          </dl>
                          <details>
                            <summary>
                              Weather & environmental observations
                            </summary>
                            <pre>
                              {JSON.stringify(
                                {
                                  weather: vessel.weather || "Unavailable",
                                  current: vessel.ocean || "Unavailable",
                                  waves: "Unavailable",
                                },
                                null,
                                2,
                              )}
                            </pre>
                          </details>
                          {vessel.ocean &&
                            Object.keys(vessel.ocean).length > 0 && (
                              <details>
                                <summary>Observed ocean current</summary>
                                <pre>
                                  {JSON.stringify(vessel.ocean, null, 2)}
                                </pre>
                              </details>
                            )}
                        </>
                      ) : (
                        segment && (
                          <>
                            <span className="eyebrow">ROUTE SEGMENT</span>
                            <h3>{segment.id}</h3>
                            <dl>
                              <dt>Distance</dt>
                              <dd>{number(segment.distance_km)} km</dd>
                              <dt>Duration</dt>
                              <dd>{number(segment.duration_hours)} h</dd>
                              <dt>Fuel</dt>
                              <dd>{number(segment.fuel_litres)} L</dd>
                              <dt>Cost</dt>
                              <dd>{money(segment.cost_usd)}</dd>
                              <dt>Risk index</dt>
                              <dd>{segment.risk_score}</dd>
                              <dt>Weather penalty</dt>
                              <dd>{segment.weather_penalty}</dd>
                              <dt>Current penalty</dt>
                              <dd>{segment.current_penalty}</dd>
                              <dt>Draft restriction</dt>
                              <dd>{segment.max_draft_m} m</dd>
                              <dt>Source</dt>
                              <dd>{segment.source}</dd>
                              <dt>Observed</dt>
                              <dd>{time(segment.observed_at)}</dd>
                            </dl>
                          </>
                        )
                      )}
                    </div>
                  )}
                </section>
                <aside className="panel route-panel">
                  <div className="panel-title">
                    <div>
                      <Anchor size={16} />
                      <h2>Decision intelligence</h2>
                    </div>
                    <Badge status={routeComponent?.status} />
                  </div>
                  <DecisionBrief run={run} />
                  {demoGeographic && selectedRoute && (
                    <div className="requirements" aria-label="Demo voyage evidence">
                      <p className="footnote">DEMO GEOGRAPHIC ROUTE — not a certified navigational route. Missing evidence is excluded, never assumed safe or zero.</p>
                      <p className="footnote">Voyage evidence checks {voyageAssessment?.checks_at ? `ran ${time(voyageAssessment.checks_at)}` : "pending"}. Checks run for each route candidate; they are not continuous live monitoring.</p>
                      <p className="footnote">{run?.request.vessel_reference?.vessel_name || "Vessel not specified"} · IMO {run?.request.vessel_reference?.imo || "unavailable"} · MMSI {run?.request.vessel_reference?.mmsi || "unavailable"}</p>
                      {(["Route geometry", "Distance", "Duration", "Vessel identity"] as const).map((label) => (
                        <div key={label}><Activity size={14} /><span>{label}</span><Badge status={label === "Vessel identity" && !run?.request.vessel_reference ? "UNAVAILABLE" : "AVAILABLE"} /></div>
                      ))}
                      {(["AIS", "Fuel", "Weather", "Ocean currents", "Waves", "Draft suitability", "Port activity"] as const).map((label) => {
                        const key = ({AIS:"vessel_state", Fuel:"fuel", Weather:"weather", "Ocean currents":"currents", Waves:"waves", "Draft suitability":"draft", "Port activity":"port_activity"} as const)[label];
                        const status = voyageAssessment?.summary?.[key];
                        const check = voyageAssessment?.checks?.[key];
                        const available = status === "AVAILABLE" || status === "VALID" || status === "PARTIAL";
                        return <div key={label} title={check?.reason || undefined}><Activity size={14} /><span>{label}{check?.lookup === "SKIPPED" ? " — checked; route lookup needs voyage timing" : check?.execution === "CHECKED" && !available ? " — checked, no usable data" : ""}</span><Badge status={available ? "AVAILABLE" : check?.lookup === "SKIPPED" ? "DATA_REQUIRED" : "OPTIONAL_UNAVAILABLE"} /></div>;
                      })}
                      <p className="footnote">Used in optimization: {((routeComponent?.explanation?.used_metrics as string[] | undefined) || ["distance", "time"]).join(", ")}. Other unavailable metrics: not used.</p>
                    </div>
                  )}
                  {voyageAssessment?.summary && !planningOnly && !demoGeographic && (
                    <div className="requirements" aria-label="Voyage evidence assessment">
                      <p className="footnote">Geographic planning only. Missing evidence is not treated as zero or safe.</p>
                      {([
                        ["Selected vessel / AIS", "vessel_state"],
                        ["Vessel fuel", "fuel"],
                        ["Weather along route", "weather"],
                        ["Ocean currents", "currents"],
                        ["Waves", "waves"],
                        ["Draft suitability", "draft"],
                        ["Port activity", "port_activity"],
                        ["Berth queue", "berth_queue"],
                      ] as const).map(([label, key]) => (
                        <div key={key}>
                          <Activity size={14} />
                          <span>{label}</span>
                          <Badge status={voyageAssessment.summary?.[key]} />
                        </div>
                      ))}
                      {voyageAssessment.fuel?.total_fuel_litres != null && (
                        <p className="footnote">Model-derived fuel estimate: {number(voyageAssessment.fuel.total_fuel_litres, 0)} L</p>
                      )}
                      {voyageAssessment.vessel_state?.observed_at && (
                        <p className="footnote">AIS observed: {time(voyageAssessment.vessel_state.observed_at)}. IMO is not verified by AIS.</p>
                      )}
                    </div>
                  )}
                  {planningEdge && !selectedRoute ? (
                    <div className="route-content" aria-label="Geographic planning preview">
                      <span className="eyebrow">AVAILABLE-DATA ROUTE PREVIEW</span>
                      <h3>{run?.request.shipment_ids?.[0] || "Geographic route"}</h3>
                      <p className="footnote">ArcNautical geographic pathfinding. Ranked by estimated transit time using available distance and duration only. This is not an operational or navigational optimum.</p>
                      <Badge status="PREVIEW_AVAILABLE" />
                      <h4>{planningEdge.planning_label || "Geographic route"}</h4>
                      <div className="route-metrics">
                        <Metric label="Distance" value={number(planningEdge.distance_km, 0)} unit="km" />
                        <Metric label="Estimated transit" value={number(planningEdge.duration_hours, 1)} unit="h" />
                      </div>
                      {!!planningEdge.hazard_zones_crossed?.length && (
                        <p className="footnote">Zones crossed: {planningEdge.hazard_zones_crossed.join(", ")}</p>
                      )}
                      {planningEdges.length > 1 && (
                        <div className="alternatives" aria-label="Geographic route alternatives">
                          {planningEdges.map((edge, index) => (
                            <button key={edge.id} className={index === routeIndex ? "selected" : ""}
                              onClick={() => setRouteIndex(index)}>
                              <span>{index === 0 ? "Fastest available" : "Alternative"}: {edge.planning_label || "Geographic route"}</span>
                              <small>{number(edge.distance_km, 0)} km · {number(edge.duration_hours, 1)} h</small>
                            </button>
                          ))}
                        </div>
                      )}
                      <p className="footnote">Source: {routeNetwork?.source || "ArcNautical"}. Cost, fuel, draft clearance, and voyage conditions are not estimated without supporting data.</p>
                    </div>
                  ) : selectedRoute ? (
                    <div className="route-content">
                      <span className="eyebrow">
                        {demoGeographic ? "DEMO GEOGRAPHIC ROUTE" : recommended.has(selectedRoute.candidate_id)
                          ? "RECOMMENDED ROUTE"
                          : "ALTERNATIVE ROUTE"}
                      </span>
                      <h3>{selectedRoute.shipment_id}</h3>
                      <div className="route-metrics">
                        <Metric
                          label="Transit time"
                          value={number(selectedRoute.duration_hours)}
                          unit="h"
                        />
                        <Metric
                          label="Distance"
                          value={number(selectedRoute.distance_km, 0)}
                          unit="km"
                        />
                        <Metric
                          label="Fuel"
                          value={number(selectedRoute.fuel_litres, 0)}
                          unit="L"
                        />
                        <Metric
                          label="Cost"
                          value={money(selectedRoute.cost_usd)}
                        />
                        <Metric
                          label="Risk index"
                          value={number(selectedRoute.risk_score, 3)}
                        />
                        <Metric
                          label="Objective"
                          value={number(selectedRoute.objective_value, 3)}
                        />
                      </div>
                      <div className="route-why">
                        <h4>Why this route?</h4>
                        <p>Actual contributions to the normalized objective.</p>
                        {Object.entries(
                          selectedRoute.objective_percentages,
                        ).map(([key, value]) => (
                          <div className="objective-row" key={key}>
                            <span>{key}</span>
                            <div>
                              <i style={{ width: `${value}%` }} />
                            </div>
                            <strong>{number(value, 1)}%</strong>
                          </div>
                        ))}
                      </div>
                      <h4>Alternatives & trade-offs</h4>
                      <div className="alternatives">
                        {routes.map((route, index) => (
                          <button
                            key={route.candidate_id}
                            className={index === routeIndex ? "selected" : ""}
                            onClick={() => setRouteIndex(index)}
                          >
                            <span>
                              {recommended.has(route.candidate_id) ? (
                                <Check size={14} />
                              ) : (
                                <ArrowRight size={14} />
                              )}{" "}
                              {recommended.has(route.candidate_id)
                                ? "Recommended"
                                : `Alternative ${index}`}
                            </span>
                            <small>
                              {money(route.cost_usd)} ?{" "}
                              {number(route.duration_hours)} h / risk{" "}
                              {number(route.risk_score, 2)}
                            </small>
                          </button>
                        ))}
                      </div>
                      <p className="footnote">
                        Optimality refers to the generated candidate set. No
                        global maritime optimum is implied.
                      </p>
                    </div>
                  ) : (
                    <>
                      <Empty title="Route data required">
                        Connect an authoritative navigational network and
                        shipment requirement to generate defensible routes.
                      </Empty>
                      <div className="requirements">
                        <div>
                          <MapPin size={14} />
                          <span>Origin & destination</span>
                          <Badge
                            status={
                              run?.business_inputs?.shipments &&
                              Array.isArray(run.business_inputs.shipments) &&
                              run.business_inputs.shipments.length
                                ? "AVAILABLE"
                                : "DATA_REQUIRED"
                            }
                          />
                        </div>
                        <div>
                          <Layers3 size={14} />
                          <span>Navigation network</span>
                          <Badge
                            status={
                              run?.business_inputs?.route_network
                                ? routeNetwork?.planning_classification === "GEOGRAPHIC_PLANNING_ONLY"
                                  ? "GEOGRAPHIC_ONLY"
                                  : "AVAILABLE"
                                : "UNAVAILABLE"
                            }
                          />
                        </div>
                        <div>
                          <Activity size={14} />
                          <span>Wave observations</span>
                          <Badge status="UNAVAILABLE" />
                        </div>
                      </div>
                      <div className="trust-note">
                        <ShieldCheck size={17} />
                        <p>
                          No routes, costs, or waypoints are generated without
                          supporting evidence.
                        </p>
                      </div>
                    </>
                  )}
                </aside>
              </div>
              <section className="panel optimizer-panel">
                <div className="panel-title">
                  <div>
                    <SlidersHorizontal size={16} />
                    <h2>{planningOnly ? "Geographic route comparison" : "Optimization engine"}</h2>
                    <span className="subtle">{planningOnly ? "Available distance and transit-time data" : "Algorithms & feasibility"}</span>
                  </div>
                  <button
                    className="text-button"
                    disabled={!run?.data}
                    onClick={() => {
                      if (run) {
                        setRequest(run.request);
                        setWhatIf(true);
                        setDrawer(true);
                      }
                    }}
                  >
                    Explore what-if <ArrowRight size={14} />
                  </button>
                </div>
                {planningOnly ? (
                  <div className="table-scroll">
                    <table>
                      <thead><tr><th>RANK</th><th>GEOGRAPHIC PATH</th><th>DISTANCE</th><th>ESTIMATED TRANSIT</th><th>STATUS</th></tr></thead>
                      <tbody>{planningEdges.map((edge, index) => (
                        <tr key={edge.id}>
                          <td>{index + 1}</td>
                          <td>{edge.planning_label || "Geographic route"}</td>
                          <td>{number(edge.distance_km, 0)} km</td>
                          <td>{number(edge.duration_hours, 1)} h</td>
                          <td><Badge status={index === 0 ? "FASTEST_PREVIEW" : "ALTERNATIVE"} /></td>
                        </tr>
                      ))}</tbody>
                    </table>
                    <p className="footnote">ArcNautical paths are ranked by estimated transit time; unverified cost, fuel, weather, draft clearance, and port conditions are excluded. OR-Tools approval optimization remains separate and blocked for execution.</p>
                  </div>
                ) : Object.keys(components).length ? (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>DOMAIN</th>
                          <th>ALGORITHM / SOLVER</th>
                          <th>CANDIDATES</th>
                          <th>OBJECTIVE</th>
                          <th>SOLVE TIME</th>
                          <th>RESULT</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(components).map(([name, c]) => (
                          <tr key={name}>
                            <td className="capitalize">{name}</td>
                            <td>
                              {c.solver || "Not executed"}
                              <small>{c.solver_version || ""}</small>
                            </td>
                            <td>{c.candidate_count}</td>
                            <td>{number(c.objective_value, 3)}</td>
                            <td>{number(c.solve_time_ms, 1)} ms</td>
                            <td>
                              <Badge status={c.status} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <div className="optimizer-footer">
                      <span>
                        Committed plan cost{" "}
                        <strong>
                          {money(run?.optimization?.total_cost_usd)}
                        </strong>
                      </span>
                      <span>
                        Scenario{" "}
                        <Badge
                          status={String(
                            run?.optimization?.scenario?.status ||
                              "ASSUMPTIONS_REQUIRED",
                          )}
                        />
                      </span>
                      <span>
                        Input snapshot{" "}
                        <strong>{run?.run_id.slice(0, 8)}</strong>
                      </span>
                    </div>
                    {run?.optimization?.scenario?.statistics != null && (
                      <div className="scenario-results">
                        <h3>SIMULATION ? Monte Carlo outcomes</h3>
                        <p className="subtle">
                          Operator-supplied assumptions; these are not measured
                          probabilities.
                        </p>
                        <table>
                          <thead>
                            <tr>
                              <th>Quantity</th>
                              <th>MEAN</th>
                              <th>P50</th>
                              <th>P90</th>
                              <th>P95</th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(
                              run.optimization.scenario.statistics as Record<
                                string,
                                Record<string, number> | number
                              >,
                            ).map(([key, stats]) => (
                              <tr key={key}>
                                <td>{key.replaceAll("_", " ")}</td>
                                {["mean", "p50", "p90", "p95"].map((p) => (
                                  <td key={p}>
                                    {typeof stats === "number"
                                      ? p === "mean"
                                        ? number(stats, 4)
                                        : "—"
                                      : stats[p] == null
                                        ? "Unavailable"
                                        : stats[p].toLocaleString(undefined, {
                                            maximumFractionDigits: 2,
                                          })}
                                  </td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <details className="explanation-details">
                      <summary>
                        Inspect mathematical formulation, constraints and solver
                        metadata
                      </summary>
                      <pre>{JSON.stringify(components, null, 2)}</pre>
                    </details>
                  </div>
                ) : (
                  <div className="inline-empty">
                    <SlidersHorizontal size={20} />
                    <div>
                      <strong>Optimization awaits validated candidates</strong>
                      <p>
                        Solver names, objective values and timings appear only
                        after execution.
                      </p>
                    </div>
                    <Badge status={run?.optimization?.status || "NOT_RUN"} />
                  </div>
                )}
              </section>
              <div className="lower-grid">
                <section className="panel agents-panel">
                  <div className="panel-title">
                    <div>
                      <Activity size={16} />
                      <h2>Agent orchestration</h2>
                    </div>
                    <span className="subtle">
                      {agents.length} / 19 executed for this run
                    </span>
                  </div>
                  <div className="stage-flow">
                    {stages.map((stage, index) => (
                      <div
                        key={stage}
                        className={
                          run?.audit?.some((e) => e.stage === stage)
                            ? "recorded"
                            : ""
                        }
                      >
                        <span>
                          {stage === "decisions" ? "Decision" : stage}
                        </span>
                        {index < stages.length - 1 && <ArrowRight size={11} />}
                      </div>
                    ))}
                  </div>
                  <div className="agent-summary">
                    <strong>19 agents</strong>
                    <span className="good-text">
                      {counts.successful} successful
                    </span>
                    <span className="warn-text">
                      {counts.unavailable} unavailable
                    </span>
                    <span>{counts.partial} partial</span>
                    <span className={counts.errors ? "bad-text" : ""}>
                      {counts.errors} errors
                    </span>
                  </div>
                  <p className="footnote">All 19 agents are invoked on each decision run. Unavailable means an agent completed its check without usable evidence; it does not mean the agent was skipped. Agents are not continuously running between decisions.</p>
                  <div className="agent-groups">
                    {Object.entries(expectedAgents).map(([domain, names]) => (
                      <div key={domain}>
                        <h4>{domain}</h4>
                        {names.map((name) => {
                          const record = agents.find(
                            (a) =>
                              a.agent_id === `${domain.toLowerCase()}.${name}`,
                          );
                          const runtimeEvent = run?.audit?.findLast(
                            (e) =>
                              e.stage === `${domain.toLowerCase()}.${name}`,
                          );
                          return (
                            <button
                              key={name}
                              className="agent-row"
                              disabled={!record}
                              onClick={() => setAgent(record)}
                            >
                              <i
                                className={`dot ${tone(record?.status || run?.audit?.findLast((e) => e.stage === `${domain.toLowerCase()}.${name}`)?.status)}`}
                              />
                              <span>{name.replaceAll("_", " ")}</span>
                              <small>
                                {record
                                  ? `${number(record.latency_ms, 0)} ms`
                                  : runtimeEvent?.status.replaceAll("_", " ") ||
                                    "Not run"}
                              </small>
                            </button>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                </section>
                <section className="panel explanation-panel">
                  <div className="panel-title">
                    <div>
                      <FileCheck size={16} />
                      <h2>Decision & authorization</h2>
                    </div>
                  </div>
                  <div className="explanation-body">
                    <div className="decision-state">
                      <ShieldCheck size={20} />
                      <div>
                        <strong>
                          {run?.status === "BLOCKED"
                            ? "Execution safely blocked"
                            : run?.approval?.status === "PENDING_APPROVAL"
                              ? "Awaiting human approval"
                              : run
                                ? "Review the recorded decision"
                                : "No decision submitted"}
                        </strong>
                        <p>
                          {run?.execution?.message ||
                            "Approval is bound to an immutable plan, its cost and selected actions."}
                        </p>
                      </div>
                    </div>
                    {reasons.length > 0 && (
                      <>
                        <h4>What information is missing?</h4>
                        <ul className="reason-list">
                          {reasons.map((r) => (
                            <li key={r}>
                              <span />
                              {r.replaceAll("_", " ")}
                            </li>
                          ))}
                        </ul>
                      </>
                    )}
                    {run?.decisions?.explanation && (
                      <details>
                        <summary>
                          What the system knows, options & assumptions
                        </summary>
                        <pre>
                          {JSON.stringify(run.decisions.explanation, null, 2)}
                        </pre>
                      </details>
                    )}
                    {run?.approval && (
                      <div className="approval-box">
                        <Badge status={run.approval.status} />
                        <dl>
                          <dt>Cost</dt>
                          <dd>{money(run.approval.cost_usd)}</dd>
                          <dt>Approval expiry</dt>
                          <dd>{time(run.approval.expires_at)}</dd>
                          <dt>Plan fingerprint</dt>
                          <dd className="mono">
                            {run.approval.plan_hash.slice(0, 16)}...
                          </dd>
                        </dl>
                        {run.approval.status === "PENDING_APPROVAL" && (
                          <>
                            <input
                              aria-label="Approval reason"
                              value={approvalReason}
                              onChange={(e) =>
                                setApprovalReason(e.target.value)
                              }
                              placeholder="Reason for approval or rejection"
                            />
                            <div className="approval-actions">
                              <button
                                className="secondary"
                                disabled={
                                  !approvalToken || !approvalReason || busy
                                }
                                onClick={() => decide("REJECTED")}
                              >
                                Reject
                              </button>
                              <button
                                className="primary"
                                disabled={
                                  !approvalToken || !approvalReason || busy
                                }
                                onClick={() => decide("APPROVED")}
                              >
                                Approve plan
                              </button>
                            </div>
                            <p className="footnote">
                              An approver credential is required.
                            </p>
                          </>
                        )}
                        {run.approval.status === "APPROVED" && !run.request.demo_mode && (
                          <button
                            className="primary"
                            disabled={!token || busy}
                            onClick={execute}
                          >
                            Submit authorized execution
                          </button>
                        )}
                      </div>
                    )}
                    {!run && (
                      <p className="footnote">
                        Create an assessment run to inspect available data.
                        Enterprise decisions require imported business records.
                      </p>
                    )}
                  </div>
                </section>
              </div>
            </>
          )}
          {view === "Data Sources" && (
            <section className="panel">
              <div className="panel-title">
                <div>
                  <Database size={16} />
                  <h2>Source evidence & freshness</h2>
                </div>
              </div>
              <div className="source-grid">
                {["kafka", "redis"].map((key) => (
                  <article className="source-card" key={key}>
                    <div>
                      <h3>{key.toUpperCase()}</h3>
                      <Badge
                        status={health?.dependencies[key] || "UNAVAILABLE"}
                      />
                    </div>
                    <p>
                      Connectivity checked {time(health?.checked_at)}. Provider
                      observations below belong to the selected immutable run.
                    </p>
                  </article>
                ))}
              </div>
              {run?.data ? (
                <div className="source-grid">
                  {Object.entries(run.data.sources).map(([key, source]) => (
                    <article className="source-card" key={key}>
                      <div>
                        <h3 className="capitalize">{key}</h3>
                        <Badge
                          status={
                            !source.payload
                              ? "UNAVAILABLE"
                              : source.valid_until &&
                                  now &&
                                  new Date(source.valid_until) < now
                                ? "STALE"
                                : source.last_known_data
                                  ? "CACHED"
                                  : source.quality === "VALID"
                                    ? "CACHED"
                                    : source.quality
                          }
                        />
                      </div>
                      <p>{source.source}</p>
                      <dl>
                        <dt>Observed</dt>
                        <dd>{time(source.observed_at)}</dd>
                        <dt>Recorded quality</dt>
                        <dd>{source.quality}</dd>
                        <dt>Record counts</dt>
                        <dd>
                          {Object.entries(source.payload || {})
                            .filter(([, v]) => Array.isArray(v))
                            .map(([k, v]) => `${k}: ${(v as unknown[]).length}`)
                            .join(" / ") || "Unavailable"}
                        </dd>
                        <dt>Received</dt>
                        <dd>{time(source.received_at)}</dd>
                        <dt>Valid until</dt>
                        <dd>{time(source.valid_until)}</dd>
                        <dt>Provider latency</dt>
                        <dd>{number(source.latency_ms)} ms</dd>
                        <dt>Cached fallback</dt>
                        <dd>
                          {source.last_known_data
                            ? "Yes / stale evidence"
                            : "No"}
                        </dd>
                      </dl>
                      {source.errors.map((e) => (
                        <p className="warn-text" key={e}>
                          {e}
                        </p>
                      ))}
                    </article>
                  ))}
                </div>
              ) : (
                <Empty title="No input snapshot selected">
                  Choose a run to inspect its immutable source evidence.
                </Empty>
              )}
            </section>
          )}
          {(view === "Audit Trail" || view === "Command Center") && (
            <section className="panel audit-panel">
              <div className="panel-title">
                <div>
                  <Clock size={16} />
                  <h2>Audit trail</h2>
                  <span className="subtle">Append-only runtime events</span>
                </div>
                <span className="subtle">{run?.audit?.length || 0} events</span>
              </div>
              {run?.audit?.length ? (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>TIME</th>
                        <th>STAGE / AGENT</th>
                        <th>STATUS</th>
                        <th>ACTOR / REASON</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...run.audit]
                        .reverse()
                        .slice(0, view === "Command Center" ? 6 : undefined)
                        .map((event) => (
                          <tr key={event.id}>
                            <td className="mono">
                              {new Date(event.timestamp).toLocaleTimeString()}
                            </td>
                            <td>{event.stage}</td>
                            <td>
                              <Badge status={event.status} />
                            </td>
                            <td>
                              <small>{event.actor}</small>
                              {event.reason}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="inline-empty">
                  <Clock size={20} />
                  <div>
                    <strong>No execution events</strong>
                    <p>
                      Run transitions and approval decisions will appear here
                      with their source and timestamp.
                    </p>
                  </div>
                </div>
              )}
            </section>
          )}
          <footer>
            <span>EXOCHAIN / EVIDENCE-DRIVEN OPERATIONS</span>
            <span>Unknown is a valid state. Fabricated certainty is not.</span>
            <a href={`${API}/docs`} target="_blank" rel="noreferrer">
              API contract <ExternalLink size={11} />
            </a>
          </footer>
        </main>
      </div>
      {authOpen && (
        <div className="modal-backdrop">
          <section className="modal compact">
            <div className="panel-title">
              <h2>Workspace access</h2>
              <button
                aria-label="Close access settings"
                onClick={() => setAuthOpen(false)}
              >
                <X size={18} />
              </button>
            </div>
            <div className="form-body">
              <p>
                Credentials are held in memory for this tab only. Configure
                independent operator and approver tokens on the backend.
              </p>
              <label>
                Operator token
                <input
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
              </label>
              <label>
                Approver token
                <input
                  type="password"
                  autoComplete="off"
                  value={approvalToken}
                  onChange={(e) => setApprovalToken(e.target.value)}
                />
              </label>
              <button className="primary" onClick={() => setAuthOpen(false)}>
                Connect workspace
              </button>
            </div>
          </section>
        </div>
      )}
      {drawer && (
        <div className="modal-backdrop">
          <section className="modal run-modal">
            <div className="panel-title">
              <div>
                <SlidersHorizontal size={17} />
                <h2>
                  {whatIf ? "What-if / immutable snapshot" : "New decision run"}
                </h2>
              </div>
              <button
                aria-label="Close new run"
                onClick={() => setDrawer(false)}
              >
                <X size={18} />
              </button>
            </div>
            <div className="form-body">
              <p>
                {whatIf
                  ? `Reuses snapshot ${selectedId.slice(0, 8)}. This run cannot approve or execute production actions.`
                  : "For a transport demo, enter a shipment and port pair. ArcNautical supplies geographic route candidates; unavailable evidence remains optional."}
              </p>
              <div className="form-grid">
                <label>
                  Operation
                  <select
                    value={request.operation}
                    disabled={whatIf}
                    onChange={(e) =>
                      setRequest({
                        ...request,
                        operation: e.target.value,
                        required_components: e.target.value === "TRANSPORT" ? ["route"] : [],
                        demo_mode: e.target.value === "TRANSPORT",
                        shipment_ids: e.target.value === "TRANSPORT" && !request.shipment_ids.length
                          ? ["ARC-SGSIN-NLRTM-DEMO"] : request.shipment_ids,
                        vessel_reference: e.target.value === "TRANSPORT" ? request.vessel_reference : null,
                      })
                    }
                  >
                    <option value="ASSESSMENT">Data assessment</option>
                    <option value="TRANSPORT">Transport planning</option>
                    <option value="REPLENISHMENT">
                      Inventory replenishment
                    </option>
                  </select>
                </label>
                <label>
                  Budget / USD
                  <input
                    type="number"
                    min="0"
                    value={request.budget_usd ?? ""}
                    onChange={(e) =>
                      setRequest({
                        ...request,
                        budget_usd: e.target.value
                          ? Number(e.target.value)
                          : null,
                      })
                    }
                    placeholder="Explicit budget required"
                  />
                </label>
                <label>
                  Maximum duration / hours
                  <input
                    type="number"
                    min="0"
                    value={request.max_duration_hours ?? ""}
                    onChange={(e) =>
                      setRequest({
                        ...request,
                        max_duration_hours: e.target.value
                          ? Number(e.target.value)
                          : null,
                      })
                    }
                  />
                </label>
                <label>
                  Maximum risk index
                  <input
                    type="number"
                    min="0"
                    max="1"
                    step="0.05"
                    value={request.max_risk}
                    onChange={(e) =>
                      setRequest({
                        ...request,
                        max_risk: Number(e.target.value),
                      })
                    }
                  />
                </label>
                {request.operation === "TRANSPORT" && (
                  <>
                    <label>
                      Shipment IDs / comma separated
                      <input
                        disabled={whatIf}
                        value={request.shipment_ids.join(",")}
                        onChange={(e) =>
                          setRequest({
                            ...request,
                            shipment_ids: e.target.value
                              .split(",")
                              .map((v) => v.trim())
                              .filter(Boolean),
                          })
                        }
                      />
                    </label>
                    <label>
                      Transport decision
                      <select
                        disabled={whatIf}
                        value={request.required_components[0] || "route"}
                        onChange={(e) =>
                          setRequest({
                            ...request,
                            required_components: [e.target.value],
                            demo_mode: e.target.value === "route" ? request.demo_mode : false,
                          })
                        }
                      >
                        <option value="route">
                          Maritime route operating plan
                        </option>
                        <option value="modal">
                          Carrier / modal allocation
                        </option>
                      </select>
                    </label>
                    {request.required_components[0] !== "modal" && (
                      <>
                        <label>
                          <input type="checkbox" checked={!!request.demo_mode} disabled={whatIf}
                            onChange={(e) => setRequest({ ...request, demo_mode: e.target.checked })} />
                          Demo geographic optimization (no real booking)
                        </label>
                        {request.demo_mode && (
                          <>
                            <label>Origin UN/LOCODE
                              <input value={originLocode} maxLength={5} disabled={whatIf}
                                onChange={(e) => setOriginLocode(e.target.value.toUpperCase())} />
                            </label>
                            <label>Destination UN/LOCODE
                              <input value={destinationLocode} maxLength={5} disabled={whatIf}
                                onChange={(e) => setDestinationLocode(e.target.value.toUpperCase())} />
                            </label>
                          </>
                        )}
                      </>
                    )}
                    <label>
                      Vessel MMSI / 9 digits
                      <input
                        disabled={whatIf}
                        inputMode="numeric"
                        maxLength={9}
                        value={request.vessel_reference?.mmsi || ""}
                        onChange={(e) => setRequest({
                          ...request,
                          vessel_reference: e.target.value
                            ? { shipment_id: request.shipment_ids[0] || "", mmsi: e.target.value,
                                imo: request.vessel_reference?.imo || null,
                                vessel_name: request.vessel_reference?.vessel_name || null }
                            : null,
                        })}
                        placeholder="Optional exact AIS lookup"
                      />
                    </label>
                    <label>
                      Vessel IMO / 7 digits
                      <input
                        disabled={whatIf || !request.vessel_reference?.mmsi}
                        inputMode="numeric"
                        maxLength={7}
                        value={request.vessel_reference?.imo || ""}
                        onChange={(e) => setRequest({
                          ...request,
                          vessel_reference: request.vessel_reference
                            ? { ...request.vessel_reference, imo: e.target.value || null }
                            : null,
                        })}
                      />
                    </label>
                    <label>
                      Vessel name / AIS cross-check
                      <input
                        disabled={whatIf || !request.vessel_reference?.mmsi}
                        value={request.vessel_reference?.vessel_name || ""}
                        onChange={(e) => setRequest({
                          ...request,
                          vessel_reference: request.vessel_reference
                            ? { ...request.vessel_reference, vessel_name: e.target.value || null }
                            : null,
                        })}
                      />
                    </label>
                  </>
                )}
                {request.operation === "REPLENISHMENT" && (
                  <label>
                    Inventory position IDs
                    <input
                      disabled={whatIf}
                      value={request.inventory_ids.join(",")}
                      onChange={(e) =>
                        setRequest({
                          ...request,
                          inventory_ids: e.target.value
                            .split(",")
                            .map((v) => v.trim())
                        })
                      }
                    />
                  </label>
                )}
              </div>
              <h4>Optimization preferences</h4>
              <label>
                <input
                  type="checkbox"
                  checked={request.require_human_approval}
                  onChange={(e) =>
                    setRequest({
                      ...request,
                      require_human_approval: e.target.checked,
                    })
                  }
                />
                Require human approval
              </label>
              <p className="footnote">
                Clearing this request only permits automatic approval when
                server policy explicitly allows it and cost is within the
                approval threshold.
              </p>
              <label>
                Warehouse capacity / units
                <input
                  type="number"
                  min="1"
                  value={request.warehouse_capacity_units ?? ""}
                  onChange={(e) =>
                    setRequest({
                      ...request,
                      warehouse_capacity_units: e.target.value
                        ? Number(e.target.value)
                        : null,
                    })
                  }
                />
              </label>
              <p className="footnote">
                Weights are operator preferences. They are not business
                observations.
              </p>
              <div className="weight-grid">
                {Object.entries(request.weights).map(([key, value]) => (
                  <label key={key}>
                    <span>
                      {key}
                      <strong>{value}</strong>
                    </span>
                    <input
                      aria-label={`${key} weight`}
                      type="range"
                      min="0"
                      max="10"
                      step="0.25"
                      value={value}
                      onChange={(e) =>
                        setRequest({
                          ...request,
                          weights: {
                            ...request.weights,
                            [key]: Number(e.target.value),
                          },
                        })
                      }
                    />
                  </label>
                ))}
              </div>
              <details>
                <summary>Monte Carlo / explicit simulation assumptions</summary>
                <p className="footnote">
                  These are operator assumptions, not measured business
                  observations. Every enabled assumption is required.
                </p>
                <button
                  className="secondary"
                  onClick={() =>
                    setRequest({
                      ...request,
                      scenarios: request.scenarios
                        ? null
                        : { source: "", explanation: "" },
                    })
                  }
                >
                  {request.scenarios
                    ? "Disable simulation"
                    : "Enable simulation assumptions"}
                </button>
                {request.scenarios && (
                  <div className="form-grid">
                    {["source", "explanation"].map((k) => (
                      <label key={k}>
                        {k}
                        <input
                          value={String(request.scenarios?.[k] ?? "")}
                          onChange={(e) =>
                            setRequest({
                              ...request,
                              scenarios: {
                                ...request.scenarios,
                                [k]: e.target.value,
                              },
                            })
                          }
                        />
                      </label>
                    ))}
                    {[
                      "cost_std_fraction",
                      "duration_std_fraction",
                      "disruption_probability",
                      "disruption_cost_multiplier",
                      "disruption_time_multiplier",
                      "iterations",
                      "seed",
                    ].map((k) => (
                      <label key={k}>
                        {k.replaceAll("_", " ")}
                        <input
                          type="number"
                          step="any"
                          value={
                            request.scenarios?.[k] == null
                              ? ""
                              : Number(request.scenarios[k])
                          }
                          onChange={(e) =>
                            setRequest({
                              ...request,
                              scenarios: {
                                ...request.scenarios,
                                [k]: e.target.value
                                  ? Number(e.target.value)
                                  : null,
                              },
                            })
                          }
                        />
                      </label>
                    ))}
                  </div>
                )}
              </details>
              {!whatIf && !(request.operation === "TRANSPORT" && request.demo_mode) && (
                <>
                  <label>
                    Authoritative business input / JSON
                    <input
                      type="file"
                      accept="application/json,.json"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (file) setBusinessText(await file.text());
                      }}
                    />
                    <textarea
                      rows={6}
                      value={businessText}
                      onChange={(e) => setBusinessText(e.target.value)}
                      placeholder="Leave empty to use the configured enterprise provider. No records will be manufactured."
                    />
                  </label>
                  <a
                    className="text-link"
                    href={`${API}/api/v1/contracts/business`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open canonical import schema <ExternalLink size={12} />
                  </a>
                </>
              )}
              <div className="form-actions">
                <span>
                  {token
                    ? "Operator credential supplied"
                    : "Connect an operator credential to submit"}
                </span>
                <button
                  className="primary"
                  disabled={busy || !token}
                  onClick={startRun}
                >
                  {busy
                    ? "Submitting..."
                    : whatIf
                      ? "Run what-if analysis"
                      : "Run decision pipeline"}
                  <ArrowRight size={15} />
                </button>
              </div>
            </div>
          </section>
        </div>
      )}
      {agent && (
        <div className="modal-backdrop">
          <section className="modal">
            <div className="panel-title">
              <h2>{agent.agent_id}</h2>
              <button
                aria-label="Close agent details"
                onClick={() => setAgent(undefined)}
              >
                <X size={18} />
              </button>
            </div>
            <div className="form-body">
              <Badge status={agent.status} />
              <dl>
                <dt>Latency</dt>
                <dd>{number(agent.latency_ms)} ms</dd>
                <dt>Input quality</dt>
                <dd>{agent.input_quality}</dd>
                <dt>Confidence</dt>
                <dd>
                  {agent.confidence == null
                    ? "Not calibrated"
                    : agent.confidence}
                </dd>
                <dt>Source</dt>
                <dd>{agent.source}</dd>
                <dt>Completed</dt>
                <dd>{time(agent.completed_at)}</dd>
              </dl>
              <pre>{JSON.stringify(agent.output, null, 2)}</pre>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
