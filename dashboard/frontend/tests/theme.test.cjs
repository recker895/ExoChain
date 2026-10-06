const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const postcss = require("postcss");

const stylesheet = fs.readFileSync(
  path.join(__dirname, "../src/app/globals.css"), "utf8",
);
const tree = postcss.parse(stylesheet);
const tokens = {};
tree.walkRules(":root", rule => {
  rule.walkDecls(declaration => { tokens[declaration.prop] = declaration.value; });
});

function luminance(hex) {
  const channels = hex.replace("#", "").match(/.{2}/g).map(value => {
    const channel = parseInt(value, 16) / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}
function contrast(foreground, background) {
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

test("visual theme remains dark and primary text has accessible contrast", () => {
  assert.equal(tokens["color-scheme"], "dark");
  assert.ok(luminance(tokens["--bg"]) < 0.03);
  for (const foreground of [tokens["--text"], tokens["--muted"], "#a9bbd5"]) {
    for (const background of [tokens["--panel"], "#17253a", "#1b2b43"]) {
      assert.ok(contrast(foreground, background) >= 4.5, `${foreground} on ${background}`);
    }
  }
});

test("primary action text is readable across both gradient endpoints", () => {
  assert.ok(contrast("#09282d", "#65edc9") >= 4.5);
  assert.ok(contrast("#09282d", "#65d8f4") >= 4.5);
});

test("result and partial states retain different semantic accents", () => {
  const accents = {};
  tree.walkRules(rule => {
    if ([".demo-agent.status-success", ".demo-agent.status-partial"].includes(rule.selector)) {
      rule.walkDecls("border-left-color", declaration => {
        accents[rule.selector] = declaration.value;
      });
    }
  });
  assert.equal(accents[".demo-agent.status-success"], "var(--accent)");
  assert.equal(accents[".demo-agent.status-partial"], "var(--warn)");
});

test("visual theme preserves narrow-screen rules and reduced-motion support", () => {
  const queries = [];
  tree.walkAtRules("media", rule => queries.push(rule.params));
  assert.ok(queries.some(query => query.includes("450px")));
  assert.ok(queries.some(query => query.includes("prefers-reduced-motion")));
  assert.ok(stylesheet.includes("textarea:focus-visible"));
});
