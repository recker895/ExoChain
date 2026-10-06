const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("typescript");
const React = require("react");

function mountMap(suppliedRoute) {
  const effects = [], states = [];
  let refs = 0, stateIndex = 0, instance;
  class MapStub {
    constructor() { instance = this; this.sources = {}; this.callbacks = {}; this.fits = []; }
    on(event, ...args) { if (event === "style.load") this.callbacks[event] = args[0]; }
    addControl() {}
    addLayer() {}
    addSource(id) { this.sources[id] = {setData: value => { this.sources[id].data = value; }}; }
    getSource(id) { return this.sources[id]; }
    fitBounds(bounds) { this.fits.push(bounds.points); }
    remove() {}
  }
  class Bounds { constructor() {this.points = [];} extend(point) { this.points.push(point); return this; } isEmpty() { return !this.points.length; } }
  const maplibre = {Map: MapStub, NavigationControl: class {}, LngLatBounds: Bounds, setWorkerUrl() {}};
  const hooks = {...React,
    useEffect: callback => effects.push(callback),
    useRef: initial => ({current: refs++ === 0 ? {} : initial}),
    // Reproduce a retained "ready" state during map recreation.
    useState: initial => [stateIndex++ === 0 ? true : initial, value => states.push(value)],
  };
  const filename = path.resolve(__dirname, "../src/app/OperationsMap.tsx");
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021},
  }).outputText;
  const componentModule = new Module(filename, module);
  componentModule.filename = filename;
  componentModule.paths = Module._nodeModulePaths(path.dirname(filename));
  const original = componentModule.require.bind(componentModule);
  componentModule.require = name => name === "react" ? hooks : name === "maplibre-gl" ? maplibre : name.endsWith(".css") ? {} : original(name);
  componentModule._compile(compiled, filename);
  const route = suppliedRoute || {candidate_id: "test-geometry", geometry: [
    {longitude: 103.85, latitude: 1.29}, {longitude: 4.5, latitude: 51.9}], segments: []};
  componentModule.exports.default({vessels: [], routes: [route], recommended: new Set([route.candidate_id]),
    ports: [], onVessel() {}, onSegment() {}});
  effects[0]();
  effects[1]();
  return {effects, instance, states, route};
}

test("map recreation ignores retained ready state until route sources exist", () => {
  const {effects, instance, states} = mountMap();
  assert.equal(states[0], false);
  assert.deepEqual(instance.sources, {});
  assert.doesNotThrow(() => effects[2]());
});
test("another selected port pair changes rendered geometry and map bounds", () => {
  const route = {candidate_id: "hong-kong-busan", geometry: [
    {longitude: 114.15, latitude: 22.29}, {longitude: 129.04, latitude: 35.1}], segments: []};
  const {effects, instance} = mountMap(route);
  instance.callbacks["style.load"]();
  effects[2]();
  const coordinates = route.geometry.map(p => [p.longitude, p.latitude]);
  assert.deepEqual(instance.sources.routes.data.features[0].geometry.coordinates, coordinates);
  assert.deepEqual(instance.fits[0], coordinates);
});
test("loaded map draws the supplied candidate geometry even without segments", () => {
  const {effects, instance, route} = mountMap();
  instance.callbacks["style.load"]();
  effects[2]();
  const features = instance.sources.routes.data.features;
  assert.equal(features.length, 1);
  assert.equal(features[0].properties.id, route.candidate_id);
  assert.equal(features[0].properties.recommended, true);
  assert.deepEqual(features[0].geometry.coordinates, route.geometry.map(p => [p.longitude, p.latitude]));
});

for (const direction of [1, -1]) {
  test(`date-line route draws continuously and fits the same world copy (${direction})`, () => {
    const geometry = [
      {longitude: direction * 179.9, latitude: 20},
      {longitude: -direction * 179.9, latitude: 21},
      {longitude: -direction * 179.7, latitude: 22},
    ];
    const original = JSON.stringify(geometry);
    const route = {candidate_id: `dateline-${direction}`, geometry, segments: [
      {id: "first", geometry: geometry.slice(0, 2)},
      {id: "second", geometry: geometry.slice(1)},
    ]};
    const {effects, instance} = mountMap(route);
    instance.callbacks["style.load"]();
    effects[2]();
    const lines = instance.sources.routes.data.features.map(f => f.geometry.coordinates);
    for (const points of lines) {
      assert.ok(Math.abs(points[1][0] - points[0][0]) < 1);
    }
    assert.deepEqual(lines[0].at(-1), lines[1][0]);
    const bounds = instance.fits[0];
    assert.ok(Math.abs(bounds.at(-1)[0] - bounds[0][0]) < 1);
    const endpoints = instance.sources.endpoints.data.features.map(f => f.geometry.coordinates);
    assert.deepEqual(endpoints, [bounds[0], bounds.at(-1)]);
    assert.equal(JSON.stringify(geometry), original);
  });
}
