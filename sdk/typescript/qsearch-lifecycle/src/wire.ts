/**
 * Constants of `protocol/qsearch-lifecycle/v1/wire.v1.json`, embedded so the
 * runtime never reads files. `test/drift.test.mjs` asserts every list and
 * scalar here equals the frozen JSON.
 */

export const CONTRACT_ID = "nano.qsearch.lifecycle-wire.v1";
export const JOB_STATE_SCHEMA_ID = "nano.qsearch.job-state.v1";

export const AUTH_HEADER = "X-Aether-Qsearch-Auth";
export const AUTH_SCHEME = "v1";
export const ACCEPT_HEADER = "Accept";
export const ACCEPT_VALUE = "application/json";
export const MAC_BYTES = 32;
export const MIN_KEY_BYTES = 16;
export const NONCE_PATTERN = /^[0-9a-f]{16,64}$/;
export const REQUEST_DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
/** SHA-256 of the empty GET body; status requests never carry a body. */
export const EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

export const STATUS_METHOD = "GET";
export const STATUS_PATH_PREFIX = "/qsearch/v2/requests/";

export const MAX_RESPONSE_BYTES = 1_048_576;
export const MAX_JSON_DEPTH = 64;
export const MAX_SAFE_INTEGER = 9_007_199_254_740_991;
export const DEFAULT_TIMEOUT_MS = 30_000;
/** Largest delay a platform timer accepts without overflowing to ~0 ms. */
export const MAX_TIMEOUT_MS = 2_147_483_647;

export const NEXT_ACTIONS = Object.freeze([
  "MANUAL_RECONCILIATION",
  "NONE",
  "POLL_STATUS",
  "RECONCILE",
  "REQUEST_CANCEL",
] as const);

export const CLIENT_ERROR_CODES = Object.freeze([
  "CLIENT_CONFIGURATION",
  "IDENTITY",
  "INVALID_RESPONSE",
  "REMOTE_ERROR",
  "TRANSPORT_UNAVAILABLE",
  "UNEXPECTED_REDIRECT",
  "UNSUPPORTED_CAPABILITY",
] as const);

export const SUPPORTED_OPERATIONS = Object.freeze(["status"] as const);
export const UNSUPPORTED_OPERATIONS = Object.freeze(["cancel", "refresh", "replace"] as const);

export const REMOTE_CODES = Object.freeze([
  "ACCOUNTING_EXPIRED",
  "ACCOUNTING_PENDING",
  "BODY_TOO_LARGE",
  "CANCELLATION_FENCE_REQUIRED",
  "CANCELLATION_NOT_AUTHORIZED",
  "CANCELLATION_PENDING",
  "CANCELLATION_REQUESTED",
  "CANCEL_ACKNOWLEDGEMENT_UNKNOWN",
  "EMPTY_BODY_REQUIRED",
  "IDENTITY",
  "INVALID_ACTION",
  "INVALID_CONTENT_LENGTH",
  "INVALID_JSON",
  "INVALID_REQUEST",
  "JSON",
  "LIFECYCLE_ADAPTER_UNAVAILABLE",
  "LIFECYCLE_BUSY",
  "LIFECYCLE_TARGET_UNSUPPORTED",
  "LIFECYCLE_UNAVAILABLE",
  "LIVE_CONTEXT_REQUIRED",
  "NEW_IDENTITY_REQUIRED",
  "NEW_TARGET_REQUIRED",
  "NOT_ENABLED",
  "NOT_FOUND",
  "ORIGINAL_ATTEMPT_UNRECONCILED",
  "PROVIDER_IDENTITY_MISMATCH",
  "PROVIDER_OR_STORE_UNAVAILABLE",
  "PROVIDER_STATUS_UNKNOWN",
  "PROVIDER_SUBMISSION_AMBIGUOUS",
  "PROVIDER_SUBMISSION_UNKNOWN",
  "PROVIDER_UNAVAILABLE",
  "QUEUE_EXPIRED",
  "REPLACEMENT_BLOCKED",
  "REPLACEMENT_CONFLICT",
  "REPLACEMENT_IDENTITY",
  "REPLACEMENT_TARGET",
  "REQUEST_BUSY",
  "REQUEST_CONFLICT",
  "REQUEST_ID_CONFLICT",
  "REQUEST_NOT_FOUND",
  "STATE_UNAVAILABLE",
  "STORE_UNAVAILABLE",
  "UNAUTHORIZED",
] as const);

export const REMOTE_FIELDS = Object.freeze([
  "authority",
  "body",
  "descriptor",
  "descriptor.max_execution_time",
  "descriptor.request_id",
  "descriptor.target",
  "headers.Content-Length",
  "jobs",
  "replacement",
  "request_digest",
  "request_id",
  "target",
] as const);

export const PROVIDER_STATUSES = Object.freeze([
  "CANCELED",
  "CANCELLED",
  "COMPLETED",
  "DONE",
  "ERROR",
  "FAILED",
  "INITIALIZING",
  "PENDING",
  "QUEUED",
  "RUNNING",
  "UNKNOWN",
  "VALIDATING",
] as const);

export type NextAction = (typeof NEXT_ACTIONS)[number];
export type ClientErrorCode = (typeof CLIENT_ERROR_CODES)[number];
export type RemoteCode = (typeof REMOTE_CODES)[number];
export type RemoteField = (typeof REMOTE_FIELDS)[number];
export type ProviderStatus = (typeof PROVIDER_STATUSES)[number];

const NEXT_ACTION_SET: ReadonlySet<string> = new Set(NEXT_ACTIONS);
const REMOTE_CODE_SET: ReadonlySet<string> = new Set(REMOTE_CODES);
const REMOTE_FIELD_SET: ReadonlySet<string> = new Set(REMOTE_FIELDS);

export function isNextAction(value: unknown): value is NextAction {
  return typeof value === "string" && NEXT_ACTION_SET.has(value);
}

export function isRemoteCode(value: unknown): value is RemoteCode {
  return typeof value === "string" && REMOTE_CODE_SET.has(value);
}

export function isRemoteField(value: unknown): value is RemoteField {
  return typeof value === "string" && REMOTE_FIELD_SET.has(value);
}

/*
 * Constants of `protocol/qsearch-lifecycle/v2/wire.v2.json` (additive to v1).
 * `test/drift-v2.test.mjs` asserts they equal the frozen JSON.
 */

export const CONTRACT_V2_ID = "nano.qsearch.lifecycle-wire.v2";

/** Every operation a v2 client may be granted, sorted as in wire.v2.json. */
export const CAPABILITIES = Object.freeze(["cancel", "refresh", "replace", "status"] as const);
export const DEFAULT_CAPABILITIES = Object.freeze(["status"] as const);
export type Capability = (typeof CAPABILITIES)[number];
export type ControlAction = Exclude<Capability, "status">;

export const CONTROL_METHOD = "POST";
export const CONTENT_TYPE_HEADER = "Content-Type";
export const CONTENT_TYPE_VALUE = "application/json";
export const MAX_CANONICAL_DEPTH = 64;
/** Python's default `sys.int_info.default_max_str_digits`: json.loads refuses longer integers. */
export const MAX_INTEGER_DIGITS = 4_300;

export const CLIENT_ERROR_CODES_V2 = Object.freeze([
  "CLIENT_CONFIGURATION",
  "IDENTITY",
  "INVALID_REQUEST",
  "INVALID_RESPONSE",
  "REMOTE_ERROR",
  "TRANSPORT_UNAVAILABLE",
  "UNEXPECTED_REDIRECT",
  "UNSUPPORTED_CAPABILITY",
] as const);
export type ClientErrorCodeV2 = (typeof CLIENT_ERROR_CODES_V2)[number];

const CAPABILITY_SET: ReadonlySet<string> = new Set(CAPABILITIES);

export function isCapability(value: unknown): value is Capability {
  return typeof value === "string" && CAPABILITY_SET.has(value);
}
