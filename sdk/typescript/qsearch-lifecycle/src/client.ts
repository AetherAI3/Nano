/**
 * Lifecycle clients for `nano.qsearch.lifecycle-wire.v1` (status) and the
 * additive `nano.qsearch.lifecycle-wire.v2` controls (refresh, cancel, replace).
 *
 * Every capability (transport, signer, clock, nonce, timer, sha256) is
 * injected by the caller. The clients read no environment, file or browser
 * storage, discover nothing and log nothing. One invocation is exactly one
 * transport call, or zero when refused before I/O; nothing is ever retried,
 * polled or followed up automatically.
 */
import {
  LifecycleClientError,
  configurationError,
  invalidResponse,
  transportUnavailable,
  unsupportedCapability,
} from "./errors.js";
import { copyUint8Array, isUint8Array, lowerHex } from "./bytes.js";
import { canonicalizeReplacement } from "./canonical.js";
import { parseStrict } from "./json.js";
import type { JobStateV1 } from "./types.js";
import { validateJobState } from "./validate.js";
import {
  ACCEPT_HEADER,
  ACCEPT_VALUE,
  AUTH_HEADER,
  AUTH_SCHEME,
  CONTENT_TYPE_HEADER,
  CONTENT_TYPE_VALUE,
  CONTROL_METHOD,
  DEFAULT_CAPABILITIES,
  DEFAULT_TIMEOUT_MS,
  EMPTY_BODY_SHA256,
  MAC_BYTES,
  MAX_RESPONSE_BYTES,
  MAX_SAFE_INTEGER,
  MAX_TIMEOUT_MS,
  NONCE_PATTERN,
  REQUEST_DIGEST_PATTERN,
  STATUS_METHOD,
  STATUS_PATH_PREFIX,
  isCapability,
  isNextAction,
  isRemoteCode,
  isRemoteField,
} from "./wire.js";
import type { Capability, ControlAction } from "./wire.js";

/** Minimal abort-signal shape handed to transports (a platform `AbortSignal`). */
export interface TransportSignal {
  readonly aborted: boolean;
  addEventListener(type: "abort", listener: () => void, options?: { once?: boolean }): void;
  removeEventListener(type: "abort", listener: () => void): void;
}

export interface TransportRequest {
  /** `GET` for status, `POST` for the v2 controls. */
  readonly method: "GET" | "POST";
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  /**
   * Request body bytes (a private copy per call). Always present on requests
   * from `LifecycleClient`: empty for GET, refresh and cancel; the canonical
   * replacement for replace. `StatusClient` (v1) omits it; its GET has no body.
   */
  readonly body?: Uint8Array;
  readonly timeoutMs: number;
  readonly maxResponseBytes: number;
  readonly signal: TransportSignal;
}

export interface TransportResponse {
  readonly status: number;
  readonly body: Uint8Array;
}

/** Performs exactly the one HTTP exchange described; must not follow redirects. */
export type Transport = (request: TransportRequest) => Promise<TransportResponse>;
/** Returns the 32 raw HMAC-SHA256 bytes of `message` under the request key. */
export type Signer = (message: Uint8Array) => Promise<Uint8Array>;
/** Returns the 32 raw SHA-256 bytes of `bytes` (needed only for `replace`). */
export type Sha256 = (bytes: Uint8Array) => Promise<Uint8Array>;
/** Unix time in seconds (finite, >= 0, <= 2^53-1). */
export type Clock = () => number;
/** A fresh 16..64 lowercase-hex nonce per request. */
export type NonceSource = () => string;
/** Schedules `callback` after `ms`; returns a function cancelling it. */
export type SetTimer = (callback: () => void, ms: number) => () => void;

export interface StatusClientOptions {
  readonly baseUrl: string;
  readonly transport: Transport;
  readonly signer: Signer;
  readonly clock: Clock;
  readonly nonce: NonceSource;
  readonly timeoutMs?: number;
  readonly setTimer?: SetTimer;
}

export interface LifecycleClientOptions extends StatusClientOptions {
  /**
   * Operations this client may perform, drawn from `status`, `refresh`,
   * `cancel`, `replace`. Default `["status"]`. Granting one grants no
   * server-side authority.
   */
  readonly capabilities?: readonly string[];
  /** Required when `replace` is granted. */
  readonly sha256?: Sha256;
}

const SCHEME_AUTHORITY = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]*)(\/?)$/;
const PRINTABLE_ASCII = /^[\x21-\x7e]+$/;
const REG_NAME_HOST = /^([A-Za-z0-9._~-]+)(?::([0-9]*))?$/;
const IPV6_HOST = /^\[([0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*)\](?::([0-9]*))?$/;

/**
 * CONTRACT.md section 1.1: http(s), non-empty host, no user info, path "" or
 * "/", no query, no fragment, not port 0. Returns the origin without a
 * trailing slash, or `null` when the URL is refused. Any `?` or `#` (even an
 * empty query or fragment), whitespace or non-ASCII character is refused.
 */
function normaliseBaseUrl(baseUrl: unknown): string | null {
  if (typeof baseUrl !== "string" || !PRINTABLE_ASCII.test(baseUrl)) return null;
  const match = SCHEME_AUTHORITY.exec(baseUrl);
  if (match === null) return null;
  const scheme = (match[1] ?? "").toLowerCase();
  const authority = match[2] ?? "";
  if (scheme !== "http" && scheme !== "https") return null;
  if (authority.includes("@")) return null;
  const host = IPV6_HOST.exec(authority) ?? REG_NAME_HOST.exec(authority);
  if (host === null) return null;
  const port = host[2];
  if (port !== undefined && port !== "") {
    const value = Number(port);
    if (!Number.isSafeInteger(value) || value < 1 || value > 65_535) return null;
  }
  try {
    // WHATWG host parsing additionally refuses malformed IPv6 literals and
    // IPv4-looking hosts such as 1.2.3.4.5 (Python's urlsplit refuses bad IPv6 too).
    new URL(baseUrl);
  } catch {
    return null;
  }
  return match[3] === "/" ? baseUrl.slice(0, -1) : baseUrl;
}

const defaultSetTimer: SetTimer = (callback, ms) => {
  const handle = globalThis.setTimeout(callback, ms);
  return () => globalThis.clearTimeout(handle);
};

function isFunction(value: unknown): value is (...args: never[]) => unknown {
  return typeof value === "function";
}

function quietly(action: () => void): void {
  try {
    action();
  } catch {
    // A faulty injected timer or abort listener cannot change the outcome.
  }
}

const LF = String.fromCharCode(0x0a);
const EMPTY_BODY = new Uint8Array(0);
const SHA256_BYTES = 32;

/** v1 requests keep their frozen shape (no `body` member); v2 requests always carry one. */
type Framing = "v1" | "v2";

interface Prepared {
  /** Lowercase hex SHA-256 of the body (the MAC input). */
  readonly bodySha256: string;
  /** The digest the 2xx envelope's `request.request_digest` must equal. */
  readonly expectedDigest: string;
}

interface CallPlan {
  readonly method: "GET" | "POST";
  readonly requestDigest: string;
  readonly action: ControlAction | null;
  readonly body: Uint8Array;
  /** Runs inside the deadline before signing; any failure is CLIENT_CONFIGURATION (sha256). */
  readonly prepare: (() => Promise<Prepared>) | null;
}

type Phase = "timer" | "prepare" | "sign" | "transport";
const PHASE_FIELD: Readonly<Record<Exclude<Phase, "transport">, string>> = Object.freeze({
  timer: "set_timer",
  prepare: "sha256",
  sign: "signer",
});

function checkDigest(requestDigest: unknown): asserts requestDigest is string {
  if (typeof requestDigest !== "string" || !REQUEST_DIGEST_PATTERN.test(requestDigest)) {
    throw new LifecycleClientError("IDENTITY", { field: "request_digest" });
  }
}

/**
 * The one engine both clients share: option validation, request framing,
 * signing, the single deadline, exactly one transport call and response
 * handling in the order of Python `LifecycleClient._call`.
 */
class Core {
  readonly #origin: string;
  readonly #transport: Transport;
  readonly #signer: Signer;
  readonly #clock: Clock;
  readonly #nonce: NonceSource;
  readonly #timeoutMs: number;
  readonly #setTimer: SetTimer;
  readonly #framing: Framing;

  constructor(options: StatusClientOptions, framing: Framing) {
    if (typeof options !== "object" || options === null) throw configurationError(null);
    const origin = normaliseBaseUrl(options.baseUrl);
    if (origin === null) throw configurationError("base_url");
    if (!isFunction(options.transport)) throw configurationError("transport");
    if (!isFunction(options.signer)) throw configurationError("signer");
    if (!isFunction(options.clock)) throw configurationError("clock");
    if (!isFunction(options.nonce)) throw configurationError("nonce");
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    if (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_TIMEOUT_MS) {
      throw configurationError("timeout_ms");
    }
    const setTimer = options.setTimer ?? defaultSetTimer;
    if (!isFunction(setTimer)) throw configurationError("set_timer");
    this.#origin = origin;
    this.#transport = options.transport;
    this.#signer = options.signer;
    this.#clock = options.clock;
    this.#nonce = options.nonce;
    this.#timeoutMs = timeoutMs;
    this.#setTimer = setTimer;
    this.#framing = framing;
    Object.freeze(this);
  }

  /** One invocation: exactly one transport call, or zero when refused before I/O. */
  async call(plan: CallPlan): Promise<JobStateV1> {
    const path = STATUS_PATH_PREFIX + plan.requestDigest + (plan.action === null ? "" : `/${plan.action}`);
    const ts = String(this.#timestamp());
    const nonce = this.#freshNonce();
    const { response, expectedDigest } = await this.#signAndExchange(plan, path, ts, nonce);
    const replaces = plan.action === "replace" ? plan.requestDigest : null;
    return interpret(response.status, response.body, expectedDigest, replaces);
  }

  #timestamp(): number {
    let now: unknown;
    try {
      now = this.#clock();
    } catch {
      throw configurationError("clock");
    }
    if (typeof now !== "number" || !Number.isFinite(now) || now < 0 || now > MAX_SAFE_INTEGER) {
      throw configurationError("clock");
    }
    return Math.floor(now);
  }

  #freshNonce(): string {
    let nonce: unknown;
    try {
      nonce = this.#nonce();
    } catch {
      throw configurationError("nonce");
    }
    if (typeof nonce !== "string" || !NONCE_PATTERN.test(nonce)) throw configurationError("nonce");
    return nonce;
  }

  /** Exactly the contract headers; v1 framing has no `body` member. Frozen. */
  #request(plan: CallPlan, path: string, auth: string, signal: TransportSignal): TransportRequest {
    const headers: Record<string, string> = { [AUTH_HEADER]: auth, [ACCEPT_HEADER]: ACCEPT_VALUE };
    if (plan.method === CONTROL_METHOD) headers[CONTENT_TYPE_HEADER] = CONTENT_TYPE_VALUE;
    const url = this.#origin + path;
    const frozenHeaders = Object.freeze(headers);
    const limits = { timeoutMs: this.#timeoutMs, maxResponseBytes: MAX_RESPONSE_BYTES };
    if (this.#framing === "v1") {
      return Object.freeze({ method: plan.method, url, headers: frozenHeaders, ...limits, signal });
    }
    return Object.freeze({ method: plan.method, url, headers: frozenHeaders, body: plan.body.slice(), ...limits, signal });
  }

  /**
   * One deadline (the injected timer, `timeoutMs`) bounds hashing (replace
   * only), signing and the single transport exchange:
   * - sha256 throws/rejects, returns anything but 32 bytes, or is still
   *   pending at the deadline: CLIENT_CONFIGURATION (field `sha256`), zero
   *   transport calls;
   * - signer throws/rejects, returns anything but 32 bytes, or is still
   *   pending at the deadline: CLIENT_CONFIGURATION (field `signer`), zero
   *   transport calls;
   * - transport throws/rejects, returns no response object, or is still
   *   pending at the deadline: TRANSPORT_UNAVAILABLE (POLL_STATUS for GET,
   *   MANUAL_RECONCILIATION for POST), and its signal is aborted;
   * - a timer that throws or fires synchronously: CLIENT_CONFIGURATION
   *   (field `set_timer`), zero transport calls.
   * Anything arriving after the outcome is decided is ignored.
   */
  #signAndExchange(
    plan: CallPlan,
    path: string,
    ts: string,
    nonce: string,
  ): Promise<{ readonly response: RawResponse; readonly expectedDigest: string }> {
    return new Promise((resolve, reject) => {
      const controller = new AbortController();
      let settled = false;
      let phase: Phase = "timer";
      let cancelTimer: () => void = () => undefined;
      const decide = (outcome: () => void): void => {
        if (settled) return;
        settled = true;
        quietly(cancelTimer);
        outcome();
      };
      const fail = (error: LifecycleClientError): void =>
        decide(() => {
          quietly(() => controller.abort());
          reject(error);
        });
      const onDeadline = (): void =>
        fail(phase === "transport" ? transportUnavailable(plan.method) : configurationError(PHASE_FIELD[phase]));
      try {
        const cancel: unknown = this.#setTimer(onDeadline, this.#timeoutMs);
        if (isFunction(cancel)) cancelTimer = cancel as () => void;
      } catch {
        fail(configurationError("set_timer"));
      }
      if (settled) {
        quietly(cancelTimer); // fired synchronously: release the handle it returned afterwards
        return;
      }
      const sign = (prepared: Prepared): void => {
        phase = "sign";
        const message = new TextEncoder().encode([plan.method, path, prepared.bodySha256, ts, nonce].join(LF));
        attempt(() => this.#signer(message)).then((signed) => {
          if (settled) return;
          // Everything the signer handed back is read inside this try: a forged or hostile
          // result is a signer fault, never an unhandled rejection or a transport error.
          let request: TransportRequest;
          try {
            const mac = copyUint8Array(signed);
            if (mac === null || mac.length !== MAC_BYTES) return fail(configurationError("signer"));
            request = this.#request(plan, path, `${AUTH_SCHEME} ts=${ts} nonce=${nonce} mac=${lowerHex(mac)}`, controller.signal);
          } catch {
            return fail(configurationError("signer"));
          }
          phase = "transport";
          attempt(() => this.#transport(request)).then(
            (result) => {
              const response = responseParts(result);
              if (response === null) fail(transportUnavailable(plan.method));
              else decide(() => resolve({ response, expectedDigest: prepared.expectedDigest }));
            },
            () => fail(transportUnavailable(plan.method)),
          );
        }, () => fail(configurationError("signer")));
      };
      if (plan.prepare === null) {
        sign({ bodySha256: EMPTY_BODY_SHA256, expectedDigest: plan.requestDigest });
        return;
      }
      phase = "prepare";
      attempt(plan.prepare).then(
        (prepared) => {
          if (!settled) sign(prepared as Prepared);
        },
        () => fail(configurationError("sha256")),
      );
    });
  }
}

/**
 * Read-only v1 status client (`nano.qsearch.lifecycle-wire.v1`). Unchanged
 * behaviour; a thin wrapper over the shared engine. It has no write method and
 * refuses every other operation.
 */
export class StatusClient {
  readonly #core: Core;

  constructor(options: StatusClientOptions) {
    this.#core = new Core(options, "v1");
    Object.freeze(this);
  }

  /** True only for `"status"`; cancel/refresh/replace and native ops are unsupported in v1. */
  supports(operation: string): boolean {
    return operation === "status";
  }

  /** Throws `UNSUPPORTED_CAPABILITY` (field `"operation"`) for anything but `"status"`. */
  requireCapability(operation: string): void {
    if (!this.supports(operation)) throw unsupportedCapability();
  }

  /** Read the job state for `requestDigest`. Exactly one transport call; never retries. */
  async status(requestDigest: string): Promise<JobStateV1> {
    checkDigest(requestDigest);
    return this.#core.call({ method: STATUS_METHOD, requestDigest, action: null, body: EMPTY_BODY, prepare: null });
  }
}

/** Snapshot the caller's capability list; anything but known operation names is refused. */
function grantedCapabilities(options: LifecycleClientOptions): ReadonlySet<Capability> {
  try {
    const requested: unknown = options.capabilities;
    if (requested === undefined) return new Set<Capability>(DEFAULT_CAPABILITIES);
    if (!Array.isArray(requested)) throw configurationError("capabilities");
    const granted = new Set<Capability>();
    for (let i = 0; i < requested.length; i += 1) {
      const operation: unknown = requested[i];
      if (!isCapability(operation)) throw configurationError("capabilities");
      granted.add(operation);
    }
    return granted;
  } catch {
    throw configurationError("capabilities");
  }
}

/** The injected SHA-256 over a private copy; anything but 32 bytes is a configuration fault. */
async function sha256Of(sha256: Sha256, bytes: Uint8Array): Promise<Uint8Array> {
  const digest = copyUint8Array(await attempt(() => sha256(bytes.slice())));
  if (digest === null || digest.length !== SHA256_BYTES) throw configurationError("sha256");
  return digest;
}

/**
 * v2 lifecycle client (`nano.qsearch.lifecycle-wire.v2`): status plus the
 * explicit host controls refresh, cancel and replace, each only when granted.
 *
 * A control is exactly one POST, or zero transport calls when refused locally.
 * The returned envelope is the server's state after the request, returned as
 * is: a cancel response is not cancellation (only `execution.status:
 * CANCELLED` with `cancellation.confirmed: true` is). A lost acknowledgement or
 * timeout on a control is `TRANSPORT_UNAVAILABLE` with
 * `MANUAL_RECONCILIATION`; nothing is retried, polled or followed up.
 */
export class LifecycleClient {
  readonly #core: Core;
  readonly #granted: ReadonlySet<Capability>;
  readonly #sha256: Sha256 | null;

  constructor(options: LifecycleClientOptions) {
    this.#core = new Core(options, "v2");
    this.#granted = grantedCapabilities(options);
    const sha256: unknown = options.sha256;
    if (sha256 !== undefined && !isFunction(sha256)) throw configurationError("sha256");
    if (this.#granted.has("replace") && sha256 === undefined) throw configurationError("sha256");
    this.#sha256 = sha256 === undefined ? null : (sha256 as Sha256);
    Object.freeze(this);
  }

  /** True only for an operation granted at construction. */
  supports(operation: string): boolean {
    return typeof operation === "string" && this.#granted.has(operation as Capability);
  }

  /** Throws `UNSUPPORTED_CAPABILITY` (field `"operation"`) for anything not granted. */
  requireCapability(operation: string): void {
    if (!this.supports(operation)) throw unsupportedCapability();
  }

  /** Read the job state (GET). Transport failure is `POLL_STATUS`. */
  async status(requestDigest: string): Promise<JobStateV1> {
    this.requireCapability("status");
    checkDigest(requestDigest);
    return this.#core.call({ method: STATUS_METHOD, requestDigest, action: null, body: EMPTY_BODY, prepare: null });
  }

  /** Ask the host to refresh provider state (empty POST). May return an unchanged state. */
  async refresh(requestDigest: string): Promise<JobStateV1> {
    return this.#control("refresh", requestDigest);
  }

  /** Ask the host to cancel (empty POST). The response is NOT cancellation; read `execution`. */
  async cancel(requestDigest: string): Promise<JobStateV1> {
    return this.#control("cancel", requestDigest);
  }

  /**
   * Ask the host to admit `replacement` (JSON text or UTF-8 bytes) as a new
   * request replacing `requestDigest`. The body is its canonical form; the
   * response must describe the new request (`sha256(canonical(descriptor))`)
   * and name `requestDigest` in `lineage.replaces_request_digest`.
   */
  async replace(requestDigest: string, replacement: string | Uint8Array): Promise<JobStateV1> {
    this.requireCapability("replace");
    const canonical = canonicalizeReplacement(replacement);
    checkDigest(requestDigest);
    const sha256 = this.#sha256;
    if (sha256 === null) throw configurationError("sha256"); // unreachable: checked at construction
    const prepare = async (): Promise<Prepared> => {
      // Both hashes start together, inside the deadline: the injected capability is
      // never invoked after the call has already been settled by the timer.
      const [bodyHash, descriptorHash] = await Promise.all([
        sha256Of(sha256, canonical.body),
        sha256Of(sha256, canonical.descriptor),
      ]);
      return { bodySha256: lowerHex(bodyHash), expectedDigest: `sha256:${lowerHex(descriptorHash)}` };
    };
    return this.#core.call({ method: CONTROL_METHOD, requestDigest, action: "replace", body: canonical.body, prepare });
  }

  async #control(action: "refresh" | "cancel", requestDigest: string): Promise<JobStateV1> {
    this.requireCapability(action);
    checkDigest(requestDigest);
    return this.#core.call({ method: CONTROL_METHOD, requestDigest, action, body: EMPTY_BODY, prepare: null });
  }
}

interface RawResponse {
  readonly status: unknown;
  readonly body: unknown;
}

/** Run an injected capability; a synchronous throw becomes a rejection. */
function attempt(capability: () => unknown): Promise<unknown> {
  try {
    return Promise.resolve(capability());
  } catch (error: unknown) {
    return Promise.reject(error);
  }
}

/** Snapshot `status` and `body` once (getters may throw); null when there is no response object. */
function responseParts(result: unknown): RawResponse | null {
  try {
    if (typeof result !== "object" || result === null) return null;
    const { status, body } = result as RawResponse;
    return { status, body };
  } catch {
    return null;
  }
}

/**
 * Response handling in the order of Python `LifecycleClient._call`.
 * `expectedDigest` binds `request.request_digest`; for replace, `replaces`
 * must also equal `lineage.replaces_request_digest` (field null, like Python).
 */
function interpret(status: unknown, body: unknown, expectedDigest: string, replaces: string | null): JobStateV1 {
  if (typeof status !== "number" || !Number.isInteger(status) || status < 100 || status > 599) {
    throw invalidResponse(null);
  }
  if (status >= 300 && status < 400) {
    throw new LifecycleClientError("UNEXPECTED_REDIRECT", { httpStatus: status });
  }
  if (!isUint8Array(body) || body.length > MAX_RESPONSE_BYTES) throw invalidResponse(status);
  let payload: unknown;
  try {
    // ignoreBOM keeps a leading U+FEFF, which the strict parser then rejects.
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(body);
    payload = parseStrict(text);
  } catch {
    throw invalidResponse(status);
  }
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) throw invalidResponse(status);
  if (status < 200 || status >= 300) throw remoteError(payload as Readonly<Record<string, unknown>>, status);
  const state = validateJobState(payload, expectedDigest, status);
  if (replaces !== null && state.lineage.replaces_request_digest !== replaces) throw invalidResponse(status);
  return state;
}

/** CONTRACT.md section 2.3: map through allowlists; free text is discarded. */
function remoteError(payload: Readonly<Record<string, unknown>>, status: number): LifecycleClientError {
  const member = (key: string): unknown =>
    Object.prototype.hasOwnProperty.call(payload, key) ? payload[key] : undefined;
  const code = member("code");
  const field = member("field");
  const nextAction = member("next_action");
  return new LifecycleClientError(isRemoteCode(code) ? code : "REMOTE_ERROR", {
    field: isRemoteField(field) ? field : null,
    retryable: member("retryable") === true,
    nextAction: isNextAction(nextAction) ? nextAction : "MANUAL_RECONCILIATION",
    httpStatus: status,
  });
}
