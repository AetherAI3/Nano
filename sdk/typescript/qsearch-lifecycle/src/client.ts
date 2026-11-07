/**
 * Read-only status client for `nano.qsearch.lifecycle-wire.v1`.
 *
 * Every capability (transport, signer, clock, nonce, timer) is injected by
 * the caller. The client reads no environment, file or browser storage,
 * discovers nothing, logs nothing and has no write operation. One `status()`
 * call is exactly one transport call, or zero when refused before I/O.
 */
import {
  LifecycleClientError,
  configurationError,
  invalidResponse,
  transportUnavailable,
} from "./errors.js";
import { copyUint8Array, isUint8Array, lowerHex } from "./bytes.js";
import { parseStrict } from "./json.js";
import type { JobStateV1 } from "./types.js";
import { validateJobState } from "./validate.js";
import {
  ACCEPT_HEADER,
  ACCEPT_VALUE,
  AUTH_HEADER,
  AUTH_SCHEME,
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
  isNextAction,
  isRemoteCode,
  isRemoteField,
} from "./wire.js";

/** Minimal abort-signal shape handed to transports (a platform `AbortSignal`). */
export interface TransportSignal {
  readonly aborted: boolean;
  addEventListener(type: "abort", listener: () => void, options?: { once?: boolean }): void;
  removeEventListener(type: "abort", listener: () => void): void;
}

export interface TransportRequest {
  readonly method: "GET";
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
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

export class StatusClient {
  readonly #origin: string;
  readonly #transport: Transport;
  readonly #signer: Signer;
  readonly #clock: Clock;
  readonly #nonce: NonceSource;
  readonly #timeoutMs: number;
  readonly #setTimer: SetTimer;

  constructor(options: StatusClientOptions) {
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
    Object.freeze(this);
  }

  /** True only for `"status"`; cancel/refresh/replace and native ops are unsupported in v1. */
  supports(operation: string): boolean {
    return operation === "status";
  }

  /** Throws `UNSUPPORTED_CAPABILITY` (field `"operation"`) for anything but `"status"`. */
  requireCapability(operation: string): void {
    if (!this.supports(operation)) {
      throw new LifecycleClientError("UNSUPPORTED_CAPABILITY", { field: "operation" });
    }
  }

  /** Read the job state for `requestDigest`. Exactly one transport call; never retries. */
  async status(requestDigest: string): Promise<JobStateV1> {
    if (typeof requestDigest !== "string" || !REQUEST_DIGEST_PATTERN.test(requestDigest)) {
      throw new LifecycleClientError("IDENTITY", { field: "request_digest" });
    }
    const path = STATUS_PATH_PREFIX + requestDigest;
    const ts = String(this.#timestamp());
    const nonce = this.#freshNonce();
    const message = new TextEncoder().encode([STATUS_METHOD, path, EMPTY_BODY_SHA256, ts, nonce].join("\n"));
    const response = await this.#signAndExchange(message, (mac) => ({
      method: STATUS_METHOD,
      url: this.#origin + path,
      headers: Object.freeze({
        [AUTH_HEADER]: `${AUTH_SCHEME} ts=${ts} nonce=${nonce} mac=${lowerHex(mac)}`,
        [ACCEPT_HEADER]: ACCEPT_VALUE,
      }),
      timeoutMs: this.#timeoutMs,
      maxResponseBytes: MAX_RESPONSE_BYTES,
    }));
    return interpret(response.status, response.body, requestDigest);
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

  /**
   * One deadline (the injected timer, `timeoutMs`) bounds signing plus the
   * single transport exchange:
   * - signer throws/rejects, returns anything but 32 bytes, or is still
   *   pending at the deadline: CLIENT_CONFIGURATION (field `signer`), zero
   *   transport calls;
   * - transport throws/rejects, returns no response object, or is still
   *   pending at the deadline: TRANSPORT_UNAVAILABLE, and its signal is aborted;
   * - a timer that throws or fires synchronously: CLIENT_CONFIGURATION
   *   (field `set_timer`), zero transport calls.
   * Anything arriving after the outcome is decided is ignored.
   */
  #signAndExchange(
    message: Uint8Array,
    build: (mac: Uint8Array) => Omit<TransportRequest, "signal">,
  ): Promise<RawResponse> {
    return new Promise((resolve, reject) => {
      const controller = new AbortController();
      let settled = false;
      let phase: "timer" | "sign" | "transport" = "timer";
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
        fail(phase === "transport" ? transportUnavailable() : configurationError(phase === "sign" ? "signer" : "set_timer"));
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
      phase = "sign";
      attempt(() => this.#signer(message)).then((signed) => {
        if (settled) return;
        // Everything the signer handed back is read inside this try: a forged or hostile
        // result is a signer fault, never an unhandled rejection or a transport error.
        let request: TransportRequest;
        try {
          const mac = copyUint8Array(signed);
          if (mac === null || mac.length !== MAC_BYTES) return fail(configurationError("signer"));
          request = Object.freeze({ ...build(mac), signal: controller.signal });
        } catch {
          return fail(configurationError("signer"));
        }
        phase = "transport";
        attempt(() => this.#transport(request)).then(
          (result) => {
            const response = responseParts(result);
            if (response === null) fail(transportUnavailable());
            else decide(() => resolve(response));
          },
          () => fail(transportUnavailable()),
        );
      }, () => fail(configurationError("signer")));
    });
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

/** Response handling in the order of Python `LifecycleClient._call`. */
function interpret(status: unknown, body: unknown, requestDigest: string): JobStateV1 {
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
  return validateJobState(payload, requestDigest, status);
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
