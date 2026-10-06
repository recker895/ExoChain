const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

// Render the real component; only the browser-only map is replaced in this SSR test.
const filename = path.resolve(__dirname, "../src/app/MaritimeDemo.tsx");
const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021, esModuleInterop: true },
}).outputText;
const componentModule = new Module(filename, module);
componentModule.filename = filename;
componentModule.paths = Module._nodeModulePaths(path.dirname(filename));
const originalRequire = componentModule.require.bind(componentModule);
const selectionFilename = path.resolve(__dirname, "../src/app/maritimeSelection.ts");
const selectionModule = new Module(selectionFilename, module);
selectionModule.filename = selectionFilename;
selectionModule.paths = Module._nodeModulePaths(path.dirname(selectionFilename));
selectionModule._compile(ts.transpileModule(fs.readFileSync(selectionFilename, "utf8"), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021, esModuleInterop: true},
}).outputText, selectionFilename);
const { catalog, maritimeInput } = selectionModule.exports;
let inspectHandlers = false;
componentModule.require = name => name === "next/dynamic" ? { __esModule: true, default: () => () => React.createElement("div", null, "Map") }
  : name === "./maritimeSelection" ? selectionModule.exports
  : name === "react" ? {...React, useState: initial => inspectHandlers ? [initial, () => {}] : React.useState(initial)} : originalRequire(name);
componentModule._compile(compiled, filename);
const { default: MaritimeDemo, MaritimeRunForm } = componentModule.exports;

test("route planner uses professional visible terminology", () => {
  const html = renderToStaticMarkup(React.createElement(MaritimeDemo, { liveEvents: [], onStart: () => {} }));
  const visibleText = html.replace(/<[^>]*>/g, " ");
  assert.doesNotMatch(visibleText, /\b(demo|college|demonstration)\b/i);
  assert.match(visibleText, /MARITIME ROUTE PLANNING/);
});

const request = { operation: "TRANSPORT", demo_mode: true, shipment_ids: ["demo"], max_risk: 0.5,
  vessel_reference: { shipment_id: "demo", mmsi: "563275100", imo: "9502946", vessel_name: "MAERSK ENSHI" },
  weights: {distance: 1, time: 1}, use_ai_explanation: false };
test("form has searchable sourced catalogs and speed assumption without ERP or mandatory budget", () => {
  const html = renderToStaticMarkup(React.createElement(MaritimeRunForm, {
    request, setRequest: () => {}, origin: "SGSIN", setOrigin: () => {}, destination: "NLRTM",
    setDestination: () => {}, speed: 14, setSpeed: () => {}, advanced: () => {},
  }));
  assert.ok(html.includes('value="9502946"') && html.includes("MMSI 563275100"));
  assert.ok(html.includes('aria-label="Search vessels"') && html.includes('aria-label="Departure port"'));
  assert.ok(html.includes("Other supported port") && html.includes("static catalog"));
  assert.ok(html.includes("your assumption") && !html.includes("operator credential"));
  assert.ok(!html.includes("Budget / USD") && !html.includes("Shipment IDs"));
});
test("route comparison uses actual solution, excludes absent evidence and shows no purchase flow", () => {
  const route = { candidate_id: "actual-result", planning_label: "Computed route", distance_km: 1852,
    duration_hours: 100, geometry: [], risk_score: null, weather_penalty: null, objective_value: 2 };
  const run = { run_id: "r", status: "ROUTE_READY", request, audit: [], data: {sources: {}, executions: []},
    decisions: { routes: [route], executions: [] }, intelligence: {executions: []},
    optimization: { components: {route: {selected: [route], alternatives: [], rejected: [],
      explanation: {used_metrics: ["distance", "time"], excluded_metrics: ["fuel", "risk"]}}} } };
  const html = renderToStaticMarkup(React.createElement(MaritimeDemo, {run, liveEvents: [], onStart: () => {}}));
  assert.ok(html.includes("Computed route") && html.includes("Selected by OR-Tools"));
  assert.ok(html.includes("Not available") && html.includes("distance, time") && html.includes("fuel, risk"));
  assert.ok(html.includes("No ERP, purchase order or booking"));
  assert.ok(html.includes("no alternative to compare"));
});

function descendants(node) {
  if (!node || typeof node !== "object") return [];
  return [node, ...React.Children.toArray(node.props?.children).flatMap(descendants)];
}
test("actual vessel dropdown handler populates IMO/MMSI/name and changes submitted identity", () => {
  let changed;
  inspectHandlers = true;
  try {
    const tree = MaritimeRunForm({request, setRequest: r => changed = r, origin: "SGSIN", destination: "NLRTM",
      setOrigin() {}, setDestination() {}, speed: 14, setSpeed() {}, advanced() {}});
    const select = descendants(tree).find(n => n.type === "select" && n.props["aria-label"] === "Vessel");
    select.props.onChange({target: {value: "9893890"}});
    const body = maritimeInput(changed, "HKHKG", "KRPUS", 16);
    assert.equal(body.request.vessel_reference.vessel_name, "EVER ACE");
    assert.equal(body.request.vessel_reference.mmsi, "352986146");
    assert.equal(body.request.vessel_reference.imo, "9893890");
    assert.deepEqual(body.request.shipment_ids, ["MARITIME-HKHKG-KRPUS-9893890"]);
    assert.equal(body.arcnautical_route.shipment_id, body.request.vessel_reference.shipment_id);
    const portComponent = descendants(tree).find(n => typeof n.type === "function" && n.props.label === "Departure port");
    let departure;
    const portTree = portComponent.type({...portComponent.props, onChange: value => departure = value});
    descendants(portTree).find(n => n.type === "select").props.onChange({target: {value: "USLAX"}});
    assert.equal(departure, "USLAX");
    assert.equal(maritimeInput(changed, departure, "USNYC", 16).arcnautical_route.origin_locode, "USLAX");
  } finally { inspectHandlers = false; }
});
test("different ports really change request scope and impossible selections fail before submission", () => {
  const first = maritimeInput(request, "SGSIN", "NLRTM", 14);
  const second = maritimeInput(request, " cnshg ", "krpus", 18);
  assert.equal(second.arcnautical_route.origin_locode, "CNSHG");
  assert.equal(second.arcnautical_route.destination_locode, "KRPUS");
  assert.equal(second.arcnautical_route.planning_speed_knots, 18);
  assert.notEqual(first.request.shipment_ids[0], second.request.shipment_ids[0]);
  assert.throws(() => maritimeInput(request, "sgsin", "SGSIN", 14), /must differ/);
  assert.throws(() => maritimeInput(request, "", "NLRTM", 14), /Choose two ports/);
  assert.throws(() => maritimeInput(request, "CNSHA", "NLRTM", 14), /airport/);
  assert.throws(() => maritimeInput(request, "SGSIN", "NLRTM", NaN), /speed/);
});
test("catalog has 20 sourced ports including India and 10 unique IMO identities, never invented vessel positions", () => {
  assert.equal(catalog.ports.length, 20);
  assert.equal(catalog.ports.filter(p => p.country === "India").length, 6);
  assert.equal(new Set(catalog.vessels.map(v => v.imo)).size, 10);
  for (const v of catalog.vessels) {
    assert.match(v.source, /^https:\/\//);
    assert.equal(v.verified_at, "2026-10-06");
    assert.equal(v.position, undefined);
    assert.equal(v.speed, undefined);
  }
  assert.equal(catalog.vessels.find(v => v.imo === "9811000").mmsi, "636026627");
});
test("all 19 task cards are visible and execution is distinct from data availability", () => {
  const html = renderToStaticMarkup(React.createElement(MaritimeDemo, {liveEvents: [], onStart: () => {}}));
  assert.equal((html.match(/class="demo-agent status-waiting"/g) || []).length, 19);
  assert.ok(html.includes("not 19 independent") || html.includes("not 19 independent language models") || html.includes("not 19"));
});
