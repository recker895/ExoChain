"use client";
import dynamic from "next/dynamic";
import { useState } from "react";
import type { Audit, RequestSpec, Route, Run } from "./types";
import { catalog, portLabel } from "./maritimeSelection";

const OperationsMap = dynamic(() => import("./OperationsMap"), { ssr: false });
const tasks: Record<string, string> = {
  "data.business": "Port inputs & ArcNautical routing", "data.maritime": "Vessel / AIS lookup",
  "data.environment": "Weather, current & wave collection", "data.ports": "PortWatch activity",
  "data.aviation": "Aircraft feed check (not used for sea routes)", "data.market": "Disruption records check",
  "intelligence.risk": "Evidence-based route risk", "intelligence.demand": "Demand scope check",
  "intelligence.forecast": "Transit-time estimate", "intelligence.disruption": "Chokepoints & disruption evidence",
  "intelligence.anomaly": "Route data checks", "intelligence.port": "Origin / destination activity",
  "intelligence.scenario": "Route preference comparisons", "decision.route": "Evidence-enriched route candidates",
  "decision.modal": "Carrier-allocation scope check", "decision.inventory": "Inventory scope check",
  "decision.supplier": "Procurement scope check", "decision.disruption_response": "Affected-route cross-check",
  "decision.executive": "Agent synthesis / optional AI explanation",
};
const sourceAgents: Record<string, string> = {business: "data.business", selected_vessel: "data.maritime",
  weather: "data.environment", ocean: "data.environment", waves: "data.environment", ports: "data.ports"};
const human = (s: string) => s.replaceAll("_", " ").toLowerCase();
const fmt = (v: unknown, digits = 2) => typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: digits }) : "Not available";
const statusText = (s: string) => s === "SUCCESS" ? "Completed · evidence produced" : s === "NOT_APPLICABLE" ? "Completed · not needed for this voyage" : s === "PARTIAL" ? "Completed · partial evidence" : s === "STALE" ? "Completed · dated evidence" : s === "RUNNING" ? "Running now" : s === "WAITING" ? "Waiting" : `Completed · ${human(s)}`;

function PortChoice({ label, code, onChange }: { label: string; code: string; onChange: (s: string) => void }) {
  const [search, setSearch] = useState("");
  const [custom, setCustom] = useState(!catalog.ports.some(p => p.locode === code));
  const options = catalog.ports.filter(p => p.locode === code || `${p.name} ${p.locode} ${p.country}`.toLowerCase().includes(search.toLowerCase()));
  const port = catalog.ports.find(p => p.locode === code);
  return <div className="catalog-choice">
    <label>{label}<input type="search" aria-label={`Search ${label.toLowerCase()}`} placeholder="Search by name, country or code" value={search} onChange={e => setSearch(e.target.value)} />
      <select aria-label={label} value={custom ? "CUSTOM" : code} onChange={e => { setCustom(e.target.value === "CUSTOM"); onChange(e.target.value === "CUSTOM" ? "" : e.target.value); }}>
        {options.map(p => <option key={p.locode} value={p.locode}>{p.name} · {p.locode}</option>)}
        <option value="CUSTOM">Other supported port…</option>
      </select>
    </label>
    {custom ? <label>Custom UN/LOCODE<input maxLength={5} placeholder="Five-character port code" value={code} onChange={e => onChange(e.target.value.toUpperCase())} /><small>Must be supported by the routing provider; no guessed coordinates.</small></label>
      : port && <small>{port.country} · <a href={port.source} target="_blank" rel="noreferrer">Port code source</a> · static catalog</small>}
  </div>;
}

export function MaritimeRunForm({ request, setRequest, origin, setOrigin, destination, setDestination, speed, setSpeed, advanced }: {
  request: RequestSpec; setRequest: (r: RequestSpec) => void;
  origin: string; setOrigin: (s: string) => void; destination: string; setDestination: (s: string) => void;
  speed: number; setSpeed: (n: number) => void; advanced: () => void;
}) {
  const reference = request.vessel_reference || { shipment_id: request.shipment_ids[0], mmsi: "", imo: "", vessel_name: "" };
  const [search, setSearch] = useState("");
  const vessel = catalog.vessels.find(v => v.imo === reference.imo);
  const vessels = catalog.vessels.filter(v => v.imo === reference.imo || `${v.name} ${v.imo} ${v.mmsi}`.toLowerCase().includes(search.toLowerCase()));
  return <div className="form-body maritime-form">
    <p>Choose a vessel and two ports. The agents collect real available evidence, generate alternatives, and let OR-Tools compare them. Nothing is booked or purchased.</p>
    <div className="form-grid">
      <div className="catalog-choice"><label>Vessel<input type="search" aria-label="Search vessels" placeholder="Search ship name, IMO or MMSI" value={search} onChange={e => setSearch(e.target.value)} /><select aria-label="Vessel" value={reference.imo || ""} onChange={e => {
        const chosen = catalog.vessels.find(v => v.imo === e.target.value);
        if (chosen) setRequest({ ...request, vessel_reference: { shipment_id: reference.shipment_id, vessel_name: chosen.name, imo: chosen.imo, mmsi: chosen.mmsi } });
      }}><option value="" disabled>Choose a real ship</option>{vessels.map(v => <option key={v.imo} value={v.imo}>{v.name} · IMO {v.imo}</option>)}</select></label>
      <small>IMO {reference.imo || "—"} · MMSI {reference.mmsi || "—"}<br />{vessel && <><a href={vessel.source} target="_blank" rel="noreferrer">Identity source</a> · checked {vessel.verified_at}</>}<br />Sourced static catalog, not live tracking. MMSI is source-reported and may change.</small></div>
      <label>Planning speed · knots (your assumption)<input type="number" min={1} max={40} value={speed} onChange={e => setSpeed(Number(e.target.value))} /><small>Used to calculate travel time; not the ship’s measured speed.</small></label>
      <PortChoice label="Departure port" code={origin} onChange={setOrigin} />
      <PortChoice label="Destination port" code={destination} onChange={setDestination} />
      {origin && origin === destination && <p role="alert">Departure and destination must differ.</p>}
      <label>Maximum duration · hours (optional)<input type="number" min={1} value={request.max_duration_hours ?? ""} onChange={e => setRequest({ ...request, max_duration_hours: e.target.value ? Number(e.target.value) : null })} /></label>
      <label>Maximum comparative risk index<input type="number" min={0} max={1} step={0.05} value={request.max_risk} onChange={e => setRequest({ ...request, max_risk: Number(e.target.value) })} /><small>Checked only when environmental evidence exists. Not a safety probability.</small></label>
    </div>
    <details><summary>Optimization preferences</summary><div className="weight-grid">
      {["distance", "time", "risk", "weather", "current", "wave", "fuel", "port"].map(key => <label key={key}><span>{key}<strong>{request.weights[key] ?? 1}</strong></span><input aria-label={`${key} preference`} type="range" min={0} max={5} step={0.5} value={request.weights[key] ?? 1} onChange={e => setRequest({ ...request, weights: { ...request.weights, [key]: Number(e.target.value) } })} /></label>)}
    </div></details>
    <label className="demo-checkbox"><input type="checkbox" checked={!!request.use_ai_explanation} onChange={e => setRequest({ ...request, use_ai_explanation: e.target.checked })} />Use the configured Groq AI for a plain-English agent summary</label>
    <p className="footnote">Speed is your planning assumption, not the ship’s measured speed. Choosing a different ship changes the AIS lookup, not sea geography; no ship-specific fuel or speed performance is invented. Environmental data is a current model snapshot along the corridors, not a prediction for every future passage time. Missing fuel, prices or depth data stays unavailable and does not stop the geographic comparison.</p>
    <button type="button" className="text-button" onClick={advanced}>Other workflows / advanced inputs →</button>
  </div>;
}

export default function MaritimeDemo({ run, liveEvents, onStart }: { run?: Run; liveEvents: Audit[]; onStart: () => void }) {
  const maritime = run?.request.operation === "TRANSPORT" && run.request.demo_mode;
  const records = maritime ? [...(run?.data?.executions || []), ...(run?.intelligence?.executions || []), ...(run?.decisions?.executions || [])] : [];
  const component = maritime ? run?.optimization?.components?.route : undefined;
  const selected = (component?.selected || []) as unknown as Route[];
  const candidates = (run?.decisions?.routes || []) as Route[];
  const scored = [...selected, ...((component?.alternatives || []) as unknown as Route[])];
  const scoredById = new Map(scored.map(r => [r.candidate_id, r]));
  const recommended = new Set(selected.map(r => r.candidate_id));
  const reference = maritime ? run?.request.vessel_reference : undefined;
  const report = component?.explanation || {};
  const used = (report.used_metrics || []) as string[];
  const excluded = (report.excluded_metrics || []) as string[];
  const unchecked = (report.unchecked_constraints || []) as string[];
  const context = run?.data?.route_context;
  const recordedVoyage = ((run?.data?.sources?.business?.payload?.shipments || []) as { id: string; origin_id: string; destination_id: string }[])
    .find(s => run?.request.shipment_ids.includes(s.id));
  const vesselSource = run?.data?.sources?.selected_vessel;
  const ai = run?.decisions?.explanation?.ai_synthesis as { status?: string; summary?: string; reason?: string; model?: string } | undefined;
  const events = [...(run?.audit || []), ...liveEvents].filter(e => e.run_id === run?.run_id);
  const latest = (id: string) => events.filter(e => e.stage === id).sort((a, b) => a.timestamp.localeCompare(b.timestamp)).at(-1);
  return <div className="maritime-demo">
    <section className="panel demo-intro">
      <div><span className="eyebrow">MARITIME ROUTE PLANNING · PUBLIC DATA</span><h2>Multi-agent maritime route optimization</h2>
        <p>Public data → specialized agents → route alternatives → Google OR-Tools → best route within the compared candidates.</p></div>
      <button className="primary" onClick={onStart}>Plan a route →</button>
    </section>
    {maritime && <section className="panel demo-context">
      <div><span>Selected vessel</span><strong>{reference?.vessel_name || "Not supplied"}</strong><small>IMO {reference?.imo || "—"} · MMSI {reference?.mmsi || "—"}</small></div>
      <div><span>Selected ports</span><strong>{portLabel(context?.routing?.origin_locode || context?.selection?.departure?.locode || recordedVoyage?.origin_id || "—")} → {portLabel(context?.routing?.destination_locode || context?.selection?.destination?.locode || recordedVoyage?.destination_id || "—")}</strong><small>Speed: {context?.routing?.planning_speed_knots ?? "—"} knots · planning assumption</small></div>
      <div><span>AIS observation</span><strong>{human(vesselSource?.quality || "WAITING")}</strong><small>{vesselSource?.observed_at ? new Date(vesselSource.observed_at).toLocaleString() : "No vessel position is invented"}</small></div>
      <div><span>Route alternatives</span><strong>{candidates.length || Object.keys(context?.routes || {}).length}</strong><small>Computed on ArcNautical’s public geographic network</small></div>
      <div><span>Agents invoked</span><strong>{records.length} / 19</strong><small>Execution and evidence availability are different</small></div>
    </section>}
    <section className="panel demo-agents"><div className="panel-title"><h2>1 · Agents working together</h2><span>Click any completed task to inspect its output</span></div>
      <div className="demo-agent-grid">{Object.entries(tasks).map(([id, name]) => {
        const record = records.find(r => r.agent_id === id);
        const event = maritime ? latest(id) : undefined;
        const status = event?.status === "RUNNING" ? "RUNNING" : record?.status || event?.status || "WAITING";
        return <details key={id} className={`demo-agent status-${status.toLowerCase()}`}><summary><strong>{name}</strong><span>{statusText(status)}</span></summary>
          <p>{record ? `${fmt(record.latency_ms, 0)} ms · ${record.source}` : "This task has not produced a saved result yet."}</p>
          {record && <pre>{JSON.stringify(record.output, null, 2)}</pre>}
        </details>;
      })}</div>
      <p className="demo-note">All 19 existing tasks execute. Weather, wave and current retrieval run concurrently inside the environment task. Inventory, procurement and aviation report their relevance honestly; they are not artificially marked successful. These are orchestrated specialist/tool agents—not 19 independent language models.</p>
    </section>
    <section className="panel"><div className="panel-title"><h2>2 · Real route alternatives</h2><span>ArcNautical generates geometry; OR-Tools selects among it</span></div>
      {candidates.length ? <div className="demo-table-wrap"><table className="demo-table"><thead><tr><th>Route</th><th>Distance · nm</th><th>Estimated hours</th><th>Risk index</th><th>Weather penalty</th><th>Wave penalty</th><th>Current penalty</th><th>Objective</th></tr></thead><tbody>
        {candidates.map(candidate => { const r = scoredById.get(candidate.candidate_id) || candidate; const rejection = component?.rejected.find(x => x.candidate_id === r.candidate_id); return <tr key={r.candidate_id} className={recommended.has(r.candidate_id) ? "selected-route" : ""}><td><strong>{r.planning_label || r.candidate_id.slice(0, 8)}</strong><small>{recommended.has(r.candidate_id) ? "Selected by OR-Tools" : rejection ? `Rejected: ${(rejection.reasons as string[]).join(", ")}` : "Alternative"}</small></td><td>{fmt(r.distance_km / 1.852, 1)}</td><td>{fmt(r.duration_hours, 1)}</td><td>{fmt(r.risk_score)}</td><td>{fmt(r.weather_penalty)}</td><td>{fmt(r.wave_penalty)}</td><td>{fmt(r.current_penalty)}</td><td>{fmt(r.objective_value, 4)}</td></tr>; })}
      </tbody></table></div> : <p className="demo-note">Start a route run. Generated candidates appear here; no alternative is manufactured.</p>}
      <p className="demo-note">Estimated time uses distance ÷ planning speed. Penalties and risk are comparative indices from public model evidence—not probabilities or certified safety assessments.</p>
      {run?.data?.sources?.business?.errors?.map(reason => <p role="alert" className="demo-note" key={reason}>{reason}</p>)}
    </section>
    <div className="demo-result-grid"><section className="panel demo-map"><div className="panel-title"><h2>3 · Routes on the map</h2><span>Solid green = selected · dashed = alternatives</span></div>
      <OperationsMap vessels={((maritime ? run?.data?.vessels : []) || []).filter(v => v.mmsi === reference?.mmsi)} routes={candidates} recommended={recommended} ports={(maritime ? run?.intelligence?.results?.port?.output?.ports as Record<string, unknown>[] : []) || []} onVessel={() => {}} onSegment={() => {}} />
    </section><section className="panel demo-recommendation"><span className="eyebrow">4 · GOOGLE OR-TOOLS CP-SAT</span><h2>{selected.length ? candidates.length === 1 ? "One supported route found" : "Best among compared candidates" : component?.status === "INFEASIBLE" ? "No route meets your limits" : "Awaiting optimization"}</h2>
      {selected.map(r => <div key={r.candidate_id}><h3>{r.planning_label || "Selected route"}</h3><p>{fmt(r.distance_km / 1.852, 1)} nautical miles · {fmt(r.duration_hours, 1)} estimated hours</p><p>{candidates.length === 1 ? "Only one geographic candidate was available. Its checkable limits were evaluated; there was no alternative to compare." : "Selected because it has the lowest weighted normalized objective among the compared routes satisfying the checkable limits."}</p><strong>Objective: {fmt(r.objective_value, 4)}</strong></div>)}
      <p>Metrics used: {used.join(", ") || "Waiting for evidence"}.</p><p>Excluded: {excluded.join(", ") || "See metric coverage"}.</p>
      {unchecked.map(v => <p key={v} className="demo-note">{v}</p>)}
      {run?.status === "NO_FEASIBLE_ROUTE" && <p>{run.validation?.reasons.join(" · ")}</p>}
      <p className="demo-note">Geographic planning recommendation only. No ERP, purchase order or booking is part of this workflow.</p>
      {ai && <details><summary>AI agent synthesis · {human(ai.status || "UNAVAILABLE")}</summary><p>{ai.summary || ai.reason}</p><small>{ai.model || "No model used"} · AI explains; it cannot change the solver’s numbers.</small></details>}
    </section></div>
    <section className="panel demo-evidence"><div className="panel-title"><h2>5 · Where the data came from</h2><span>Source time and coverage stay visible</span></div>
      {context?.selection && <details className="demo-note"><summary>Port and vessel catalog · static, checked {context.selection.verified_at}</summary><p>{context.selection.coordinate_note}</p><p>{context.selection.vessel_note}</p>{[context.selection.departure, context.selection.destination].map((port, i) => port && <p key={i}>{port.name} · {port.locode} · {port.lat}, {port.lon} · <a href={port.source} target="_blank" rel="noreferrer">Port code source</a> · <a href={port.coordinate_source || context.selection?.coordinate_source} target="_blank" rel="noreferrer">Coordinate source</a></p>)}{context.selection.vessel && <a href={context.selection.vessel.source} target="_blank" rel="noreferrer">Vessel identity source</a>}</details>}
      {maritime && Object.entries(run?.data?.sources || {}).filter(([key]) => ["selected_vessel", "weather", "ocean", "waves", "ports", "business"].includes(key)).map(([key, source]) => <div className="demo-source" key={key}><strong>{key === "business" ? "Routing data" : key}</strong><span>{source.source}<small> · {sourceAgents[key]}</small></span><span>{human(source.quality)}</span><small>{source.observed_at ? new Date(source.observed_at).toLocaleString() : "No source-valid time available"}{typeof source.payload?.usable_points === "number" && <><br />{source.payload.usable_points} / {String(source.payload.requested_points)} usable model points</>}</small></div>)}
      <p className="demo-note">Weather, waves and currents are current numerical-model snapshots at sampled route positions. At least three common valid fractions are required for a metric to enter the comparison. PortWatch shows activity, not physical congestion. Fuel is excluded unless a real vessel-specific curve is supplied.</p>
    </section>
  </div>;
}
