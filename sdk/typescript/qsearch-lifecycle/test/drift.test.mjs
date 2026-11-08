// Drift guard: the constants embedded in the SDK must equal the frozen
// contract files. Any edit to protocol/qsearch-lifecycle/v1 turns this red.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { JOB_STATE_SPEC } from "../dist/validate.js";
import * as wire from "../dist/wire.js";
import { PROTOCOL_DIR, readProtocolJson } from "../test-support/helpers.mjs";

const wireJson = readProtocolJson("wire.v1.json");
const schema = readProtocolJson("job-state.v1.schema.json");
const errorSchema = readProtocolJson("lifecycle-error.v1.schema.json");

test("wire.v1.json lists equal the embedded constants", () => {
  assert.deepStrictEqual([...wire.REMOTE_CODES], wireJson.remote_codes);
  assert.deepStrictEqual([...wire.REMOTE_FIELDS], wireJson.remote_fields);
  assert.deepStrictEqual([...wire.NEXT_ACTIONS], wireJson.next_actions);
  assert.deepStrictEqual([...wire.PROVIDER_STATUSES], wireJson.provider_statuses);
  assert.deepStrictEqual([...wire.CLIENT_ERROR_CODES], wireJson.client_error_codes);
  assert.deepStrictEqual([...wire.UNSUPPORTED_OPERATIONS], wireJson.unsupported_operations);
  assert.deepStrictEqual([...wire.SUPPORTED_OPERATIONS], Object.keys(wireJson.operations));
});

test("wire.v1.json scalars equal the embedded constants", () => {
  assert.equal(wire.CONTRACT_ID, wireJson.contract);
  assert.equal(wire.JOB_STATE_SCHEMA_ID, wireJson.job_state_schema);
  assert.equal(wire.AUTH_HEADER, wireJson.auth.header);
  assert.equal(wire.AUTH_SCHEME, wireJson.auth.scheme);
  assert.equal(wireJson.auth.algorithm, "HMAC-SHA256");
  assert.equal(wireJson.auth.message, "METHOD\nPATH\nBODY_SHA256_HEX\nTS\nNONCE");
  assert.equal(wire.MAC_BYTES, wireJson.auth.mac_bytes);
  assert.equal(wire.MIN_KEY_BYTES, wireJson.auth.min_key_bytes);
  assert.equal(wire.NONCE_PATTERN.source, wireJson.auth.nonce_pattern);
  assert.equal(wire.EMPTY_BODY_SHA256, wireJson.auth.empty_body_sha256);
  assert.equal(wire.REQUEST_DIGEST_PATTERN.source, wireJson.request_digest_pattern);
  assert.deepStrictEqual({ [wire.ACCEPT_HEADER]: wire.ACCEPT_VALUE }, wireJson.request_headers);
  assert.equal(wire.MAX_RESPONSE_BYTES, wireJson.max_response_bytes);
  assert.equal(wire.MAX_JSON_DEPTH, wireJson.max_json_depth);
  assert.equal(wire.MAX_SAFE_INTEGER, wireJson.max_safe_integer);
  assert.equal(wire.DEFAULT_TIMEOUT_MS, wireJson.default_timeout_s * 1000);
  const status = wireJson.operations.status;
  assert.equal(wire.STATUS_METHOD, status.method);
  assert.equal(status.body, "");
  assert.equal(wire.STATUS_PATH_PREFIX + "{request_digest}", status.path_template);
});

test("the wire file has no member this SDK does not account for", () => {
  assert.deepStrictEqual(Object.keys(wireJson).sort(), [
    "auth",
    "client_error_codes",
    "contract",
    "default_timeout_s",
    "job_state_schema",
    "max_json_depth",
    "max_response_bytes",
    "max_safe_integer",
    "next_actions",
    "operations",
    "provider_statuses",
    "remote_codes",
    "remote_fields",
    "request_digest_pattern",
    "request_headers",
    "unsupported_operations",
  ]);
  assert.deepStrictEqual(Object.keys(wireJson.auth).sort(), [
    "algorithm",
    "empty_body_sha256",
    "header",
    "mac_bytes",
    "message",
    "min_key_bytes",
    "nonce_pattern",
    "scheme",
    "window_s",
  ]);
});

const resolve = (node) => (node?.$ref ? schema.$defs[node.$ref.split("/").pop()] : node);

test("job-state schema: required members equal the validator's, every object closed", () => {
  assert.equal(schema.properties.schema.const, JOB_STATE_SPEC.schema);
  assert.equal(schema.additionalProperties, false);
  assert.deepStrictEqual([...JOB_STATE_SPEC.required[""]], schema.required);
  assert.deepStrictEqual(Object.keys(schema.properties), schema.required);
  const sections = Object.entries(schema.properties).filter(([, s]) => s.type === "object");
  assert.deepStrictEqual(sections.map(([k]) => k).sort(), Object.keys(JOB_STATE_SPEC.required).filter((k) => k !== "").sort());
  for (const [name, section] of sections) {
    assert.equal(section.additionalProperties, false, name);
    assert.deepStrictEqual([...JOB_STATE_SPEC.required[name]], section.required, name);
    assert.deepStrictEqual(Object.keys(section.properties), section.required, name);
  }
});

test("job-state schema: enums equal the validator's", () => {
  const at = (path) => path.split(".").reduce((node, key) => resolve(node.properties[key]), schema);
  for (const [path, values] of Object.entries(JOB_STATE_SPEC.enums)) {
    assert.deepStrictEqual([...values], at(path).enum, path);
  }
  const enumPaths = [];
  const walk = (node, path) => {
    if (node.enum) enumPaths.push(path);
    for (const [k, child] of Object.entries(node.properties ?? {})) walk(child, path ? `${path}.${k}` : k);
  };
  walk(schema, "");
  assert.deepStrictEqual(enumPaths.sort(), Object.keys(JOB_STATE_SPEC.enums).sort(), "every schema enum is checked");
  assert.deepStrictEqual([...wire.NEXT_ACTIONS].sort(), [...JOB_STATE_SPEC.enums.next_action].sort());
  assert.deepStrictEqual([...wire.PROVIDER_STATUSES], [...JOB_STATE_SPEC.enums["provider.status"]]);
});

test("job-state schema: patterns, amount range and defs equal the validator's", () => {
  assert.equal(schema.properties.request.properties.request_id.pattern, JOB_STATE_SPEC.patterns["request.request_id"]);
  assert.equal(schema.$defs.digest.pattern, JOB_STATE_SPEC.patterns.digest);
  assert.equal(schema.$defs.identifier.pattern, JOB_STATE_SPEC.patterns.identifier);
  assert.deepStrictEqual(
    { minimum: schema.$defs.amount.minimum, maximum: schema.$defs.amount.maximum },
    { ...JOB_STATE_SPEC.amount },
  );
  assert.deepStrictEqual(Object.keys(schema.$defs).sort(), ["amount", "digest", "identifier", "nullableAmount", "nullableDigest", "nullableIdentifier"]);
});

test("job-state schema: invariant lists equal the validator's (I1-I8 count pinned)", () => {
  const descriptions = schema.allOf.map((rule) => rule.description.split(" ")[0]);
  assert.deepStrictEqual(descriptions, ["I1", "I2", "I3", "I4", "I5", "I6a", "I6b", "I6c", "I6d", "I6e", "I7", "I8"]);
  const [, , i3, i4, i5] = schema.allOf;
  assert.deepStrictEqual([...JOB_STATE_SPEC.invariants.pendingStates], i3.then.properties.state.enum);
  assert.deepStrictEqual([...JOB_STATE_SPEC.invariants.pendingReservations], i3.then.properties.usage.properties.reservation_status.enum);
  assert.deepStrictEqual([...JOB_STATE_SPEC.invariants.nonTerminalStatuses], i4.then.properties.execution.properties.status.enum);
  assert.deepStrictEqual([...JOB_STATE_SPEC.invariants.terminalStatuses], i5.then.properties.execution.properties.status.enum);
});

test("lifecycle-error schema members are exactly the ones the client maps", () => {
  assert.deepStrictEqual(Object.keys(errorSchema.properties).sort(), ["code", "error", "field", "next_action", "retryable"]);
  assert.equal(errorSchema.additionalProperties, undefined, "error body is open on purpose");
});

test("CONTRACT.md still states the caps this SDK enforces", () => {
  const contract = readFileSync(PROTOCOL_DIR + "CONTRACT.md", "utf8");
  for (const phrase of ["1,048,576 bytes", "nesting depth at most 64", "9007199254740991", "q-search lifecycle failed (<code>)", "never follows a redirect"]) {
    assert.ok(contract.includes(phrase), phrase);
  }
});
