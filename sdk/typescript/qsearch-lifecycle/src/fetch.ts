/**
 * Optional transport over a caller-supplied `fetch` (WHATWG). The fetch
 * function must be passed explicitly; this module never picks up a global.
 *
 * - `redirect: "manual"`: a 3xx (or a browser's opaque-redirect response) is
 *   returned as a 3xx status and the client maps it to UNEXPECTED_REDIRECT;
 *   the auth header is never forwarded to another origin.
 * - `credentials: "omit"`, `referrerPolicy: "no-referrer"`, `cache: "no-store"`:
 *   no ambient cookies or HTTP auth, no Referer header, and every read is a
 *   fresh authenticated request (never served from an HTTP cache).
 * - The body is streamed and reading stops at `maxResponseBytes + 1`, so an
 *   oversized body is detected without buffering it.
 * - The request's abort signal is passed to fetch and cancels the body read.
 */
import { isUint8Array } from "./bytes.js";
import { configurationError } from "./errors.js";
import type { Transport, TransportRequest, TransportResponse } from "./client.js";

/** Status reported for an opaque-redirect response, whose real 3xx code the platform hides. */
export const OPAQUE_REDIRECT_STATUS = 300;

export interface FetchInitLike {
  readonly method?: string;
  readonly headers?: unknown;
  readonly redirect?: string;
  readonly credentials?: string;
  readonly referrerPolicy?: string;
  readonly cache?: string;
  readonly signal?: unknown;
}

export interface FetchBodyReaderLike {
  read(): Promise<{ readonly done: boolean; readonly value?: Uint8Array | undefined }>;
  cancel(reason?: unknown): Promise<void>;
}

export interface FetchResponseLike {
  readonly status: number;
  readonly type?: string;
  readonly body: { getReader(): FetchBodyReaderLike } | null;
}

/**
 * `Init` is inferred from the supplied fetch (e.g. the platform `RequestInit`),
 * so `{ fetch: globalThis.fetch }` type-checks without a DOM-typed public API.
 * The adapter always passes a `FetchInitLike`-shaped object.
 */
export interface FetchTransportOptions<Init = FetchInitLike> {
  /** Typically `globalThis.fetch` or `undici.fetch`; called with `this` undefined. */
  fetch(input: string, init: Init): Promise<FetchResponseLike>;
}

function cancelQuietly(reader: FetchBodyReaderLike): void {
  Promise.resolve()
    .then(() => reader.cancel())
    .catch(() => undefined);
}

function discardBody(response: FetchResponseLike): void {
  try {
    if (response.body) cancelQuietly(response.body.getReader());
  } catch {
    // A locked or already-consumed body needs no cleanup.
  }
}

function concat(chunks: readonly Uint8Array[], total: number): Uint8Array {
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

async function readCapped(response: FetchResponseLike, request: TransportRequest): Promise<Uint8Array> {
  const { body } = response;
  if (body === null || body === undefined) return new Uint8Array(0);
  const reader = body.getReader();
  const limit = request.maxResponseBytes + 1;
  const onAbort = (): void => cancelQuietly(reader);
  request.signal.addEventListener("abort", onAbort, { once: true });
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (total < limit) {
      if (request.signal.aborted) throw new Error("aborted");
      const { done, value } = await reader.read();
      if (done) return concat(chunks, total);
      if (!isUint8Array(value)) throw new Error("non-byte chunk");
      const room = limit - total;
      const kept = value.length > room ? value.subarray(0, room) : value;
      chunks.push(kept.slice());
      total += kept.length;
    }
    cancelQuietly(reader); // one byte over the cap is enough to refuse the body
    return concat(chunks, total);
  } catch (error: unknown) {
    cancelQuietly(reader);
    throw error;
  } finally {
    request.signal.removeEventListener("abort", onAbort);
  }
}

/** Build a `Transport` from an explicitly supplied fetch function. */
export function createFetchTransport<Init = FetchInitLike>(options: FetchTransportOptions<Init>): Transport {
  const fetchFn = typeof options === "object" && options !== null ? options.fetch : undefined;
  if (typeof fetchFn !== "function") throw configurationError("transport");
  return async (request: TransportRequest): Promise<TransportResponse> => {
    if (request.signal.aborted) throw new Error("aborted");
    const init: FetchInitLike = {
      method: request.method,
      headers: { ...request.headers },
      redirect: "manual",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      cache: "no-store",
      signal: request.signal,
    };
    const response = await fetchFn.call(undefined, request.url, init as Init);
    if (typeof response !== "object" || response === null) throw new Error("no response");
    if (response.type === "opaqueredirect") {
      discardBody(response);
      return { status: OPAQUE_REDIRECT_STATUS, body: new Uint8Array(0) };
    }
    const { status } = response;
    if (typeof status === "number" && status >= 300 && status < 400) {
      discardBody(response);
      return { status, body: new Uint8Array(0) };
    }
    return { status, body: await readCapped(response, request) };
  };
}
