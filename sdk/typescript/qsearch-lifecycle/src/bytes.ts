/** Byte helpers shared by the client and the adapters (internal, not exported from the package). */

/** True for any Uint8Array, including one from another realm (vm, iframe, jsdom) and Node Buffers. */
export function isUint8Array(value: unknown): value is Uint8Array {
  return value instanceof Uint8Array || Object.prototype.toString.call(value) === "[object Uint8Array]";
}

export function lowerHex(bytes: Uint8Array): string {
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}
