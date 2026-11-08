// Drift guard for the additive v2 contract: the constants and behaviour the
// SDK embeds must equal protocol/qsearch-lifecycle/v2/wire.v2.json. Any edit
// to the frozen v2 files turns this red.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { LifecycleClient } from "../dist/index.js";
import * as wire from "../dist/wire.js";
import { DIGEST, errorShape, fakeTimer, options, rejection, thrown } from "../test-support/helpers.mjs";

const V2_DIR = fileURLToPath(new URL("../../../../protocol/qsearch-lifecycle/v2/", import.meta.url));
const wireV2 = JSON.parse(readFileSync(V2_DIR + "wire.v2.json", "utf8"));

test("wire.v2.json scalars and lists equal the embedded constants", () => {
  assert.equal(wire.CONTRACT_V2_ID, wireV2.contract);
  assert.equal(wire.CONTRACT_ID, wireV2.extends);
  assert.equal(wire.AUTH_HEADER, wireV2.auth_header);
  assert.equal(wire.JOB_STATE_SCHEMA_ID, wireV2.job_state_schema);
  assert.equal(wire.EMPTY_BODY_SHA256, wireV2.empty_body_sha256);
  assert.equal(wire.MAX_CANONICAL_DEPTH, wireV2.max_canonical_depth);
  assert.deepStrictEqual([...wire.CAPABILITIES], wireV2.capabilities);
  assert.deepStrictEqual([...wire.DEFAULT_CAPABILITIES], wireV2.default_capabilities);
  assert.deepStrictEqual([...wire.CLIENT_ERROR_CODES_V2], wireV2.client_error_codes);
  assert.deepStrictEqual(Object.keys(wireV2.operations).sort(), [...wire.CAPABILITIES]);
});

test("wire.v2.json operations: methods, paths, headers, bodies and transport failure equal the client's", async () => {
  const replacement = '{"descriptor":{}}';
  for (const [name, op] of Object.entries(wireV2.operations)) {
    const calls = [];
    const client = new LifecycleClient(
      options({
        capabilities: [name],
        sha256: async () => new Uint8Array(32),
        setTimer: fakeTimer().setTimer,
        transport: async (request) => {
          calls.push(request);
          throw new Error("capture only");
        },
      }),
    );
    const error = await rejection(name === "replace" ? client.replace(DIGEST, replacement) : client[name](DIGEST));
    assert.equal(calls.length, 1, name);
    const [request] = calls;
    assert.equal(request.method, op.method, name);
    assert.ok(request.url.endsWith(op.path_template.replace("{request_digest}", DIGEST)), name);
    const { [wireV2.auth_header]: auth, ...rest } = request.headers;
    assert.ok(auth.startsWith("v1 ts="), name);
    assert.deepStrictEqual(rest, wireV2.request_headers[op.method], name);
    if (op.body === "empty") assert.equal(request.body.length, 0, name);
    else assert.equal(Buffer.from(request.body).toString(), replacement, name);
    const failure = wireV2.transport_failure[op.method];
    assert.deepStrictEqual(
      { code: error.code, retryable: error.retryable, nextAction: error.nextAction },
      { code: failure.code, retryable: failure.retryable, nextAction: failure.next_action },
      name,
    );
  }
  assert.equal(wireV2.operations.replace.lineage_replaces_request_digest, "request_digest");
});

test("wire.v2.json local refusals equal the client's", async () => {
  const client = new LifecycleClient(options({ capabilities: ["replace"], sha256: async () => new Uint8Array(32) }));
  const capability = errorShape(thrown(() => client.requireCapability("cancel")));
  assert.deepStrictEqual({ code: capability.code, field: capability.field }, wireV2.local_refusals.capability);
  const replacement = errorShape(await rejection(client.replace(DIGEST, "[]")));
  assert.deepStrictEqual({ code: replacement.code, field: replacement.field }, wireV2.local_refusals.replacement);
});

test("the v2 wire file has no member this SDK does not account for", () => {
  assert.deepStrictEqual(Object.keys(wireV2).sort(), [
    "auth_header",
    "capabilities",
    "client_error_codes",
    "contract",
    "default_capabilities",
    "empty_body_sha256",
    "extends",
    "job_state_schema",
    "local_refusals",
    "max_canonical_depth",
    "operations",
    "request_headers",
    "transport_failure",
  ]);
});

test("CONTRACT v2 still states the rules this SDK implements", () => {
  const contract = readFileSync(V2_DIR + "CONTRACT.md", "utf8");
  for (const phrase of [
    "The default set is `{status}`",
    "Nesting depth at most 64",
    "A float that overflows to",
    "**exactly one** transport call",
    "A cancel response is the server's state **after** the request",
    "`retryable: true`, `next_action: MANUAL_RECONCILIATION`",
  ]) {
    assert.ok(contract.includes(phrase), phrase);
  }
});
