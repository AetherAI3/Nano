/** Byte helpers shared by the client and the adapters (internal, not exported from the package). */

/** True for any Uint8Array, including one from another realm (vm, iframe, jsdom) and Node Buffers. */
export function isUint8Array(value: unknown): value is Uint8Array {
  return value instanceof Uint8Array || Object.prototype.toString.call(value) === "[object Uint8Array]";
}

/** `%TypedArray%.prototype[@@toStringTag]` reads the internal [[TypedArrayName]] slot: unforgeable, cross-realm. */
const TYPED_ARRAY_NAME = Object.getOwnPropertyDescriptor(
  Object.getPrototypeOf(Uint8Array.prototype) as object,
  Symbol.toStringTag,
)?.get as (this: unknown) => string | undefined;

/**
 * A private copy of a genuine Uint8Array (any realm, Buffer included), or null.
 * Uses internal slots only, so a spoofed tag, a subclass with hostile getters, or
 * a later mutation by the caller cannot change what the client reads.
 */
export function copyUint8Array(value: unknown): Uint8Array | null {
  try {
    if (TYPED_ARRAY_NAME.call(value) !== "Uint8Array") return null;
    return new Uint8Array(value as Uint8Array);
  } catch {
    return null;
  }
}

export function lowerHex(bytes: Uint8Array): string {
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}
