import assert from "node:assert/strict";
import { test } from "node:test";

import { LifecycleClientError, parseStrictJson, validateJobState } from "../dist/index.js";
import { DIGEST, OTHER_DIGEST, clone, devSet, queued, readProtocolJson } from "../test-support/helpers.mjs";
import { compileSchema } from "../test-support/schema-eval.mjs";

const schemaValid = compileSchema(readProtocolJson("job-state.v1.schema.json"));

function accepts(value) {
  try {
    validateJobState(value, DIGEST);
    return true;
  } catch (error) {
    assert.ok(error instanceof LifecycleClientError);
    assert.equal(error.code, "INVALID_RESPONSE");
    assert.equal(error.nextAction, "MANUAL_RECONCILIATION");
    assert.equal(error.retryable, false);
    return false;
  }
}

/** Oracle: the frozen schema plus the digest binding of CONTRACT.md section 2.2. */
const oracle = (value) => schemaValid(value) && value?.request?.request_digest === DIGEST;

function invalidField(value, digest = DIGEST) {
  try {
    validateJobState(value, digest, 200);
  } catch (error) {
    assert.equal(error.code, "INVALID_RESPONSE");
    assert.equal(error.httpStatus, 200);
    return error.field;
  }
  assert.fail("expected INVALID_RESPONSE");
}

function isDeepFrozen(value) {
  if (typeof value !== "object" || value === null) return true;
  return Object.isFrozen(value) && Object.values(value).every(isDeepFrozen);
}

test("every dev-set envelope is schema-valid and accepted unchanged, deep-frozen", () => {
  for (const [name, envelope] of Object.entries(devSet())) {
    assert.ok(schemaValid(envelope), `${name} must satisfy the frozen schema`);
    const state = validateJobState(envelope, DIGEST);
    assert.deepStrictEqual(state, envelope, name);
    assert.ok(isDeepFrozen(state), `${name} must be deep-frozen`);
    assert.notStrictEqual(state, envelope, "result is a fresh object, not the input");
  }
});

test("dev-set histories stay distinct after validation", () => {
  const states = Object.entries(devSet()).map(([name, e]) => [name, validateJobState(e, DIGEST)]);
  const serialised = new Set(states.map(([, s]) => JSON.stringify(s)));
  assert.equal(serialised.size, states.length);
  for (let i = 0; i < states.length; i += 1) {
    for (let j = i + 1; j < states.length; j += 1) {
      assert.notDeepStrictEqual(states[i][1], states[j][1], `${states[i][0]} vs ${states[j][0]}`);
    }
  }
});

test("zero is server data only: confirmed zero stays 0, unknown usage stays null", () => {
  const set = devSet();
  const zero = validateJobState(set["confirmed-zero"], DIGEST);
  assert.equal(zero.usage.settled_quantum_seconds, 0);
  assert.equal(zero.accounting.status, "SETTLED");
  const unknown = validateJobState(set["unknown-zero-held"], DIGEST);
  assert.equal(unknown.usage.settled_quantum_seconds, null);
  assert.equal(unknown.usage.held_quantum_seconds, null);
  assert.equal(unknown.accounting.status, "PENDING");
  const pending = validateJobState(set["accounting-pending"], DIGEST);
  assert.equal(pending.usage.held_quantum_seconds, null);
});

const ODD_VALUES = [null, true, false, 0, -1, 1.5, -0.5, 9007199254740991, 9007199254740992, "", "X", "sha256:", [], {}, Number.NaN, Number.POSITIVE_INFINITY];

function* leafPaths(value, path = []) {
  for (const key of Object.keys(value)) {
    const child = value[key];
    yield [...path, key];
    if (typeof child === "object" && child !== null) yield* leafPaths(child, [...path, key]);
  }
}

function setPath(root, path, mutate) {
  const copy = clone(root);
  let parent = copy;
  for (const key of path.slice(0, -1)) parent = parent[key];
  mutate(parent, path[path.length - 1]);
  return copy;
}

function enumValuesAt(schema, path) {
  let node = schema;
  for (const key of path) {
    node = node?.properties?.[key];
    if (node?.$ref) node = schema.$defs[node.$ref.split("/").pop()];
  }
  return node?.enum ?? [];
}

test("differential: single-member mutations agree with the schema oracle", () => {
  const schema = readProtocolJson("job-state.v1.schema.json");
  let checked = 0;
  for (const [name, envelope] of Object.entries(devSet())) {
    const mutants = [];
    for (const path of leafPaths(envelope)) {
      mutants.push(setPath(envelope, path, (p, k) => delete p[k]));
      for (const odd of [...ODD_VALUES, ...enumValuesAt(schema, path)]) {
        mutants.push(setPath(envelope, path, (p, k) => (p[k] = odd)));
      }
      mutants.push(setPath(envelope, path.slice(0, -1).concat(["extra_member"]), (p, k) => (p[k] = null)));
    }
    mutants.push(setPath(envelope, ["request", "request_digest"], (p, k) => (p[k] = OTHER_DIGEST)));
    for (const mutant of mutants) {
      assert.equal(accepts(mutant), oracle(mutant), `${name}: ${JSON.stringify(mutant)}`);
      checked += 1;
    }
  }
  assert.ok(checked > 2000, `checked ${checked}`);
});

test("differential: invariant cross-products I1-I8 agree with the schema oracle", () => {
  const base = queued();
  const executionStatuses = ["ACCEPTED", "SUBMITTED_UNKNOWN", "QUEUED", "RUNNING", "COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN"];
  const outcomes = ["SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN", "UNKNOWN"];
  const accounting = ["PENDING", "SETTLED", "NOT_APPLICABLE"];
  const reservations = ["HELD", "UNKNOWN", "SETTLED", "NOT_RESERVED"];
  const amounts = [null, 0, 5.5];
  let accepted = 0;
  let checked = 0;
  for (const status of executionStatuses)
    for (const outcome of outcomes)
      for (const terminal of [true, false])
        for (const acc of accounting)
          for (const reservation of reservations)
            for (const held of amounts)
              for (const settled of amounts) {
                const v = clone(base);
                v.execution = { status, outcome, terminal };
                v.accounting.status = acc;
                v.usage = { reservation_status: reservation, held_quantum_seconds: held, settled_quantum_seconds: settled };
                if (acc === "SETTLED") v.state = "SETTLED";
                if (acc !== "PENDING") v.next_action = "NONE";
                if (status === "CANCELLED") v.cancellation.confirmed = true;
                const got = accepts(v);
                assert.equal(got, oracle(v), JSON.stringify(v));
                accepted += got ? 1 : 0;
                checked += 1;
              }
  const states = ["ACCEPTED", "SUBMITTED_UNKNOWN", "QUEUED", "RUNNING", "COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN", "QUEUE_EXPIRED", "CANCELLATION_REQUESTED", "ACCOUNTING_PENDING", "ACCOUNTING_EXPIRED", "SETTLED"];
  const nextActions = ["NONE", "POLL_STATUS", "REQUEST_CANCEL", "RECONCILE", "MANUAL_RECONCILIATION"];
  for (const set of [devSet().queued, devSet()["settled-positive"], devSet().cancelled])
    for (const state of states)
      for (const nextAction of nextActions)
        for (const requested of [true, false])
          for (const requestedAt of [null, 7])
            for (const confirmed of [true, false])
              for (const confirmedAt of [null, 9]) {
                const v = clone(set);
                v.state = state;
                v.next_action = nextAction;
                v.cancellation = { requested, requested_at: requestedAt, confirmed, confirmed_at: confirmedAt };
                const got = accepts(v);
                assert.equal(got, oracle(v), JSON.stringify(v));
                accepted += got ? 1 : 0;
                checked += 1;
              }
  assert.ok(accepted > 20 && accepted < checked, `accepted ${accepted} of ${checked}`);
});

test("field is a static schema path or null, never response content", () => {
  const extraTop = { ...queued(), "secret-looking-key": 1 };
  assert.equal(invalidField(extraTop), null);
  const extraUsage = queued();
  extraUsage.usage["secret-looking-key"] = 1;
  assert.equal(invalidField(extraUsage), "usage");
  const boolAmount = queued();
  boolAmount.usage.held_quantum_seconds = true;
  assert.equal(invalidField(boolAmount), "usage.held_quantum_seconds");
  const numberBool = queued();
  numberBool.execution.terminal = 0;
  assert.equal(invalidField(numberBool), "execution.terminal");
  const badTarget = queued();
  badTarget.request.target = "has space";
  assert.equal(invalidField(badTarget), "request.target");
  const i1 = devSet()["settled-positive"];
  i1.usage.held_quantum_seconds = 1;
  assert.equal(invalidField(i1), "usage.held_quantum_seconds");
});

test("Python-parity checks report field null (schema id, digest binding, next_action)", () => {
  const wrongSchema = { ...queued(), schema: "nano.qsearch.job-state.v2" };
  assert.equal(invalidField(wrongSchema), null);
  assert.equal(invalidField(queued(OTHER_DIGEST)), null);
  const badRequest = { ...queued(), request: [] };
  assert.equal(invalidField(badRequest), null);
  const badAction = { ...queued(), next_action: "RETRY" };
  assert.equal(invalidField(badAction), null);
  assert.equal(invalidField([]), null);
  assert.equal(invalidField(null), null);
});

test("objects from JSON.parse with __proto__, accessors, symbols or class instances are refused", () => {
  const text = JSON.stringify(queued()).replace('"schema":', '"__proto__":{"x":1},"schema":');
  assert.equal(accepts(JSON.parse(text)), false);
  const accessor = queued();
  Object.defineProperty(accessor, "state", { get: () => "QUEUED", enumerable: true });
  assert.equal(accepts(accessor), false);
  const symbol = queued();
  symbol.usage[Symbol("x")] = 1;
  assert.equal(accepts(symbol), false);
  class Shape {}
  const instance = Object.assign(new Shape(), queued());
  assert.equal(accepts(instance), false);
  assert.equal({}.x, undefined);
});

test("parseStrictJson output (null-prototype objects) validates to plain objects", () => {
  const parsed = parseStrictJson(JSON.stringify(devSet().cancelled));
  assert.equal(Object.getPrototypeOf(parsed), null);
  const state = validateJobState(parsed, DIGEST);
  assert.equal(Object.getPrototypeOf(state), Object.prototype);
  assert.deepStrictEqual(state, devSet().cancelled);
});

test("a malformed expected digest is IDENTITY, not a validation result", () => {
  let error;
  try {
    validateJobState(queued(), "sha256:ABC");
  } catch (e) {
    error = e;
  }
  assert.equal(error?.code, "IDENTITY");
  assert.equal(error?.field, "request_digest");
});
