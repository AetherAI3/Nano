/**
 * Optional Web Crypto adapters (`globalThis.crypto`, Node >= 20 and browsers).
 *
 * The key is supplied by the caller as bytes; this module never reads keys
 * from the environment, files or storage. It is imported into a
 * non-extractable `CryptoKey` and the module's private copy is zeroed once
 * the import settles.
 */
import { copyUint8Array, lowerHex } from "./bytes.js";
import { configurationError } from "./errors.js";
import type { Sha256, Signer } from "./client.js";
import { MIN_KEY_BYTES } from "./wire.js";

const NONCE_BYTES = 16;

/**
 * HMAC-SHA256 signer for `StatusClient`. `key` must be a `Uint8Array` of at
 * least 16 bytes; otherwise `CLIENT_CONFIGURATION` (field `request_key`).
 * Without Web Crypto it throws `CLIENT_CONFIGURATION` (field `signer`).
 */
export function createHmacSha256Signer(key: Uint8Array): Signer {
  const copy = copyUint8Array(key);
  if (copy === null || copy.length < MIN_KEY_BYTES) {
    copy?.fill(0);
    throw configurationError("request_key");
  }
  const api = globalThis.crypto?.subtle;
  if (api === undefined) {
    copy.fill(0);
    throw configurationError("signer");
  }
  const cryptoKey = api
    .importKey("raw", copy, { name: "HMAC", hash: "SHA-256" }, false, ["sign"])
    .finally(() => copy.fill(0));
  // An import failure surfaces on use (as a signer failure), never as an unhandled rejection.
  cryptoKey.catch(() => undefined);
  return async (message: Uint8Array): Promise<Uint8Array> => {
    const mac = await api.sign("HMAC", await cryptoKey, new Uint8Array(message));
    return new Uint8Array(mac);
  };
}

/**
 * Nonce source for `StatusClient`: 16 bytes from `crypto.getRandomValues`
 * as 32 lowercase hex characters. Pass the function itself (`nonce:
 * createRandomNonce`); each call returns a fresh value.
 */
export function createRandomNonce(): string {
  const api = globalThis.crypto;
  if (api === undefined || typeof api.getRandomValues !== "function") throw configurationError("nonce");
  return lowerHex(api.getRandomValues(new Uint8Array(NONCE_BYTES)));
}

/**
 * SHA-256 capability for `LifecycleClient` (`replace` only), over
 * `crypto.subtle.digest("SHA-256", ...)`. Each call hashes a private copy of
 * the bytes and returns 32 bytes. Without Web Crypto it throws
 * `CLIENT_CONFIGURATION` (field `sha256`).
 */
export function createSha256(): Sha256 {
  const api = globalThis.crypto?.subtle;
  if (api === undefined || typeof api.digest !== "function") throw configurationError("sha256");
  return async (bytes: Uint8Array): Promise<Uint8Array> => {
    const copy = copyUint8Array(bytes);
    if (copy === null) throw configurationError("sha256");
    return new Uint8Array(await api.digest("SHA-256", copy));
  };
}
