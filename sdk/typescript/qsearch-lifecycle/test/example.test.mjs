import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const packageDir = fileURLToPath(new URL("..", import.meta.url));

test("examples/offline-status.mjs runs offline through the package's public entry points", () => {
  const output = execFileSync(process.execPath, ["examples/offline-status.mjs"], { cwd: packageDir, encoding: "utf8" });
  assert.match(output, /state=RUNNING execution=RUNNING next_action=POLL_STATUS/);
  assert.match(output, /settled_quantum_seconds=null/);
  assert.match(output, /supports\(cancel\)=false transport_calls=1/);
  assert.match(output, /offline-status: ok/);
});

test("package manifest: private, ESM, zero runtime dependencies, exact typescript pin", () => {
  const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(manifest.name, "@nano/qsearch-lifecycle");
  assert.equal(manifest.private, true);
  assert.equal(manifest.type, "module");
  assert.equal(manifest.engines.node, ">=20");
  assert.equal(manifest.dependencies, undefined);
  assert.deepStrictEqual(Object.keys(manifest.devDependencies), ["typescript"]);
  assert.match(manifest.devDependencies.typescript, /^\d+\.\d+\.\d+$/);
  assert.deepStrictEqual(Object.keys(manifest.exports), [".", "./webcrypto", "./fetch"]);
});

test("runtime sources touch no environment, storage, network or logging APIs", () => {
  const sources = ["bytes", "client", "errors", "fetch", "index", "json", "types", "validate", "webcrypto", "wire"].map((name) =>
    readFileSync(new URL(`../dist/${name}.js`, import.meta.url), "utf8"),
  );
  for (const banned of [/process\.env/, /localStorage/, /sessionStorage/, /indexedDB/, /document\.cookie/, /\bconsole\./, /require\(/, /from "node:/, /globalThis\.fetch/, /\bimport\(/]) {
    for (const source of sources) assert.ok(!banned.test(source), `${banned} found in dist`);
  }
});
