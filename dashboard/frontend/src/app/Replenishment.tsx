"use client";
import type { Run } from "./types";

const display = (v: unknown) =>
  v == null
    ? "Unavailable"
    : typeof v === "object"
      ? JSON.stringify(v)
      : String(v);

export default function Replenishment({
  run,
  canReconcile,
  busy,
  onReconcile,
}: {
  run?: Run;
  canReconcile: boolean;
  busy: boolean;
  onReconcile: () => void;
}) {
  if (run?.request.operation !== "REPLENISHMENT") return null;
  const identity = run.data?.business_identity;
  const lifecycle = run.execution_lifecycle;
  const inventory = run.optimization?.components?.inventory?.selected || [];
  const procurement = run.optimization?.components?.procurement?.selected || [];
  const feasibility = run.optimization?.components?.inventory?.explanation
    .feasibility_analysis as Record<string, unknown> | undefined;
  const rows = (
    title: string,
    records: Record<string, unknown>[],
    fields: string[],
  ) => (
    <section className="panel evidence-card">
      <h3>{title}</h3>
      {records.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {fields.map((f) => (
                  <th key={f}>{f.replaceAll("_", " ")}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {records.map((r, i) => (
                <tr key={i}>
                  {fields.map((f) => (
                    <td key={f}>{display(r[f])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p>No supported records available.</p>
      )}
    </section>
  );
  return (
    <div className="workspace-cards">
      <section className="panel evidence-card">
        <h2>Replenishment source</h2>
        <p>Run lifecycle: {run.status}</p>
        <dl className="evidence-fields">
          {Object.entries(identity || {}).map(([k, v]) => (
            <div key={k}>
              <dt>{k.replaceAll("_", " ")}</dt>
              <dd>{display(v)}</dd>
            </div>
          ))}
        </dl>
        <p>
          Business quality:{" "}
          {run.data?.sources?.business?.quality || "UNAVAILABLE"}
        </p>
        <p>
          Validation:{" "}
          {run.validation?.valid === undefined
            ? "NOT RUN"
            : run.validation.valid
              ? "VALID"
              : "BLOCKED"}{" "}
          · {run.validation?.reasons?.join(" · ")}
        </p>
        <p>
          Approval: {run.approval?.status || "NOT ISSUED"} ·{" "}
          {run.approval?.mode} · {run.approval?.policy_reason}
        </p>
        <p>
          Budget: {display(run.request.budget_usd)} USD · Plan cost:{" "}
          {display(run.optimization?.total_cost_usd)} USD
        </p>
      </section>
      {feasibility && (
        <section className="panel evidence-card">
          <h3>Feasibility evidence — not an executable plan</h3>
          <p>Diagnostic solver: {display(feasibility.status)}</p>
          <p>
            Minimum feasible budget:{" "}
            {display(feasibility.minimum_feasible_budget_usd)} USD
          </p>
          <p>{display(feasibility.scope)}</p>
          <details>
            <summary>Binding requirements and allocation witness</summary>
            <pre>{JSON.stringify(feasibility, null, 2)}</pre>
          </details>
        </section>
      )}
      {rows(
        "Authoritative inventory",
        (run.business_inputs.inventory || []) as Record<string, unknown>[],
        [
          "id",
          "sku_id",
          "location_id",
          "current_inventory_units",
          "expected_demand_units",
          "demand_reference",
          "horizon_days",
          "safety_stock_units",
          "service_level",
          "max_stock_units",
          "source",
          "version",
          "provenance",
        ],
      )}
      {rows("Inventory optimization", inventory, [
        "id",
        "order_units",
        "shortage_units",
        "ending_units",
        "ordering_cost_usd_committed",
      ])}
      {rows(
        "Supplier candidates",
        (run.decisions?.candidates || [])
          .filter((c) => c.decision_type === "supplier")
          .map((c) => c.parameters),
        [
          "id",
          "supplier_id",
          "sku_id",
          "location_id",
          "capacity_units",
          "unit_cost_usd",
          "lead_time_days",
          "risk_score",
          "supplier_quality",
          "supplier_reliability",
          "provenance",
        ],
      )}
      {rows("Selected procurement", procurement, [
        "id",
        "supplier_id",
        "sku_id",
        "location_id",
        "units",
        "unit_cost_usd",
        "total_cost_usd",
        "lead_time_days",
      ])}
      <section className="panel evidence-card">
        <h2>ERP execution and reconciliation</h2>
        <p>
          Execution: {run.execution?.status || "NOT SUBMITTED"} ·{" "}
          {run.execution?.simulation ? "SIMULATION" : "Production"}
        </p>
        <p>
          ERP PO identifier:{" "}
          {run.execution?.external_reference || "Unavailable"}
        </p>
        <p>Lifecycle: {lifecycle?.stage || "NOT CREATED"}</p>
        <p>
          {run.execution?.message} {lifecycle?.error}
        </p>
        {lifecycle?.command && !run.request.simulation && (
          <button
            className="primary"
            disabled={!canReconcile || busy || lifecycle.stage === "RECONCILED"}
            onClick={onReconcile}
          >
            Reconcile with ERP
          </button>
        )}
        {lifecycle?.history?.map((h, i) => (
          <p key={i}>
            {h.timestamp} · {h.stage}
          </p>
        ))}
        <details>
          <summary>Exact command and acknowledgement</summary>
          <pre>{JSON.stringify(lifecycle, null, 2)}</pre>
        </details>
      </section>
      {rows("Current execution feedback", run.feedback || [], [
        "supplier_id",
        "sku_id",
        "location_id",
        "ordered_quantity",
        "planned_cost_usd",
        "acknowledged_order_cost_usd",
        "planned_lead_time_days",
        "actual_order_lead_time_days",
        "status",
        "provenance",
      ])}
      {rows(
        "Prior acknowledged outcomes in this snapshot",
        run.data?.feedback || [],
        [
          "supplier_id",
          "sku_id",
          "location_id",
          "ordered_quantity",
          "planned_cost_usd",
          "acknowledged_order_cost_usd",
          "planned_lead_time_days",
          "actual_order_lead_time_days",
          "status",
          "provenance",
        ],
      )}
    </div>
  );
}
