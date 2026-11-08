import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

import { StatusClient } from "../dist/index.js";
import { OPAQUE_REDIRECT_STATUS, createFetchTransport } from "../dist/fetch.js";
import { DIGEST, STATUS_URL, bytes, errorShape, fakeTimer, jsonBytes, options, queued, rejection, thrown, tick } from "../test-support/helpers.mjs";

const CAP = 1_048_576;

/** A reader over fixed chunks that records reads and cancellation. */
function chunkedBody(chunks) {
  const log = { reads: 0, cancelled: false };
  let index = 0;
  const reader = {
    read: async () => {
      log.reads += 1;
      if (log.cancelled || index >= chunks.length) return { done: true, value: undefined };
      return { done: false, value: chunks[index++] };
    },
    cancel: async () => {
      log.cancelled = true;
    },
  };
  return { body: { getReader: () => reader }, log };
}

function request(overrides = {}) {
  const controller = new AbortController();
  return {
    method: "GET",
    url: STATUS_URL,
    headers: Object.freeze({ "X-Aether-Qsearch-Auth": "v1 ts=1 nonce=00 mac=00", Accept: "application/json" }),
    timeoutMs: 30_000,
    maxResponseBytes: CAP,
    signal: controller.signal,
    controller,
    ...overrides,
  };
}

test("fetch must be supplied explicitly", () => {
  for (const bad of [undefined, null, {}, { fetch: "x" }]) {
    const error = thrown(() => createFetchTransport(bad));
    assert.deepStrictEqual(errorShape(error), { code: "CLIENT_CONFIGURATION", field: "transport", retryable: false, nextAction: "NONE", httpStatus: null });
  }
});

test("calls fetch once with manual redirects, no credentials, the exact headers and the signal", async () => {
  const seen = [];
  const transport = createFetchTransport({
    fetch: function (input, init) {
      seen.push({ input, init, self: this });
      return Promise.resolve({ status: 200, type: "basic", body: chunkedBody([bytes("{}")]).body });
    },
  });
  const req = request();
  const response = await transport(req);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].input, STATUS_URL);
  assert.equal(seen[0].self, undefined, "fetch called without a receiver");
  assert.deepStrictEqual(Object.keys(seen[0].init).sort(), ["cache", "credentials", "headers", "method", "redirect", "referrerPolicy", "signal"]);
  assert.equal(seen[0].init.method, "GET");
  assert.equal(seen[0].init.redirect, "manual");
  assert.equal(seen[0].init.credentials, "omit");
  assert.equal(seen[0].init.referrerPolicy, "no-referrer");
  assert.equal(seen[0].init.cache, "no-store");
  assert.strictEqual(seen[0].init.signal, req.signal);
  assert.deepStrictEqual(seen[0].init.headers, { ...req.headers });
  assert.deepStrictEqual(response, { status: 200, body: bytes("{}") });
});

test("3xx and opaque redirects are surfaced as redirect statuses; bodies discarded", async () => {
  const redirect = chunkedBody([bytes("moved")]);
  const t1 = createFetchTransport({ fetch: async () => ({ status: 302, body: redirect.body }) });
  assert.deepStrictEqual(await t1(request()), { status: 302, body: new Uint8Array(0) });
  await tick();
  assert.equal(redirect.log.cancelled, true);
  const t2 = createFetchTransport({ fetch: async () => ({ status: 0, type: "opaqueredirect", body: null }) });
  assert.deepStrictEqual(await t2(request()), { status: OPAQUE_REDIRECT_STATUS, body: new Uint8Array(0) });
  assert.ok(OPAQUE_REDIRECT_STATUS >= 300 && OPAQUE_REDIRECT_STATUS < 400);
  for (const t of [t1, t2]) {
    const client = new StatusClient(options({ transport: t }));
    const error = await rejection(client.status(DIGEST));
    assert.equal(error.code, "UNEXPECTED_REDIRECT");
  }
});

test("streaming read stops at cap + 1 and cancels the stream", async () => {
  const chunk = new Uint8Array(100_000).fill(0x20);
  const many = chunkedBody(Array(50).fill(chunk)); // 5 MB on offer
  const transport = createFetchTransport({ fetch: async () => ({ status: 200, body: many.body }) });
  const response = await transport(request());
  assert.equal(response.body.length, CAP + 1);
  assert.ok(many.log.reads <= 11, `reads ${many.log.reads}`);
  await tick();
  assert.equal(many.log.cancelled, true);
});

test("a body of exactly the cap is returned whole", async () => {
  const exact = chunkedBody([new Uint8Array(CAP - 10), new Uint8Array(10)]);
  const transport = createFetchTransport({ fetch: async () => ({ status: 200, body: exact.body }) });
  assert.equal((await transport(request())).body.length, CAP);
});

test("null body is empty; non-byte chunks reject", async () => {
  const empty = createFetchTransport({ fetch: async () => ({ status: 204, body: null }) });
  assert.deepStrictEqual(await empty(request()), { status: 204, body: new Uint8Array(0) });
  const bad = createFetchTransport({ fetch: async () => ({ status: 200, body: chunkedBody(["text"]).body }) });
  await assert.rejects(bad(request()));
});

test("abort: signal aborted before the call, or during the body read, rejects", async () => {
  let fetched = 0;
  const t = createFetchTransport({ fetch: async () => (fetched++, { status: 200, body: null }) });
  const pre = request();
  pre.controller.abort();
  await assert.rejects(t(pre));
  assert.equal(fetched, 0);

  let cancelled = false;
  let releaseRead;
  const hanging = {
    getReader: () => ({
      read: () => new Promise((resolve) => (releaseRead = resolve)),
      cancel: async () => {
        cancelled = true;
        releaseRead?.({ done: true, value: undefined });
      },
    }),
  };
  const slow = createFetchTransport({ fetch: async () => ({ status: 200, body: hanging }) });
  const req = request();
  const pending = slow(req);
  await tick();
  req.controller.abort();
  await pending.catch(() => undefined);
  assert.equal(cancelled, true, "abort cancels the body stream");
});

test("client timeout aborts the fetch signal and maps to TRANSPORT_UNAVAILABLE", async () => {
  let signal;
  const transport = createFetchTransport({
    fetch: (_, init) =>
      new Promise((_, reject) => {
        signal = init.signal;
        init.signal.addEventListener("abort", () => reject(new Error("AbortError s3cr3t")));
      }),
  });
  const timer = fakeTimer();
  const client = new StatusClient(options({ transport, setTimer: timer.setTimer }));
  const pending = client.status(DIGEST);
  await tick();
  timer.fire();
  const error = await rejection(pending);
  assert.equal(error.code, "TRANSPORT_UNAVAILABLE");
  assert.equal(signal.aborted, true);
  assert.ok(!JSON.stringify(error).includes("s3cr3t"));
});

test("fetch rejection (network error) maps to TRANSPORT_UNAVAILABLE", async () => {
  const transport = createFetchTransport({ fetch: async () => { throw new TypeError("fetch failed: ECONNREFUSED 10.0.0.1"); } });
  const client = new StatusClient(options({ transport }));
  const error = await rejection(client.status(DIGEST));
  assert.equal(error.code, "TRANSPORT_UNAVAILABLE");
  assert.ok(!String(error).includes("10.0.0.1"));
});

test("end to end with the platform Response class (no network)", async () => {
  const transport = createFetchTransport({
    fetch: async (url, init) => {
      assert.equal(url, STATUS_URL);
      assert.equal(init.redirect, "manual");
      return new Response(jsonBytes(queued()), { status: 200, headers: { "content-type": "application/json" } });
    },
  });
  const client = new StatusClient(options({ transport }));
  assert.deepStrictEqual(await client.status(DIGEST), queued());
  const oversize = createFetchTransport({
    fetch: async () => new Response(new Uint8Array(CAP + 50).fill(0x20), { status: 200 }),
  });
  const error = await rejection(new StatusClient(options({ transport: oversize })).status(DIGEST));
  assert.equal(error.code, "INVALID_RESPONSE");
  const redirect = createFetchTransport({ fetch: async () => new Response(null, { status: 307, headers: { location: "https://elsewhere.test/" } }) });
  const moved = await rejection(new StatusClient(options({ transport: redirect })).status(DIGEST));
  assert.equal(moved.code, "UNEXPECTED_REDIRECT");
  assert.equal(moved.httpStatus, 307);
});

test("chunks from another realm are accepted as bytes", async () => {
  const foreign = runInNewContext("new Uint8Array([123, 125])");
  assert.equal(foreign instanceof Uint8Array, false, "really from another realm");
  const transport = createFetchTransport({ fetch: async () => ({ status: 200, body: chunkedBody([foreign]).body }) });
  assert.deepStrictEqual(await transport(request()), { status: 200, body: bytes("{}") });
});
