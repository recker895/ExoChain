const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("typescript");

test("production same-origin API retains authorization", async () => {
  const filename = path.resolve(__dirname, "../src/app/api.ts");
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const previous = process.env.NEXT_PUBLIC_API_URL;
  const originalFetch = global.fetch;
  try {
    process.env.NEXT_PUBLIC_API_URL = "";
    const instance = new Module(filename, module);
    instance._compile(compiled, filename);
    assert.equal(instance.exports.API, "");
    global.fetch = async (url, options) => {
      assert.equal(url, "/api/v1/telemetry");
      assert.equal(options.headers.Authorization, "Bearer test-only");
      return { ok: true, json: async () => ({ status: "UNAVAILABLE" }) };
    };
    assert.deepEqual(await instance.exports.api("/api/v1/telemetry", "test-only"), { status: "UNAVAILABLE" });
  } finally {
    global.fetch = originalFetch;
    if (previous === undefined) delete process.env.NEXT_PUBLIC_API_URL;
    else process.env.NEXT_PUBLIC_API_URL = previous;
  }
});
