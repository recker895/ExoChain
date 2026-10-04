const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

// Compile the real TSX component in memory; no generated test source/artifacts.
function loadComponent(filename) {
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
  }).outputText;
  const componentModule = new Module(filename, module);
  componentModule.filename = filename;
  componentModule.paths = Module._nodeModulePaths(path.dirname(filename));
  const originalRequire = componentModule.require.bind(componentModule);
  componentModule.require = (name) => {
    if (name.startsWith(".")) {
      for (const ext of [".ts", ".tsx"]) {
        const target = path.resolve(path.dirname(filename), name + ext);
        if (fs.existsSync(target)) return loadComponent(target);
      }
    }
    return originalRequire(name);
  };
  componentModule._compile(compiled, filename);
  return componentModule.exports;
}
const Replenishment = loadComponent(
  path.resolve(__dirname, "../src/app/Replenishment.tsx"),
).default;
function render(changes = {}) {
  const run = {
    status: "DRAFT",
    request: { operation: "REPLENISHMENT", budget_usd: 35000 },
    business_inputs: {},
    ...changes,
  };
  return renderToStaticMarkup(
    React.createElement(Replenishment, {
      run,
      canReconcile: true,
      busy: false,
      onReconcile: () => {},
    }),
  );
}

test("asynchronous draft/running states are not labelled failed validation", () => {
  for (const status of ["DRAFT", "RUNNING"]) {
    const html = render({
      status,
      validation: {},
      data: {},
      optimization: {},
      decisions: {},
      intelligence: {},
    });
    assert.ok(html.includes(`Run lifecycle: ${status}`));
    assert.ok(html.includes("Validation: NOT RUN"));
    assert.ok(!html.includes("Validation: BLOCKED"));
    assert.ok(html.includes("NOT SUBMITTED"));
  }
});

test("blocked budget proof is not presented as an executable plan", () => {
  const html = render({
    status: "BLOCKED",
    validation: { valid: false, reasons: ["BUDGET_LIMIT"] },
    optimization: {
      components: {
        inventory: {
          explanation: {
            feasibility_analysis: {
              status: "OPTIMAL",
              minimum_feasible_budget_usd: 117227,
              scope: "Diagnostic only",
            },
          },
        },
      },
    },
  });
  assert.ok(html.includes("Validation: BLOCKED"));
  assert.ok(html.includes("117227"));
  assert.ok(html.includes("not an executable plan"));
  assert.ok(html.includes("NOT SUBMITTED"));
});

test("pending human approval is not ERP execution", () => {
  const html = render({
    status: "PENDING_APPROVAL",
    validation: { valid: true, reasons: [] },
    approval: { status: "PENDING_APPROVAL", mode: "HUMAN" },
    execution_lifecycle: { stage: "PENDING_APPROVAL", history: [] },
  });
  assert.ok(html.includes("Approval: PENDING_APPROVAL"));
  assert.ok(html.includes("NOT SUBMITTED"));
  assert.ok(!html.includes("Reconcile with ERP"));
});

test("reconciled ERP result retains its actual status and disables duplicate reconciliation", () => {
  const html = render({
    status: "EXECUTED",
    execution: { status: "EXECUTED", external_reference: "TEST-ONLY-PO" },
    execution_lifecycle: { stage: "RECONCILED", command: {}, history: [] },
  });
  assert.ok(html.includes("Lifecycle: RECONCILED"));
  assert.ok(html.includes("TEST-ONLY-PO"));
  assert.match(html, /<button[^>]*disabled=""[^>]*>Reconcile with ERP/);
});

test("all evidence workspaces can render the API's empty draft envelopes", () => {
  const workspaces = loadComponent(
    path.resolve(__dirname, "../src/app/Workspaces.tsx"),
  );
  const run = {
    status: "DRAFT",
    request: { operation: "REPLENISHMENT" },
    business_inputs: {},
    data: {},
    intelligence: {},
    decisions: {},
    optimization: {},
    validation: {},
  };
  for (const name of [
    "DecisionBrief",
    "IntelligenceView",
    "DecisionsView",
    "AgentsView",
    "ScenarioView",
    "SystemView",
  ]) {
    assert.doesNotThrow(() =>
      renderToStaticMarkup(
        React.createElement(workspaces[name], {
          run,
          token: "",
          onAgent: () => {},
          onConfigure: () => {},
        }),
      ),
    );
  }
});

test("selected supplier candidates reflect optimized procurement identities", () => {
  const { DecisionsView } = loadComponent(
    path.resolve(__dirname, "../src/app/Workspaces.tsx"),
  );
  const run = {
    status: "PENDING_APPROVAL",
    request: {},
    validation: { valid: true },
    decisions: {
      executions: [
        {
          agent_id: "decision.supplier",
          output: {
            candidates: [
              {
                candidate_id: "supplier:q:v1",
                entity_id: "q",
                decision_type: "supplier",
                action: "PROCURE",
              },
            ],
          },
        },
      ],
    },
    optimization: {
      components: { procurement: { selected: [{ id: "q", units: 2 }] } },
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(DecisionsView, { run }),
  );
  assert.ok(html.includes("VALIDATED"));
});

test("budget infeasibility does not mislabel valid business data as missing", () => {
  const { DecisionBrief } = loadComponent(
    path.resolve(__dirname, "../src/app/Workspaces.tsx"),
  );
  const run = {
    status: "BLOCKED",
    request: { operation: "REPLENISHMENT" },
    data: { sources: { business: { quality: "VALID" } } },
    validation: { valid: false, reasons: ["BUDGET_LIMIT"] },
  };
  const html = renderToStaticMarkup(
    React.createElement(DecisionBrief, { run }),
  );
  assert.ok(html.includes("DATA VALID"));
  assert.ok(html.includes("Plan blocked by validation"));
});
