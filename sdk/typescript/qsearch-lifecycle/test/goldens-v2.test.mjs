// Golden vectors for the v2 controls, produced by the Python implementation
// (protocol/qsearch-lifecycle/v2/fixtures/). Missing fixtures FAIL this file;
// they are never skipped and an empty list is a failure too.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { LifecycleClient, LifecycleClientError, canonicalJson } from "../dist/index.js";
import { canonicalizeReplacement } from "../dist/canonical.js";
import { createHmacSha256Signer, createSha256 } from "../dist/webcrypto.js";
import { fakeTimer, rejection } from "../test-support/helpers.mjs";

const FIXTURES = fileURLToPath(new URL("../../../../protocol/qsearch-lifecycle/v2/fixtures/", import.meta.url));
const OPERATIONS = ["status", "refresh", "cancel", "replace"];
const LF = String.fromCharCode(10);
const b64 = (text) => new Uint8Array(Buffer.from(text, "base64"));
const hex = (bytes) => Buffer.from(bytes).toString("hex");
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const stripPrefix = (digest) => String(digest).replace(/^sha256:/, "");

function load(name, listKey) {
  if (!existsSync(FIXTURES)) {
    assert.fail(`v2 golden fixture directory missing: ${FIXTURES} (expected Python-derived fixtures; see v2/CONTRACT.md)`);
  }
  const path = FIXTURES + name;
  if (!existsSync(path)) assert.fail(`v2 golden fixture file missing: ${path}`);
  const document = JSON.parse(readFileSync(path, "utf8"));
  const list = document[listKey];
  assert.ok(Array.isArray(list), `${name}: "${listKey}" must be an array`);
  assert.ok(list.length > 0, `${name}: "${listKey}" is empty; an empty golden set proves nothing`);
  return list;
}

/** A client granted every operation, whose transport serves `respond` and records every request. */
function client({ base_url = "http://127.0.0.1:8080", clock = 1_700_000_000, nonce = "0123456789abcdef0123456789abcdef", signer, respond }) {
  const calls = [];
  const instance = new LifecycleClient({
    baseUrl: base_url,
    capabilities: OPERATIONS,
    sha256: createSha256(),
    transport: async (request) => {
      calls.push(request);
      return respond(request);
    },
    signer: signer ?? (async () => new Uint8Array(32)),
    clock: () => clock,
    nonce: () => nonce,
    setTimer: fakeTimer().setTimer,
  });
  return { instance, calls };
}

/** Invoke the case's operation exactly as a caller would. */
function invoke(instance, c, label) {
  assert.ok(OPERATIONS.includes(c.operation), `${label}: unknown operation ${c.operation}`);
  if (c.operation !== "replace") return instance[c.operation](c.request_digest);
  assert.equal(typeof c.replacement_b64, "string", `${label}: replace needs replacement_b64`);
  return instance.replace(c.request_digest, b64(c.replacement_b64));
}

function assertError(error, expected, label, { optional = false } = {}) {
  assert.ok(error instanceof LifecycleClientError, `${label}: ${error}`);
  assert.equal(error.code, expected.code, `${label}: code`);
  const pairs = [
    ["field", "field"],
    ["retryable", "retryable"],
    ["next_action", "nextAction"],
    ["http_status", "httpStatus"],
  ];
  for (const [wireKey, prop] of pairs) {
    if (optional && !Object.prototype.hasOwnProperty.call(expected, wireKey)) continue;
    assert.ok(Object.prototype.hasOwnProperty.call(expected, wireKey), `${label}: expected_error.${wireKey} missing`);
    assert.equal(error[prop], expected[wireKey], `${label}: ${wireKey}`);
  }
  assert.equal(error.message, `q-search lifecycle failed (${expected.code})`, `${label}: message`);
}

test("canonical.v2.json: canonical bytes and descriptor digests equal Python's", async () => {
  for (const v of load("canonical.v2.json", "vectors")) {
    const label = `canonical/${v.name}`;
    const input = b64(v.input_b64);
    assert.equal(hex(canonicalJson(input)), hex(b64(v.canonical_b64)), `${label}: canonical bytes`);
    const replacement = canonicalizeReplacement(input);
    assert.equal(hex(replacement.descriptor), hex(b64(v.descriptor_canonical_b64)), `${label}: descriptor bytes`);
    assert.equal(`sha256:${sha256(replacement.descriptor)}`, v.descriptor_digest, `${label}: descriptor digest`);
    // The client sends exactly these bytes.
    const { instance, calls } = client({
      respond: () => {
        throw new Error("capture only");
      },
    });
    await rejection(instance.replace("sha256:" + "ab".repeat(32), input));
    assert.equal(calls.length, 1, `${label}: one transport call`);
    assert.equal(hex(calls[0].body), hex(b64(v.canonical_b64)), `${label}: request body`);
  }
});

test("canonical.v2.json: every refusal is INVALID_REQUEST (field replacement) with zero transport calls", async () => {
  for (const r of load("canonical.v2.json", "refusals")) {
    const label = `canonical-refusal/${r.name} (python_client: ${JSON.stringify(r.python_client)})`;
    const { instance, calls } = client({ respond: () => assert.fail(`${label}: transport must not be called`) });
    const error = await rejection(instance.replace("sha256:" + "ab".repeat(32), b64(r.input_b64)));
    assertError(error, { code: "INVALID_REQUEST", field: "replacement", retryable: false, next_action: "NONE", http_status: null }, label);
    assert.equal(calls.length, 0, `${label}: zero transport calls`);
  }
});

test("controls-signing.v2.json: url, exact headers, body, message bytes, mac and header match Python", async () => {
  for (const v of load("controls-signing.v2.json", "vectors")) {
    const label = `signing/${v.name}`;
    const key = new Uint8Array(Buffer.from(v.key_hex, "hex"));
    const webcrypto = createHmacSha256Signer(key);
    const messages = [];
    const macs = [];
    const { instance, calls } = client({
      base_url: v.base_url,
      clock: v.clock,
      nonce: v.nonce,
      signer: async (message) => {
        messages.push(message);
        const mac = await webcrypto(message);
        macs.push(mac);
        return mac;
      },
      respond: () => {
        throw new Error("capture only");
      },
    });
    const error = await rejection(invoke(instance, v, label));
    assert.equal(error.code, "TRANSPORT_UNAVAILABLE", label);
    assert.equal(error.nextAction, v.method === "GET" ? "POLL_STATUS" : "MANUAL_RECONCILIATION", `${label}: next_action`);
    assert.equal(calls.length, 1, `${label}: one transport call`);
    const [request] = calls;
    assert.equal(request.url, v.url, `${label}: url`);
    assert.equal(request.method, v.method, `${label}: method`);
    assert.ok(v.url.endsWith(v.path), `${label}: url ends with path`);
    assert.deepStrictEqual({ ...request.headers }, v.headers, `${label}: headers (exact)`);
    assert.equal(hex(request.body), hex(b64(v.body_b64)), `${label}: body bytes`);
    assert.equal(sha256(request.body), stripPrefix(v.body_sha256), `${label}: body sha256`);
    assert.equal(hex(messages[0]), hex(b64(v.message_b64)), `${label}: message bytes`);
    assert.equal(hex(macs[0]), v.mac_hex, `${label}: mac`);
    assert.equal(request.headers["X-Aether-Qsearch-Auth"], v.header, `${label}: header`);
    const text = new TextDecoder().decode(b64(v.message_b64));
    assert.equal(text, [v.method, v.path, stripPrefix(v.body_sha256), String(Math.floor(v.clock)), v.nonce].join(LF), `${label}: framing`);
  }
});

test("controls-histories.v2.json: returned envelopes deep-equal Python's", async () => {
  for (const h of load("controls-histories.v2.json", "cases")) {
    const label = `history/${h.name}`;
    const { instance, calls } = client({ respond: async () => ({ status: h.http_status, body: b64(h.body_b64) }) });
    const state = await invoke(instance, h, label);
    assert.equal(calls.length, 1, `${label}: one transport call`);
    assert.deepStrictEqual(state, h.expected, label);
  }
});

test("controls-errors.v2.json: mapped client errors match Python", async () => {
  for (const c of load("controls-errors.v2.json", "cases")) {
    const label = `error/${c.name}`;
    const { instance, calls } = client({ respond: async () => ({ status: c.http_status, body: b64(c.body_b64) }) });
    const error = await rejection(invoke(instance, c, label));
    assert.ok(calls.length <= 1, `${label}: at most one transport call`);
    assertError(error, c.expected_error, label);
  }
});

test("controls-negative.v2.json: strict-contract refusals", async () => {
  for (const c of load("controls-negative.v2.json", "cases")) {
    const label = `negative/${c.name} (python_client: ${JSON.stringify(c.python_client)})`;
    const pad = c.pad_trailing_spaces ?? 0;
    assert.ok(Number.isSafeInteger(pad) && pad >= 0, `${label}: pad_trailing_spaces`);
    const raw = b64(c.body_b64);
    const body = new Uint8Array(raw.length + pad);
    body.set(raw);
    body.fill(0x20, raw.length);
    const { instance, calls } = client({ respond: async () => ({ status: c.http_status, body }) });
    const error = await rejection(invoke(instance, c, label));
    assert.ok(calls.length <= 1, `${label}: at most one transport call`);
    assertError(error, c.expected_error, label, { optional: true });
  }
});
