import type { NextAction } from "./wire.js";

export interface LifecycleClientErrorInit {
  readonly field?: string | null;
  readonly retryable?: boolean;
  readonly nextAction?: NextAction;
  readonly httpStatus?: number | null;
}

/**
 * The only error a client surfaces. It carries five pieces of stable metadata
 * and a constant message. It never holds the URL, key, auth header, nonce,
 * response body or an underlying exception (there is deliberately no `cause`).
 */
export class LifecycleClientError extends Error {
  readonly code: string;
  readonly field: string | null;
  readonly retryable: boolean;
  readonly nextAction: NextAction;
  readonly httpStatus: number | null;

  constructor(code: string, init: LifecycleClientErrorInit = {}) {
    super(`q-search lifecycle failed (${code})`);
    this.code = code;
    this.field = init.field ?? null;
    this.retryable = init.retryable === true;
    this.nextAction = init.nextAction ?? "NONE";
    this.httpStatus = init.httpStatus ?? null;
    Object.defineProperty(this, "name", { value: "LifecycleClientError", enumerable: false });
    Object.freeze(this);
  }
}

export function configurationError(field: string | null): LifecycleClientError {
  return new LifecycleClientError("CLIENT_CONFIGURATION", { field });
}

export function invalidResponse(httpStatus: number | null, field: string | null = null): LifecycleClientError {
  return new LifecycleClientError("INVALID_RESPONSE", {
    field,
    nextAction: "MANUAL_RECONCILIATION",
    httpStatus,
  });
}

/**
 * Transport failure or client timeout. A GET read only needs another read
 * (`POLL_STATUS`); a POST control's acknowledgement is unknown, so the caller
 * must observe and reconcile (`MANUAL_RECONCILIATION`). Never a job state.
 */
export function transportUnavailable(method: "GET" | "POST" = "GET"): LifecycleClientError {
  return new LifecycleClientError("TRANSPORT_UNAVAILABLE", {
    retryable: true,
    nextAction: method === "GET" ? "POLL_STATUS" : "MANUAL_RECONCILIATION",
  });
}

export function unsupportedCapability(): LifecycleClientError {
  return new LifecycleClientError("UNSUPPORTED_CAPABILITY", { field: "operation" });
}

export function invalidRequest(field: string): LifecycleClientError {
  return new LifecycleClientError("INVALID_REQUEST", { field });
}
