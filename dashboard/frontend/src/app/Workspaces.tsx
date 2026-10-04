"use client";
import { useEffect, useState } from "react";
import { api } from "./api";
import type { Agent, Health, Run, Route } from "./types";

const names = {
  DATA: ["maritime", "aviation", "environment", "ports", "market", "business"],
  INTELLIGENCE: [
    "risk",
    "demand",
    "forecast",
    "disruption",
    "anomaly",
    "scenario",
    "port",
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
const label = (s: string) => s.replaceAll("_", " ");
const stamp = (s?: string) =>
  s ? new Date(s).toLocaleString() : "Unavailable";
const value = (v: unknown): string =>
  v == null
    ? "Unavailable"
    : typeof v === "number"
      ? v.toLocaleString(undefined, { maximumFractionDigits: 3 })
      : typeof v === "object"
        ? JSON.stringify(v)
        : String(v);
export function State({ status = "UNAVAILABLE" }: { status?: string }) {
  return (
    <span
      className={`badge ${/^(HEALTHY|VALID|SUCCESS|APPROVED|EXECUTED|OPTIMAL|LIVE|PREVIEW_AVAILABLE)$/.test(status) ? "good" : /FAILED|ERROR|REJECTED/.test(status) ? "bad" : "warn"}`}
    >
      <i />
      {label(status)}
    </span>
  );
}
function Evidence({ data }: { data: Record<string, unknown> }) {
  return (
    <dl className="evidence-fields">
      {Object.entries(data).map(([k, v]) => (
        <div key={k}>
          <dt>{label(k)}</dt>
          <dd>{value(v)}</dd>
        </div>
      ))}
    </dl>
  );
}
export function DecisionBrief({ run }: { run?: Run }) {
  const blocked = run?.status === "BLOCKED";
  const planningOnly = run?.request.operation === "TRANSPORT" && !run?.request.demo_mode &&
    (run?.business_inputs?.route_network as { planning_classification?: string } | undefined)
      ?.planning_classification === "GEOGRAPHIC_PLANNING_ONLY";
  const planningEdges = (run?.business_inputs?.route_network as {
    edges?: { distance_km?: number; duration_hours?: number }[];
  } | undefined)?.edges || [];
  const planningEdge = [...planningEdges].sort((a, b) =>
    (a.duration_hours ?? Infinity) - (b.duration_hours ?? Infinity))[0];
  const reasons = run?.validation?.reasons || run?.decisions?.missing || [];
  const selected = Object.values(run?.optimization?.components || {}).flatMap(
    (c) => c.selected,
  );
  return (
    <div className="decision-brief">
      <div className="brief-heading">
        <span className="eyebrow">CURRENT DECISION</span>
        <State
          status={
            planningOnly && blocked
              ? "PREVIEW_AVAILABLE"
              : blocked
              ? "BLOCKED"
              : run?.execution?.status ||
                (run?.approval?.status === "PENDING_APPROVAL"
                  ? "APPROVAL_REQUIRED"
                  : run?.status || "DATA_REQUIRED")
          }
        />
      </div>
      <h3>
        {planningOnly && blocked
          ? "Geographic route preview available"
          : blocked
          ? "Plan blocked by validation"
          : selected.length
            ? "Review the optimized plan"
            : "Awaiting a decision run"}
      </h3>
      <p>
        {planningOnly && blocked
          ? "The route can be explored for planning. It is not a verified navigational plan; approval and execution remain blocked."
          : blocked
          ? "The assessment is recorded. Execution remains blocked until a valid business plan can be established."
          : "Every selected action must pass validation and bound human approval."}
      </p>
      {planningOnly && planningEdge && (
        <p>ArcNautical fastest available path: {value(planningEdge.distance_km)} km · {value(planningEdge.duration_hours)} hours. Compared {planningEdges.length} geographic candidate{planningEdges.length === 1 ? "" : "s"}; no navigational optimum is claimed.</p>
      )}
      {!planningOnly && reasons.length > 0 && (
        <ul className="brief-reasons">
          {reasons.slice(0, 4).map((r) => (
            <li key={r}>{label(r)}</li>
          ))}
        </ul>
      )}
      <div className="safety-flow">
        <span>
          DATA{" "}
          {(run?.request.operation === "REPLENISHMENT"
            ? run.data?.sources?.business?.quality
            : run?.data?.quality?.status) || "NOT RUN"}
        </span>
        <span>OPTIMIZATION {run?.optimization?.status || "NOT RUN"}</span>
        <span>APPROVAL {run?.approval?.status || "NOT ISSUED"}</span>
        <span>ERP {run?.execution?.status || "NOT EXECUTED"}</span>
      </div>
      {!planningOnly && <Evidence
        data={{
          ...(run?.request.demo_mode ? {
            route_classification: "DEMO GEOGRAPHIC ROUTE — not for navigation",
            vessel_name: run.request.vessel_reference?.vessel_name || "Unavailable",
            vessel_imo: run.request.vessel_reference?.imo || "Unavailable",
            vessel_mmsi: run.request.vessel_reference?.mmsi || "Unavailable",
          } : {}),
          budget_usd: run?.request.budget_usd,
          capacity_units: run?.request.warehouse_capacity_units,
          max_risk: run?.request.max_risk,
          max_duration_hours: run?.request.max_duration_hours,
          plan_cost_usd: run?.optimization?.total_cost_usd,
        }}
      />}
      <details>
        <summary>Intelligence factors & provenance</summary>
        {Object.entries(run?.intelligence?.results || {}).map(([k, r]) => (
          <p key={k}>
            <b>{label(k)}</b> · {r.explanation}
          </p>
        ))}
        <p>
          Sources:{" "}
          {Object.values(run?.data?.sources || {})
            .map((s) => s.source)
            .join(", ") || "Unavailable"}
        </p>
      </details>
    </div>
  );
}
export function IntelligenceView({ run }: { run?: Run }) {
  return (
    <div className="workspace-cards">
      {names.INTELLIGENCE.map((name) => {
        const r = run?.intelligence?.results?.[name];
        return (
          <article className="panel evidence-card" key={name}>
            <div className="card-heading">
              <h2>{name === "port" ? "Port intelligence" : label(name)}</h2>
              <State status={r?.status} />
            </div>
            <p>
              {r?.explanation ||
                "Run an assessment to collect source evidence."}
            </p>
            <Evidence
              data={{
                confidence: r?.confidence,
                timestamp: r?.timestamp ? stamp(r.timestamp) : undefined,
                source: r?.model_source,
                affected_entities:
                  r?.input_references?.join(", ") || "Unavailable",
              }}
            />
            {r && (
              <details>
                <summary>Metrics & observations</summary>
                <Evidence data={r.output} />
              </details>
            )}
          </article>
        );
      })}
    </div>
  );
}
export function AgentsView({
  run,
  onAgent,
}: {
  run?: Run;
  onAgent: (a: Agent) => void;
}) {
  const all = [
    ...(run?.data?.executions || []),
    ...(run?.intelligence?.executions || []),
    ...(run?.decisions?.executions || []),
  ];
  return (
    <div className="cluster-grid">
      {Object.entries(names).map(([domain, members]) => {
        const records = all.filter((a) => a.domain === domain);
        const state =
          records.length !== members.length
            ? "UNAVAILABLE"
            : records.some((a) => /FAILED|ERROR/.test(a.status))
              ? "ERROR"
              : records.every((a) => a.status === "SUCCESS")
                ? "HEALTHY"
                : "DEGRADED";
        return (
          <section className="panel cluster" key={domain}>
            <div className="panel-title">
              <h2>
                {domain} <small>{members.length}</small>
              </h2>
              <State status={state} />
            </div>
            <p className="cluster-note">
              {records.length} / {members.length} executed for this run · result status reflects available evidence
            </p>
            {members.map((name) => {
              const a = records.find(
                (a) => a.agent_id === `${domain.toLowerCase()}.${name}`,
              );
              const status = !a
                ? "UNAVAILABLE"
                : a.status === "SUCCESS"
                  ? "HEALTHY"
                  : a.status === "PARTIAL" || a.status === "STALE"
                    ? "DEGRADED"
                    : /FAILED|ERROR/.test(a.status)
                      ? "ERROR"
                      : /REQUIRED|BLOCKED/.test(a.status)
                        ? "BLOCKED"
                        : "UNAVAILABLE";
              return (
                <button
                  className="matrix-agent"
                  key={name}
                  onClick={() => a && onAgent(a)}
                  disabled={!a}
                >
                  <div className="card-heading">
                    <strong>{label(name)}</strong>
                    <State status={status} />
                  </div>
                  <Evidence
                    data={{
                      latency_ms: a?.latency_ms,
                      last_run: a ? stamp(a.completed_at) : undefined,
                      source: a?.source,
                      input_freshness: a?.input_quality,
                      confidence: a?.confidence,
                    }}
                  />
                  {a?.errors.length ? (
                    <p className="bad-text">{a.errors.join(" · ")}</p>
                  ) : null}
                  <span className="subtle">Inspect output & errors →</span>
                </button>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}
export function DecisionsView({ run }: { run?: Run }) {
  return (
    <>
      <DecisionBrief run={run} />
      <div className="workspace-cards">
        {names.DECISION.map((name) => {
          const record = run?.decisions?.executions?.find(
            (a) => a.agent_id === `decision.${name}`,
          );
          const candidates = (record?.output?.candidates || []) as NonNullable<
            NonNullable<Run["decisions"]>["candidates"]
          >;
          return (
            <article className="panel evidence-card" key={name}>
              <div className="card-heading">
                <h2>{label(name)}</h2>
                <State status={record?.status} />
              </div>
              {candidates.length ? (
                candidates.map((c) => {
                  const selected = Object.entries(
                    run?.optimization?.components || {},
                  ).some(([domain, s]) =>
                    s.selected.some(
                      (x) =>
                        x.candidate_id === c.candidate_id ||
                        (domain ===
                          (c.decision_type === "supplier"
                            ? "procurement"
                            : c.decision_type) &&
                          x.id === c.entity_id),
                    ),
                  );
                  const state = !selected
                    ? "CANDIDATE"
                    : run?.status === "BLOCKED"
                      ? "BLOCKED"
                      : run?.execution?.status === "EXECUTED"
                        ? "EXECUTED"
                        : run?.approval?.status === "APPROVED"
                          ? "APPROVED"
                          : run?.validation?.valid
                            ? "VALIDATED"
                            : "OPTIMIZED";
                  return (
                    <div className="candidate" key={c.candidate_id}>
                      <State status={state} />
                      <h3>{label(c.action)}</h3>
                      <Evidence
                        data={{
                          entity: c.entity_id,
                          confidence: c.confidence,
                          optimization_required: c.requires_optimization,
                          approval_required: c.requires_human_approval,
                          execution: selected
                            ? run?.execution?.status || "NOT EXECUTED"
                            : "NOT SELECTED",
                          constraints: c.constraints,
                          expected_effect: c.expected_effect,
                        }}
                      />
                      <details>
                        <summary>Rationale & provenance</summary>
                        <Evidence
                          data={{
                            parameters: c.parameters,
                            provenance: c.provenance,
                          }}
                        />
                      </details>
                    </div>
                  );
                })
              ) : (
                <p>
                  {((record?.output?.missing as string[]) || [])
                    .map(label)
                    .join(" · ") ||
                    (name === "executive"
                      ? "Evidence synthesis available in the decision record."
                      : "No supported candidate available.")}
                </p>
              )}
              {record && (
                <details>
                  <summary>Agent rationale</summary>
                  <Evidence data={record.output} />
                </details>
              )}
            </article>
          );
        })}
      </div>
    </>
  );
}
export function RouteComparison({ routes }: { routes: Route[] }) {
  if (!routes.length) return null;
  return (
    <section className="panel evidence-card">
      <h2>Candidate comparison · observed plan estimates</h2>
      <div className="comparison-grid">
        {(
          [
            ["cost_usd", "Cost / USD"],
            ["duration_hours", "Time / h"],
            ["risk_score", "Risk index"],
            ["fuel_litres", "Fuel / L"],
          ] as const
        ).map(([key, title]) => {
          const max = Math.max(...routes.map((r) => r[key] ?? 0), 1);
          return (
            <div key={key}>
              <h4>{title}</h4>
              {routes.map((r, i) => (
                <div className="compare-row" key={r.candidate_id}>
                  <span>
                    {r.shipment_id} / {i + 1}
                  </span>
                  {r[key] != null ? <meter min={0} max={max} value={r[key]} /> : <span>Not measured</span>}
                  <b>{value(r[key])}</b>
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </section>
  );
}
export function ScenarioView({
  run,
  token,
  onConfigure,
}: {
  run?: Run;
  token: string;
  onConfigure: () => void;
}) {
  const [baseline, setBaseline] = useState<Run>();
  const [error, setError] = useState("");
  const parent = run?.request.parent_run_id;
  useEffect(() => {
    if (!parent || !token) return;
    let active = true;
    api<Run>(`/api/v1/runs/${parent}`, token)
      .then((r) => {
        if (active) setBaseline(r);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [parent, token]);
  const base = parent
    ? baseline?.run_id === parent
      ? baseline
      : undefined
    : run;
  const ready =
    !!run?.data &&
    run.request.operation !== "ASSESSMENT" &&
    !!Object.values(run.business_inputs || {}).some(
      (v) => Array.isArray(v) && v.length,
    );
  const fields = [
    "budget_usd",
    "max_duration_hours",
    "max_risk",
    "warehouse_capacity_units",
    "weights",
    "scenarios",
  ] as const;
  return (
    <section className="panel evidence-card">
      <div className="card-heading">
        <h2>What-if analysis</h2>
        <State status="SIMULATION" />
      </div>
      <p>
        Replay the immutable input snapshot with explicit constraints and
        objective preferences. Scenario runs cannot execute.
      </p>
      <button
        className="primary scenario-launch"
        disabled={!ready}
        onClick={onConfigure}
      >
        Configure scenario →
      </button>
      {!ready && (
        <div className="inline-empty">
          <strong>
            Scenario unavailable — required business data missing.
          </strong>
        </div>
      )}
      {error && <p className="bad-text">{error}</p>}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>PARAMETER</th>
              <th>BASELINE</th>
              <th>SCENARIO</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((k) => (
              <tr key={k}>
                <td>{label(k)}</td>
                <td>{value(base?.request[k])}</td>
                <td>{parent ? value(run?.request[k]) : "Not run"}</td>
              </tr>
            ))}
            <tr>
              <td>Plan cost / USD</td>
              <td>{value(base?.optimization?.total_cost_usd)}</td>
              <td>
                {parent ? value(run?.optimization?.total_cost_usd) : "Not run"}
              </td>
            </tr>
            <tr>
              <td>Result</td>
              <td>
                <State status={base?.status} />
              </td>
              <td>{parent ? <State status={run?.status} /> : "Not run"}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {parent && (
        <p className="scenario-note">
          Changed:{" "}
          {fields
            .filter(
              (k) =>
                JSON.stringify(base?.request[k]) !==
                JSON.stringify(run?.request[k]),
            )
            .map(label)
            .join(", ") || "No constraint changes"}
          .{" "}
          {run?.status === "BLOCKED"
            ? "No feasible executable result was established."
            : "Review optimizer and validation results before interpreting the comparison."}
        </p>
      )}
    </section>
  );
}
export function SystemView({ health, run }: { health?: Health; run?: Run }) {
  const deps = {
    fastapi: health ? "HEALTHY" : "UNAVAILABLE",
    ...health?.dependencies,
  };
  return (
    <>
      <div className="workspace-cards">
        {Object.entries(deps).map(([k, s]) => (
          <article className="panel evidence-card" key={k}>
            <div className="card-heading">
              <h2>{label(k)}</h2>
              <State status={s} />
            </div>
            <p>Checked {stamp(health?.checked_at)}</p>
            <p>
              {k === "fastapi"
                ? `Health probe: ${value(health?.latency_ms)} ms`
                : "Individual service latency unavailable"}
            </p>
          </article>
        ))}
      </div>
      <div className="panel evidence-card">
        <h2>Agent health reflects the selected assessment</h2>
        <p>
          Run {run?.run_id || "unavailable"}. Inspect the Agents workspace for
          observed latency, source quality and errors.
        </p>
      </div>
    </>
  );
}
