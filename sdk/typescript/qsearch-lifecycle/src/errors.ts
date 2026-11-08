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

export function transportUnavailable(): LifecycleClientError {
  return new LifecycleClientError("TRANSPORT_UNAVAILABLE", { retryable: true, nextAction: "POLL_STATUS" });
}
