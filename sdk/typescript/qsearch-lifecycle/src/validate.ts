/**
 * Hand-written runtime validator equal to `job-state.v1.schema.json`
 * (closed objects, enums, patterns, nullability, number range, strict
 * booleans) plus the cross-field invariants I1-I8.
 *
 * It only checks server-reported data. It never derives, upgrades or
 * re-validates zero finality, never coerces `null` to `0`, and performs no
 * budget or accounting arithmetic. `test/drift.test.mjs` asserts that
 * `JOB_STATE_SPEC` equals the frozen schema file.
 */
import { LifecycleClientError, invalidResponse } from "./errors.js";
import type { JobStateV1 } from "./types.js";
import { JOB_STATE_SCHEMA_ID, MAX_SAFE_INTEGER, NEXT_ACTIONS, PROVIDER_STATUSES, REQUEST_DIGEST_PATTERN } from "./wire.js";

const STATES = Object.freeze([
  "ACCEPTED",
  "SUBMITTED_UNKNOWN",
  "QUEUED",
  "RUNNING",
  "COMPLETED",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "NOT_RUN",
  "QUEUE_EXPIRED",
  "CANCELLATION_REQUESTED",
  "ACCOUNTING_PENDING",
  "ACCOUNTING_EXPIRED",
  "SETTLED",
] as const);
const EXECUTION_STATUSES = Object.freeze([
  "ACCEPTED",
  "SUBMITTED_UNKNOWN",
  "QUEUED",
  "RUNNING",
  "COMPLETED",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "NOT_RUN",
] as const);
const OUTCOMES = Object.freeze(["SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN", "UNKNOWN"] as const);
const ACCOUNTING_STATUSES = Object.freeze(["PENDING", "SETTLED", "NOT_APPLICABLE"] as const);
const RESERVATION_STATUSES = Object.freeze(["HELD", "UNKNOWN", "SETTLED", "NOT_RESERVED"] as const);
const SCHEMA_NEXT_ACTIONS = Object.freeze(["NONE", "POLL_STATUS", "REQUEST_CANCEL", "RECONCILE", "MANUAL_RECONCILIATION"] as const);

/** I3: states allowed while accounting is PENDING. */
const PENDING_STATES = Object.freeze([
  "ACCEPTED",
  "SUBMITTED_UNKNOWN",
  "QUEUED",
  "RUNNING",
  "QUEUE_EXPIRED",
  "CANCELLATION_REQUESTED",
  "ACCOUNTING_PENDING",
  "ACCOUNTING_EXPIRED",
] as const);
const PENDING_RESERVATIONS = Object.freeze(["HELD", "UNKNOWN"] as const);
/** I4 / I5 */
const NON_TERMINAL_STATUSES = Object.freeze(["ACCEPTED", "SUBMITTED_UNKNOWN", "QUEUED", "RUNNING"] as const);
const TERMINAL_STATUSES = Object.freeze(["COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN"] as const);
/** I6a-e: decided execution status -> required outcome (terminal is always true). */
const DECIDED_OUTCOME: ReadonlyMap<string, string> = new Map([
  ["SUCCEEDED", "SUCCEEDED"],
  ["FAILED", "FAILED"],
  ["CANCELLED", "CANCELLED"],
  ["NOT_RUN", "NOT_RUN"],
  ["COMPLETED", "UNKNOWN"],
]);

const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,80}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9_.:-]{1,200}$/;

const REQUIRED = Object.freeze({
  "": Object.freeze(["schema", "request", "lineage", "state", "provider", "execution", "accounting", "usage", "cancellation", "deadlines", "next_action"]),
  request: Object.freeze(["request_id", "request_digest", "target"]),
  lineage: Object.freeze(["replaces_request_digest", "replacement_request_digest"]),
  provider: Object.freeze(["job_id", "status"]),
  execution: Object.freeze(["status", "outcome", "terminal"]),
  accounting: Object.freeze(["status"]),
  usage: Object.freeze(["reservation_status", "held_quantum_seconds", "settled_quantum_seconds"]),
  cancellation: Object.freeze(["requested", "requested_at", "confirmed", "confirmed_at"]),
  deadlines: Object.freeze(["queue_at", "queue_expired", "accounting_at", "accounting_expired"]),
});
type Section = Exclude<keyof typeof REQUIRED, "">;

/** Machine-checkable description of what this validator enforces (drift-tested). */
export const JOB_STATE_SPEC = Object.freeze({
  schema: JOB_STATE_SCHEMA_ID,
  required: REQUIRED,
  enums: Object.freeze({
    state: STATES,
    "provider.status": PROVIDER_STATUSES,
    "execution.status": EXECUTION_STATUSES,
    "execution.outcome": OUTCOMES,
    "accounting.status": ACCOUNTING_STATUSES,
    "usage.reservation_status": RESERVATION_STATUSES,
    next_action: SCHEMA_NEXT_ACTIONS,
  }),
  patterns: Object.freeze({
    "request.request_id": REQUEST_ID_PATTERN.source,
    digest: REQUEST_DIGEST_PATTERN.source,
    identifier: IDENTIFIER_PATTERN.source,
  }),
  amount: Object.freeze({ minimum: 0, maximum: MAX_SAFE_INTEGER }),
  invariants: Object.freeze({
    pendingStates: PENDING_STATES,
    pendingReservations: PENDING_RESERVATIONS,
    nonTerminalStatuses: NON_TERMINAL_STATUSES,
    terminalStatuses: TERMINAL_STATUSES,
  }),
});

/** Internal failure carrying a static schema path (or null); never content. */
class Invalid extends Error {
  constructor(readonly field: string | null) {
    super("invalid job state");
  }
}

type Plain = Readonly<Record<string, unknown>>;

function isPlainObject(value: unknown): value is Plain {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === null || proto === Object.prototype;
}

/** Own data property value, or `undefined` when absent or an accessor. */
function own(object: Plain, key: string): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  return descriptor !== undefined && "value" in descriptor ? descriptor.value : undefined;
}

function hasDataMember(object: Plain, key: string): boolean {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  return descriptor !== undefined && "value" in descriptor && descriptor.enumerable === true;
}

/** Closed object: exactly the listed members, each an own enumerable data property. */
function closed(value: unknown, path: keyof typeof REQUIRED): Plain {
  const field = path === "" ? null : path;
  if (!isPlainObject(value)) throw new Invalid(field);
  if (Object.getOwnPropertySymbols(value).length !== 0) throw new Invalid(field);
  const required = REQUIRED[path];
  if (Object.getOwnPropertyNames(value).length !== required.length) throw new Invalid(field);
  for (const key of required) {
    if (!hasDataMember(value, key)) throw new Invalid(field);
  }
  return value;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], field: string): T {
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) throw new Invalid(field);
  return value as T;
}

function bool(value: unknown, field: string): boolean {
  if (value !== true && value !== false) throw new Invalid(field);
  return value;
}

function amount(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > MAX_SAFE_INTEGER) {
    throw new Invalid(field);
  }
  return value;
}

function nullableAmount(value: unknown, field: string): number | null {
  return value === null ? null : amount(value, field);
}

function matching(value: unknown, pattern: RegExp, field: string): string {
  if (typeof value !== "string" || !pattern.test(value)) throw new Invalid(field);
  return value;
}

function nullableMatching(value: unknown, pattern: RegExp, field: string): string | null {
  return value === null ? null : matching(value, pattern, field);
}

function shape(value: unknown): JobStateV1 {
  const root = closed(value, "");
  const sections = {} as Record<Section, Plain>;
  for (const name of ["request", "lineage", "provider", "execution", "accounting", "usage", "cancellation", "deadlines"] as const) {
    sections[name] = closed(own(root, name), name);
  }
  if (own(root, "schema") !== JOB_STATE_SCHEMA_ID) throw new Invalid("schema");
  /** Value and static path of `<section>.<key>`. */
  const at = (name: Section, key: string): unknown => own(sections[name], key);
  const p = (name: Section, key: string): string => `${name}.${key}`;
  return {
    schema: JOB_STATE_SCHEMA_ID,
    request: {
      request_id: matching(at("request", "request_id"), REQUEST_ID_PATTERN, p("request", "request_id")),
      request_digest: matching(at("request", "request_digest"), REQUEST_DIGEST_PATTERN, p("request", "request_digest")),
      target: nullableMatching(at("request", "target"), IDENTIFIER_PATTERN, p("request", "target")),
    },
    lineage: {
      replaces_request_digest: nullableMatching(
        at("lineage", "replaces_request_digest"),
        REQUEST_DIGEST_PATTERN,
        p("lineage", "replaces_request_digest"),
      ),
      replacement_request_digest: nullableMatching(
        at("lineage", "replacement_request_digest"),
        REQUEST_DIGEST_PATTERN,
        p("lineage", "replacement_request_digest"),
      ),
    },
    state: oneOf(own(root, "state"), STATES, "state"),
    provider: {
      job_id: nullableMatching(at("provider", "job_id"), IDENTIFIER_PATTERN, p("provider", "job_id")),
      status: oneOf(at("provider", "status"), PROVIDER_STATUSES, p("provider", "status")),
    },
    execution: {
      status: oneOf(at("execution", "status"), EXECUTION_STATUSES, p("execution", "status")),
      outcome: oneOf(at("execution", "outcome"), OUTCOMES, p("execution", "outcome")),
      terminal: bool(at("execution", "terminal"), p("execution", "terminal")),
    },
    accounting: {
      status: oneOf(at("accounting", "status"), ACCOUNTING_STATUSES, p("accounting", "status")),
    },
    usage: {
      reservation_status: oneOf(at("usage", "reservation_status"), RESERVATION_STATUSES, p("usage", "reservation_status")),
      held_quantum_seconds: nullableAmount(at("usage", "held_quantum_seconds"), p("usage", "held_quantum_seconds")),
      settled_quantum_seconds: nullableAmount(at("usage", "settled_quantum_seconds"), p("usage", "settled_quantum_seconds")),
    },
    cancellation: {
      requested: bool(at("cancellation", "requested"), p("cancellation", "requested")),
      requested_at: nullableAmount(at("cancellation", "requested_at"), p("cancellation", "requested_at")),
      confirmed: bool(at("cancellation", "confirmed"), p("cancellation", "confirmed")),
      confirmed_at: nullableAmount(at("cancellation", "confirmed_at"), p("cancellation", "confirmed_at")),
    },
    deadlines: {
      queue_at: amount(at("deadlines", "queue_at"), p("deadlines", "queue_at")),
      queue_expired: bool(at("deadlines", "queue_expired"), p("deadlines", "queue_expired")),
      accounting_at: nullableAmount(at("deadlines", "accounting_at"), p("deadlines", "accounting_at")),
      accounting_expired: bool(at("deadlines", "accounting_expired"), p("deadlines", "accounting_expired")),
    },
    next_action: oneOf(own(root, "next_action"), SCHEMA_NEXT_ACTIONS, "next_action"),
  };
}

function ensure(condition: boolean, field: string): void {
  if (!condition) throw new Invalid(field);
}

function includes(list: readonly string[], value: string): boolean {
  return list.includes(value);
}

function checkAccounting(s: JobStateV1): void {
  const { usage } = s;
  switch (s.accounting.status) {
    case "SETTLED": // I1
      ensure(s.state === "SETTLED", "state");
      ensure(s.next_action === "NONE", "next_action");
      ensure(usage.reservation_status === "SETTLED", "usage.reservation_status");
      ensure(usage.held_quantum_seconds === 0, "usage.held_quantum_seconds");
      ensure(usage.settled_quantum_seconds !== null, "usage.settled_quantum_seconds");
      return;
    case "NOT_APPLICABLE": // I2
      ensure(s.next_action === "NONE", "next_action");
      ensure(usage.reservation_status === "NOT_RESERVED", "usage.reservation_status");
      ensure(usage.held_quantum_seconds === null, "usage.held_quantum_seconds");
      ensure(usage.settled_quantum_seconds === null, "usage.settled_quantum_seconds");
      return;
    case "PENDING": // I3
      ensure(includes(PENDING_STATES, s.state), "state");
      ensure(includes(PENDING_RESERVATIONS, usage.reservation_status), "usage.reservation_status");
      ensure(usage.settled_quantum_seconds === null, "usage.settled_quantum_seconds");
      return;
  }
}

function checkExecution(s: JobStateV1): void {
  const { execution } = s;
  if (execution.terminal) {
    ensure(includes(TERMINAL_STATUSES, execution.status), "execution.status"); // I5
  } else {
    ensure(includes(NON_TERMINAL_STATUSES, execution.status), "execution.status"); // I4
    ensure(execution.outcome === "UNKNOWN", "execution.outcome");
  }
  const decided = DECIDED_OUTCOME.get(execution.status); // I6a-e
  if (decided !== undefined) {
    ensure(execution.outcome === decided, "execution.outcome");
    ensure(execution.terminal, "execution.terminal");
  }
}

function checkCancellation(s: JobStateV1): void {
  const { cancellation } = s;
  if (s.execution.status === "CANCELLED") {
    ensure(cancellation.confirmed, "cancellation.confirmed"); // I7
  } else {
    ensure(!cancellation.confirmed, "cancellation.confirmed");
    ensure(cancellation.confirmed_at === null, "cancellation.confirmed_at");
  }
  if (cancellation.requested) {
    ensure(cancellation.requested_at !== null, "cancellation.requested_at"); // I8
  } else {
    ensure(cancellation.requested_at === null, "cancellation.requested_at");
  }
}

function deepFreeze<T>(value: T): T {
  if (typeof value === "object" && value !== null) {
    for (const key of Object.keys(value)) deepFreeze((value as Record<string, unknown>)[key]);
    Object.freeze(value);
  }
  return value;
}

/**
 * The success checks the Python `LifecycleClient` performs, in its order. A
 * failure here reports `field: null`, exactly like Python does.
 */
function pythonParityChecks(value: unknown, expectedDigest: string): void {
  if (!isPlainObject(value)) throw new Invalid(null);
  if (own(value, "schema") !== JOB_STATE_SCHEMA_ID) throw new Invalid(null);
  const request = own(value, "request");
  const nextAction = own(value, "next_action");
  if (
    !isPlainObject(request) ||
    own(request, "request_digest") !== expectedDigest ||
    typeof nextAction !== "string" ||
    !includes(NEXT_ACTIONS, nextAction)
  ) {
    throw new Invalid(null);
  }
}

/**
 * Validate a parsed 2xx body against the closed v1 envelope and bind it to
 * `expectedDigest`. Returns a fresh, deep-frozen plain object; throws
 * `LifecycleClientError("INVALID_RESPONSE")` whose `field` is a static schema
 * path or `null` (never response content).
 */
export function validateJobState(value: unknown, expectedDigest: string, httpStatus: number | null = null): JobStateV1 {
  if (typeof expectedDigest !== "string" || !REQUEST_DIGEST_PATTERN.test(expectedDigest)) {
    throw new LifecycleClientError("IDENTITY", { field: "request_digest" });
  }
  try {
    pythonParityChecks(value, expectedDigest);
    const state = shape(value);
    checkAccounting(state);
    checkExecution(state);
    checkCancellation(state);
    return deepFreeze(state);
  } catch (error: unknown) {
    throw invalidResponse(httpStatus, error instanceof Invalid ? error.field : null);
  }
}
