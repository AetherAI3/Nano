// Golden vectors produced by the Python implementation
// (protocol/qsearch-lifecycle/v1/fixtures/). Missing fixtures FAIL this file;
// they are never skipped and an empty vector list is a failure too.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

import { LifecycleClientError, StatusClient } from "../dist/index.js";
import { createHmacSha256Signer } from "../dist/webcrypto.js";
import { PROTOCOL_DIR, fakeTimer, rejection } from "../test-support/helpers.mjs";

const FIXTURES = PROTOCOL_DIR + "fixtures/";
const b64 = (text) => new Uint8Array(Buffer.from(text, "base64"));
const hex = (bytes) => Buffer.from(bytes).toString("hex");
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const stripPrefix = (digest) => String(digest).replace(/^sha256:/, "");

function load(name, listKey) {
  const path = FIXTURES + name;
  if (!existsSync(FIXTURES)) {
    assert.fail(`golden fixture directory missing: ${FIXTURES} (expected Python-derived fixtures; see CONTRACT.md)`);
  }
  if (!existsSync(path)) assert.fail(`golden fixture file missing: ${path}`);
  const document = JSON.parse(readFileSync(path, "utf8"));
  const list = document[listKey];
  assert.ok(Array.isArray(list), `${name}: "${listKey}" must be an array`);
  assert.ok(list.length > 0, `${name}: "${listKey}" is empty; an empty golden set proves nothing`);
  return list;
}

/** A client whose transport serves `respond` and records every request. */
function client({ base_url = "http://127.0.0.1:8080", clock = 1_700_000_000, nonce = "0123456789abcdef0123456789abcdef", signer, respond }) {
  const calls = [];
  const instance = new StatusClient({
    baseUrl: base_url,
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

function assertError(error, expected, label, { optional = false } = {}) {
  assert.ok(error instanceof LifecycleClientError, label);
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

test("signing.v1.json: url, exact headers, message bytes, mac and header match Python", async () => {
  const vectors = load("signing.v1.json", "vectors");
  for (const v of vectors) {
    const label = `signing/${v.name}`;
    assert.equal(v.method, "GET", label);
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
    const error = await rejection(instance.status(v.request_digest));
    assert.equal(error.code, "TRANSPORT_UNAVAILABLE", label);
    assert.equal(calls.length, 1, `${label}: one transport call`);
    const [request] = calls;
    assert.equal(request.url, v.url, `${label}: url`);
    assert.equal(request.method, v.method, `${label}: method`);
    assert.deepStrictEqual({ ...request.headers }, v.headers, `${label}: headers (exact)`);
    assert.equal(hex(messages[0]), hex(b64(v.message_b64)), `${label}: message bytes`);
    assert.equal(hex(macs[0]), v.mac_hex, `${label}: mac`);
    assert.equal(request.headers["X-Aether-Qsearch-Auth"], v.header, `${label}: header`);
    // Internal consistency of the vector itself.
    assert.equal(String(v.ts), String(Math.floor(v.clock)), `${label}: ts = floor(clock)`);
    assert.ok(v.url.endsWith(v.path), `${label}: url ends with path`);
    const text = new TextDecoder().decode(b64(v.message_b64));
    assert.equal(text, [v.method, v.path, stripPrefix(v.body_sha256), String(v.ts), v.nonce].join("\n"), `${label}: framing`);
  }
});

test("status-histories.v1.json: parsed states deep-equal Python's and stay distinct", async () => {
  const histories = load("status-histories.v1.json", "histories");
  const results = [];
  for (const h of histories) {
    const label = `history/${h.name}`;
    const body = b64(h.body_b64);
    if (h.body_sha256 !== undefined) assert.equal(sha256(body), stripPrefix(h.body_sha256), `${label}: fixture body hash`);
    const { instance, calls } = client({ respond: async () => ({ status: h.http_status, body }) });
    const state = await instance.status(h.request_digest);
    assert.equal(calls.length, 1, label);
    assert.deepStrictEqual(state, h.expected, label);
    results.push([h.name, state]);
  }
  const distinct = new Set(results.map(([, s]) => JSON.stringify(s)));
  assert.equal(distinct.size, results.length, "every history must produce a distinct state");
  const expectedDistinct = new Set(histories.map((h) => JSON.stringify(h.expected)));
  assert.equal(expectedDistinct.size, histories.length, "fixture histories themselves are distinct");
});

test("errors.v1.json: mapped client errors match Python", async () => {
  const cases = load("errors.v1.json", "cases");
  for (const c of cases) {
    const label = `error/${c.name}`;
    const { instance, calls } = client({ respond: async () => ({ status: c.http_status, body: b64(c.body_b64) }) });
    const error = await rejection(instance.status(c.request_digest));
    assert.ok(calls.length <= 1, `${label}: at most one transport call`);
    assertError(error, c.expected_error, label);
  }
});

test("negative.v1.json: strict-contract refusals", async () => {
  const cases = load("negative.v1.json", "cases");
  for (const c of cases) {
    const label = `negative/${c.name}`;
    const pad = c.pad_trailing_spaces ?? 0;
    assert.ok(Number.isSafeInteger(pad) && pad >= 0, `${label}: pad_trailing_spaces`);
    const raw = b64(c.body_b64);
    const body = new Uint8Array(raw.length + pad);
    body.set(raw);
    body.fill(0x20, raw.length);
    const { instance, calls } = client({ respond: async () => ({ status: c.http_status, body }) });
    const error = await rejection(instance.status(c.request_digest));
    assert.ok(calls.length <= 1, `${label}: at most one transport call`);
    assertError(error, c.expected_error, label, { optional: true });
  }
});
