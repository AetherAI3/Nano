// Shared offline test helpers: synthetic envelopes, fake transport, fake timer.
// Every key, digest and host below is synthetic.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const PROTOCOL_DIR = fileURLToPath(new URL("../../../../protocol/qsearch-lifecycle/v1/", import.meta.url));
export const readProtocolJson = (name) => JSON.parse(readFileSync(PROTOCOL_DIR + name, "utf8"));

export const DIGEST = "sha256:" + "ab".repeat(32);
export const OTHER_DIGEST = "sha256:" + "cd".repeat(32);
export const BASE_URL = "https://qsearch.test.invalid:8443";
export const STATUS_URL = `${BASE_URL}/qsearch/v2/requests/${DIGEST}`;
export const NONCE = "0123456789abcdef0123456789abcdef";
export const CLOCK_S = 1_700_000_000.75;
export const FAKE_MAC = new Uint8Array(32).fill(0xab);

const enc = new TextEncoder();
export const bytes = (text) => enc.encode(text);
export const jsonBytes = (value) => enc.encode(JSON.stringify(value));

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** Base: a queued job with a held reservation (schema-valid). */
export function queued(digest = DIGEST) {
  return {
    schema: "nano.qsearch.job-state.v1",
    request: { request_id: "req-queued_1", request_digest: digest, target: "ibm_fez" },
    lineage: { replaces_request_digest: null, replacement_request_digest: null },
    state: "QUEUED",
    provider: { job_id: "d1abc-job.01", status: "QUEUED" },
    execution: { status: "QUEUED", outcome: "UNKNOWN", terminal: false },
    accounting: { status: "PENDING" },
    usage: { reservation_status: "HELD", held_quantum_seconds: 3600.0, settled_quantum_seconds: null },
    cancellation: { requested: false, requested_at: null, confirmed: false, confirmed_at: null },
    deadlines: { queue_at: 1_700_003_600.0, queue_expired: false, accounting_at: null, accounting_expired: false },
    next_action: "POLL_STATUS",
  };
}

function edit(base, mutate) {
  const value = clone(base);
  mutate(value);
  return value;
}

/** Synthetic dev set: one good envelope per lifecycle shape; all must stay distinct. */
export function devSet(digest = DIGEST) {
  const q = queued(digest);
  const settled = (outcomeStatus, settledSeconds) => (v) => {
    v.state = "SETTLED";
    v.provider.status = outcomeStatus === "CANCELLED" ? "CANCELLED" : "DONE";
    v.execution = { status: outcomeStatus, outcome: outcomeStatus, terminal: true };
    v.accounting.status = "SETTLED";
    v.usage = { reservation_status: "SETTLED", held_quantum_seconds: 0.0, settled_quantum_seconds: settledSeconds };
    v.deadlines.accounting_at = 1_700_090_000.0;
    v.next_action = "NONE";
  };
  return {
    queued: q,
    running: edit(q, (v) => {
      v.state = "RUNNING";
      v.provider.status = "RUNNING";
      v.execution.status = "RUNNING";
    }),
    completed: edit(q, (v) => {
      v.state = "ACCOUNTING_PENDING";
      v.provider.status = "DONE";
      v.execution = { status: "COMPLETED", outcome: "UNKNOWN", terminal: true };
      v.deadlines.accounting_at = 1_700_090_000.0;
      v.next_action = "RECONCILE";
    }),
    failed: edit(q, (v) => {
      v.state = "ACCOUNTING_PENDING";
      v.provider.status = "ERROR";
      v.execution = { status: "FAILED", outcome: "FAILED", terminal: true };
      v.deadlines.accounting_at = 1_700_090_000.0;
      v.next_action = "RECONCILE";
    }),
    cancelled: edit(q, (v) => {
      settled("CANCELLED", 12.5)(v);
      v.cancellation = { requested: true, requested_at: 1_700_000_100.0, confirmed: true, confirmed_at: 1_700_000_200.5 };
    }),
    "accounting-pending": edit(q, (v) => {
      v.state = "ACCOUNTING_EXPIRED";
      v.provider.status = "DONE";
      v.execution = { status: "SUCCEEDED", outcome: "SUCCEEDED", terminal: true };
      v.usage.held_quantum_seconds = null; // unknown stays null, never 0
      v.deadlines.accounting_at = 1_700_000_500.0;
      v.deadlines.accounting_expired = true;
      v.next_action = "MANUAL_RECONCILIATION";
    }),
    "settled-positive": edit(q, settled("SUCCEEDED", 42.5)),
    "confirmed-zero": edit(q, settled("SUCCEEDED", 0)),
    "unknown-zero-held": edit(q, (v) => {
      v.state = "ACCOUNTING_PENDING";
      v.provider.status = "DONE";
      v.execution = { status: "COMPLETED", outcome: "UNKNOWN", terminal: true };
      v.usage = { reservation_status: "UNKNOWN", held_quantum_seconds: null, settled_quantum_seconds: null };
      v.deadlines.accounting_at = 1_700_090_000.0;
      v.next_action = "RECONCILE";
    }),
  };
}

/** Records every call; responds with `respond(request, index)` (sync value, promise, or throw). */
export function fakeTransport(respond) {
  const calls = [];
  const transport = (request) => {
    calls.push(request);
    return respond(request, calls.length - 1);
  };
  return { transport, calls };
}

export const respondWith = (status, body) => async () => ({ status, body });

/** Manual timer: nothing fires unless the test calls `fire()`. */
export function fakeTimer() {
  const timers = [];
  const setTimer = (callback, ms) => {
    const timer = { callback, ms, cancelled: false };
    timers.push(timer);
    return () => {
      timer.cancelled = true;
    };
  };
  const fire = () => {
    for (const timer of timers) if (!timer.cancelled) timer.callback();
  };
  return { setTimer, timers, fire };
}

export function options(overrides = {}) {
  return {
    baseUrl: BASE_URL,
    transport: fakeTransport(respondWith(200, jsonBytes(queued()))).transport,
    signer: async () => FAKE_MAC.slice(),
    clock: () => CLOCK_S,
    nonce: () => NONCE,
    setTimer: fakeTimer().setTimer,
    ...overrides,
  };
}

/** Await `promise` and return the rejection reason (fails if it resolves). */
export async function rejection(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("expected a rejection");
}

export function thrown(fn) {
  try {
    fn();
  } catch (error) {
    return error;
  }
  throw new Error("expected a throw");
}

export const tick = () => new Promise((resolve) => setImmediate(resolve));

export function errorShape(error) {
  return {
    code: error.code,
    field: error.field,
    retryable: error.retryable,
    nextAction: error.nextAction,
    httpStatus: error.httpStatus,
  };
}
