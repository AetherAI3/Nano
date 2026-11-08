import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { createHmacSha256Signer, createRandomNonce } from "../dist/webcrypto.js";
import { errorShape, thrown } from "../test-support/helpers.mjs";

const hex = (bytes) => Buffer.from(bytes).toString("hex");

test("RFC 4231 test case 1 (20-byte key)", async () => {
  const signer = createHmacSha256Signer(new Uint8Array(20).fill(0x0b));
  const mac = await signer(new TextEncoder().encode("Hi There"));
  assert.ok(mac instanceof Uint8Array);
  assert.equal(mac.length, 32);
  assert.equal(hex(mac), "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
});

test("matches node:crypto HMAC-SHA256 for synthetic keys and messages", async () => {
  for (const size of [16, 32, 64, 65, 200]) {
    const key = new Uint8Array(size).map((_, i) => (i * 7 + size) & 0xff);
    const message = new TextEncoder().encode(`GET\n/qsearch/v2/requests/x\n${size}`);
    const mac = await createHmacSha256Signer(key)(message);
    assert.equal(hex(mac), createHmac("sha256", Buffer.from(key)).update(message).digest("hex"));
  }
});

test("the key is copied: mutating the caller's array afterwards changes nothing", async () => {
  const key = new Uint8Array(32).fill(1);
  const signer = createHmacSha256Signer(key);
  key.fill(2);
  const mac = await signer(new Uint8Array([1, 2, 3]));
  assert.equal(hex(mac), createHmac("sha256", Buffer.alloc(32, 1)).update(Buffer.from([1, 2, 3])).digest("hex"));
});

test("keys shorter than 16 bytes or not Uint8Array are refused", () => {
  for (const key of [new Uint8Array(15), new Uint8Array(0), "0123456789abcdef0123", [1, 2, 3], new ArrayBuffer(32), undefined]) {
    assert.deepStrictEqual(errorShape(thrown(() => createHmacSha256Signer(key))), {
      code: "CLIENT_CONFIGURATION",
      field: "request_key",
      retryable: false,
      nextAction: "NONE",
      httpStatus: null,
    });
  }
});

test("createRandomNonce: 32 lowercase hex characters, fresh each call", () => {
  const seen = new Set();
  for (let i = 0; i < 200; i += 1) {
    const nonce = createRandomNonce();
    assert.match(nonce, /^[0-9a-f]{32}$/);
    seen.add(nonce);
  }
  assert.equal(seen.size, 200);
});
