import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { LifecycleClientError, StatusClient } from "../dist/index.js";
import { createHmacSha256Signer } from "../dist/webcrypto.js";
import {
  CLOCK_S,
  DIGEST,
  FAKE_MAC,
  NONCE,
  OTHER_DIGEST,
  STATUS_URL,
  bytes,
  devSet,
  errorShape,
  fakeTimer,
  fakeTransport,
  jsonBytes,
  options,
  queued,
  rejection,
  respondWith,
  thrown,
  tick,
} from "../test-support/helpers.mjs";

const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const INVALID = { code: "INVALID_RESPONSE", retryable: false, nextAction: "MANUAL_RECONCILIATION" };

function clientWith(respond, overrides = {}) {
  const fake = fakeTransport(respond);
  const timer = fakeTimer();
  const client = new StatusClient(options({ transport: fake.transport, setTimer: timer.setTimer, ...overrides }));
  return { client, calls: fake.calls, timer };
}

async function statusError(respond, overrides) {
  const { client, calls } = clientWith(respond, overrides);
  const error = await rejection(client.status(DIGEST));
  assert.ok(error instanceof LifecycleClientError);
  assert.equal(calls.length, 1, "exactly one transport call");
  return error;
}

test("status: exactly one GET with exactly the two contract headers; deep-frozen result", async () => {
  const { client, calls, timer } = clientWith(respondWith(200, jsonBytes(queued())));
  const state = await client.status(DIGEST);
  assert.equal(calls.length, 1);
  const [request] = calls;
  assert.equal(request.method, "GET");
  assert.equal(request.url, STATUS_URL);
  assert.deepStrictEqual(Object.keys(request.headers), ["X-Aether-Qsearch-Auth", "Accept"]);
  assert.equal(request.headers.Accept, "application/json");
  const ts = Math.floor(CLOCK_S);
  const mac = Buffer.from(FAKE_MAC).toString("hex");
  assert.equal(request.headers["X-Aether-Qsearch-Auth"], `v1 ts=${ts} nonce=${NONCE} mac=${mac}`);
  assert.equal(request.timeoutMs, 30_000);
  assert.equal(request.maxResponseBytes, 1_048_576);
  assert.equal(request.signal.aborted, false);
  assert.ok(Object.isFrozen(request) && Object.isFrozen(request.headers));
  assert.deepStrictEqual(Object.keys(request).sort(), ["headers", "maxResponseBytes", "method", "signal", "timeoutMs", "url"]);
  assert.deepStrictEqual(state, queued());
  assert.ok(Object.isFrozen(state) && Object.isFrozen(state.usage));
  assert.equal(timer.timers.length, 1);
  assert.equal(timer.timers[0].ms, 30_000);
  assert.equal(timer.timers[0].cancelled, true, "timer cancelled after the response");
});

test("signing bytes: canonical message verified the way the Python service does", async () => {
  const key = new Uint8Array(32).map((_, i) => i + 1);
  let message;
  const signer = createHmacSha256Signer(key);
  const { client, calls } = clientWith(respondWith(200, jsonBytes(queued())), {
    signer: async (m) => {
      message = m;
      return signer(m);
    },
  });
  await client.status(DIGEST);
  const path = `/qsearch/v2/requests/${DIGEST}`;
  const expected = `GET\n${path}\n${EMPTY_SHA256}\n${Math.floor(CLOCK_S)}\n${NONCE}`;
  assert.equal(new TextDecoder().decode(message), expected);
  const header = calls[0].headers["X-Aether-Qsearch-Auth"];
  const match = /^v1 ts=(\d{1,12}) nonce=([0-9a-f]{16,64}) mac=([0-9a-f]{64})$/.exec(header);
  assert.ok(match, header);
  const serverMac = createHmac("sha256", Buffer.from(key)).update(`GET\n${path}\n${EMPTY_SHA256}\n${match[1]}\n${match[2]}`).digest("hex");
  assert.equal(match[3], serverMac);
});

test("base URL: trailing slash removed; origin used verbatim", async () => {
  for (const [base, url] of [
    ["http://127.0.0.1:8080/", `http://127.0.0.1:8080/qsearch/v2/requests/${DIGEST}`],
    ["https://h", `https://h/qsearch/v2/requests/${DIGEST}`],
    ["http://[::1]:9000", `http://[::1]:9000/qsearch/v2/requests/${DIGEST}`],
    ["HTTPS://Example.test/", `HTTPS://Example.test/qsearch/v2/requests/${DIGEST}`],
  ]) {
    const { client, calls } = clientWith(respondWith(200, jsonBytes(queued())), { baseUrl: base });
    await client.status(DIGEST);
    assert.equal(calls[0].url, url, base);
  }
});

test("base URL refusals are CLIENT_CONFIGURATION field base_url (CONTRACT 1.1)", () => {
  for (const base of [
    "",
    "qsearch.test",
    "ftp://h",
    "file:///etc",
    "http://",
    "http:///x",
    "http://user@h",
    "http://user:pw@h",
    "http://h/path",
    "http://h//",
    "http://h?x=1",
    "http://h/?",
    "http://h#frag",
    "http://h/#",
    "http://h:0",
    "http://h:00",
    "http://h:65536",
    "http://h:x",
    "http://h h",
    " http://h",
    "http://h\n",
    "http://hé.test",
    "http://[zz]",
    "http://[1234]",
    "http://[1:2]:80/",
    "http://1.2.3.4.5",
    "http://h:123456",
    "javascript:alert(1)",
    "//h",
    42,
    null,
    undefined,
  ]) {
    const error = thrown(() => new StatusClient(options({ baseUrl: base })));
    assert.deepStrictEqual(errorShape(error), {
      code: "CLIENT_CONFIGURATION",
      field: "base_url",
      retryable: false,
      nextAction: "NONE",
      httpStatus: null,
    }, String(base));
  }
});

test("required capabilities and timeout are validated at construction", () => {
  const cases = [
    [{ transport: undefined }, "transport"],
    [{ signer: undefined }, "signer"],
    [{ clock: 1 }, "clock"],
    [{ nonce: "abc" }, "nonce"],
    [{ timeoutMs: 0 }, "timeout_ms"],
    [{ timeoutMs: -1 }, "timeout_ms"],
    [{ timeoutMs: Number.NaN }, "timeout_ms"],
    [{ timeoutMs: Number.POSITIVE_INFINITY }, "timeout_ms"],
    [{ timeoutMs: 2 ** 31 }, "timeout_ms"],
    [{ timeoutMs: "30000" }, "timeout_ms"],
    [{ setTimer: 5 }, "set_timer"],
  ];
  for (const [override, field] of cases) {
    const error = thrown(() => new StatusClient(options(override)));
    assert.equal(error.code, "CLIENT_CONFIGURATION");
    assert.equal(error.field, field, JSON.stringify(override));
  }
  assert.equal(thrown(() => new StatusClient(undefined)).code, "CLIENT_CONFIGURATION");
});

test("malformed digests are IDENTITY with zero transport calls", async () => {
  const { client, calls } = clientWith(respondWith(200, jsonBytes(queued())));
  for (const digest of [
    "",
    "sha256:",
    DIGEST.toUpperCase(),
    "sha256:" + "AB".repeat(32),
    "sha256:" + "ab".repeat(31),
    DIGEST + "0",
    DIGEST + "/cancel",
    DIGEST + "\n",
    "sha512:" + "ab".repeat(32),
    "sha256:../" + "ab".repeat(31),
    "sha256%3A" + "ab".repeat(32),
    42,
    null,
    undefined,
  ]) {
    const error = await rejection(client.status(digest));
    assert.deepStrictEqual(errorShape(error), {
      code: "IDENTITY",
      field: "request_digest",
      retryable: false,
      nextAction: "NONE",
      httpStatus: null,
    });
  }
  assert.equal(calls.length, 0);
});

test("clock, nonce and signer misconfiguration refuse before I/O", async () => {
  const cases = [
    [{ clock: () => Number.NaN }, "clock"],
    [{ clock: () => Number.POSITIVE_INFINITY }, "clock"],
    [{ clock: () => -1 }, "clock"],
    [{ clock: () => 2 ** 53 }, "clock"],
    [{ clock: () => "1700000000" }, "clock"],
    [{ clock: () => true }, "clock"],
    [{ clock: () => 10n }, "clock"],
    [{ clock: () => { throw new Error("clock exploded"); } }, "clock"],
    [{ nonce: () => "ABCDEF0123456789" }, "nonce"],
    [{ nonce: () => "0123456789abcde" }, "nonce"],
    [{ nonce: () => "a".repeat(65) }, "nonce"],
    [{ nonce: () => "0123456789abcdef\n" }, "nonce"],
    [{ nonce: () => 1234567890123456 }, "nonce"],
    [{ nonce: () => { throw new Error("nonce exploded"); } }, "nonce"],
    [{ signer: async () => new Uint8Array(31) }, "signer"],
    [{ signer: async () => new Uint8Array(33) }, "signer"],
    [{ signer: async () => new ArrayBuffer(32) }, "signer"],
    [{ signer: async () => Array(32).fill(0) }, "signer"],
    [{ signer: async () => "ab".repeat(32) }, "signer"],
    [{ signer: async () => { throw new Error("hsm offline"); } }, "signer"],
    [{ signer: () => { throw new Error("sync throw"); } }, "signer"],
  ];
  for (const [override, field] of cases) {
    const { client, calls } = clientWith(respondWith(200, jsonBytes(queued())), override);
    const error = await rejection(client.status(DIGEST));
    assert.deepStrictEqual(errorShape(error), {
      code: "CLIENT_CONFIGURATION",
      field,
      retryable: false,
      nextAction: "NONE",
      httpStatus: null,
    }, `${field}: ${override[field]}`);
    assert.equal(calls.length, 0);
  }
});

test("clock edge values: 0 and 2^53-1 accepted; fractional seconds floored", async () => {
  for (const [now, ts] of [[0, "0"], [-0, "0"], [9007199254740991, "9007199254740991"], [1_700_000_000.999, "1700000000"]]) {
    const { client, calls } = clientWith(respondWith(200, jsonBytes(queued())), { clock: () => now });
    await client.status(DIGEST);
    assert.match(calls[0].headers["X-Aether-Qsearch-Auth"], new RegExp(`^v1 ts=${ts} `));
  }
});

test("timeout: TRANSPORT_UNAVAILABLE via the injected timer, aborts the signal, fabricates no state", async () => {
  let release;
  const { client, calls, timer } = clientWith(() => new Promise((resolve) => (release = resolve)));
  const pending = client.status(DIGEST);
  await tick();
  assert.equal(calls.length, 1);
  assert.equal(timer.timers[0].ms, 30_000);
  timer.fire();
  const error = await rejection(pending);
  assert.deepStrictEqual(errorShape(error), {
    code: "TRANSPORT_UNAVAILABLE",
    field: null,
    retryable: true,
    nextAction: "POLL_STATUS",
    httpStatus: null,
  });
  assert.equal(calls[0].signal.aborted, true);
  release({ status: 200, body: jsonBytes(queued()) }); // late result is ignored
  await tick();
  assert.equal(calls.length, 1, "no retry after timeout");
});

test("timeout is the client bound only: independent of envelope deadlines", async () => {
  const expired = queued();
  expired.state = "QUEUE_EXPIRED";
  expired.deadlines.queue_at = 1; // long past relative to the clock
  expired.deadlines.queue_expired = true;
  expired.next_action = "REQUEST_CANCEL";
  const { client, calls, timer } = clientWith(respondWith(200, jsonBytes(expired)), { timeoutMs: 1234 });
  const state = await client.status(DIGEST);
  assert.deepStrictEqual(state, expired, "deadline data passed through, not recomputed");
  assert.equal(timer.timers[0].ms, 1234);
  assert.equal(calls[0].timeoutMs, 1234);
  await tick();
  assert.equal(calls.length, 1, "REQUEST_CANCEL triggers nothing");
});

test("a late rejection after timeout is swallowed (no unhandled rejection)", async () => {
  let fail;
  const { client, timer } = clientWith(() => new Promise((_, reject) => (fail = reject)));
  const pending = client.status(DIGEST);
  await tick();
  timer.fire();
  assert.equal((await rejection(pending)).code, "TRANSPORT_UNAVAILABLE");
  fail(new Error("late"));
  await tick();
});

test("real default timer: a hung transport times out", async () => {
  const fake = fakeTransport(() => new Promise(() => undefined));
  const client = new StatusClient(options({ transport: fake.transport, setTimer: undefined, timeoutMs: 20 }));
  const error = await rejection(client.status(DIGEST));
  assert.equal(error.code, "TRANSPORT_UNAVAILABLE");
  assert.equal(fake.calls[0].signal.aborted, true);
});

test("transport throw/reject never leaks URL, key, header, nonce or exception text", async () => {
  const secretKey = new Uint8Array(32).fill(0x5e);
  const secretHex = Buffer.from(secretKey).toString("hex");
  const secretNonce = "feedfacecafebeef0011223344556677";
  const secretBase = "https://secret-host.internal.test:9443";
  const secrets = [secretHex, secretNonce, "secret-host", "s3cr3t-exception", "X-Aether", "mac=", "ts=", DIGEST];
  for (const respond of [
    () => {
      throw new Error("s3cr3t-exception at https://secret-host.internal.test");
    },
    async () => {
      throw new Error(`s3cr3t-exception ${secretHex} ${secretNonce}`);
    },
    () => Promise.reject("s3cr3t-exception"),
    async () => null,
    async () => 42,
  ]) {
    const { client, calls } = clientWith(respond, {
      baseUrl: secretBase,
      nonce: () => secretNonce,
      signer: createHmacSha256Signer(secretKey),
    });
    const error = await rejection(client.status(DIGEST));
    assert.equal(calls.length, 1);
    assert.equal(error.code, "TRANSPORT_UNAVAILABLE");
    assert.equal(error.message, "q-search lifecycle failed (TRANSPORT_UNAVAILABLE)");
    assert.equal(error.cause, undefined);
    assert.equal("cause" in error, false);
    const own = Object.getOwnPropertyNames(error).map((k) => String(error[k]));
    const views = [String(error), JSON.stringify(error), error.message, String(error.stack), ...own];
    for (const view of views) for (const secret of secrets) assert.ok(!view.includes(secret), `${secret} leaked in ${view}`);
    assert.deepStrictEqual(Object.getOwnPropertyNames(error).sort(), ["code", "field", "httpStatus", "message", "name", "nextAction", "retryable", "stack"].sort());
    assert.ok(Object.isFrozen(error));
  }
});

test("redirects: any 3xx is UNEXPECTED_REDIRECT, never followed", async () => {
  for (const status of [300, 301, 302, 303, 304, 307, 308, 399]) {
    const error = await statusError(respondWith(status, bytes("not json at all")));
    assert.deepStrictEqual(errorShape(error), {
      code: "UNEXPECTED_REDIRECT",
      field: null,
      retryable: false,
      nextAction: "NONE",
      httpStatus: status,
    });
  }
});

test("status codes outside integer 100..599 are INVALID_RESPONSE without http_status", async () => {
  for (const status of [0, 99, 600, 200.5, "200", Number.NaN, null, undefined, 200n]) {
    const error = await statusError(respondWith(status, jsonBytes(queued())));
    assert.deepStrictEqual(errorShape(error), { ...INVALID, field: null, httpStatus: null }, String(status));
  }
});

test("body cap: exactly 1,048,576 bytes accepted, one more refused", async () => {
  const json = JSON.stringify(queued());
  const exact = bytes(json + " ".repeat(1_048_576 - json.length));
  assert.equal(exact.length, 1_048_576);
  const { client } = clientWith(respondWith(200, exact));
  assert.deepStrictEqual(await client.status(DIGEST), queued());
  const over = bytes(json + " ".repeat(1_048_577 - json.length));
  const error = await statusError(respondWith(200, over));
  assert.deepStrictEqual(errorShape(error), { ...INVALID, field: null, httpStatus: 200 });
});

test("body must be a Uint8Array", async () => {
  for (const body of ["{}", new ArrayBuffer(2), [123, 125], null, undefined, { length: 2 }]) {
    const error = await statusError(respondWith(200, body));
    assert.deepStrictEqual(errorShape(error), { ...INVALID, field: null, httpStatus: 200 });
  }
  const { client } = clientWith(respondWith(200, Buffer.from(JSON.stringify(queued()))));
  assert.deepStrictEqual(await client.status(DIGEST), queued(), "a Node Buffer is a Uint8Array");
});

test("encoding and JSON violations are INVALID_RESPONSE", async () => {
  const good = JSON.stringify(queued());
  const cases = {
    bom: new Uint8Array([0xef, 0xbb, 0xbf, ...bytes(good)]),
    invalidUtf8: new Uint8Array([0x7b, 0xff, 0x7d]),
    overlongUtf8: new Uint8Array([0x22, 0xc0, 0xaf, 0x22]),
    encodedSurrogate: new Uint8Array([0x22, 0xed, 0xa0, 0x80, 0x22]),
    utf16: new Uint8Array(Buffer.from(good, "utf16le")),
    empty: new Uint8Array(0),
    truncated: bytes(good.slice(0, -1)),
    trailing: bytes(good + "x"),
    twoDocs: bytes(good + good),
    array: bytes("[" + good + "]"),
    string: bytes('"ok"'),
    nullDoc: bytes("null"),
    duplicateTop: bytes(good.replace('{"schema":', '{"state":"RUNNING","schema":')),
    duplicateNested: bytes(good.replace('"reservation_status":"HELD"', '"reservation_status":"HELD","reservation_status":"SETTLED"')),
    nan: bytes(good.replace("3600", "NaN")),
    infinity: bytes(good.replace("3600", "Infinity")),
    hugeExponent: bytes(good.replace("3600", "1e400")),
    unsafeInt: bytes(good.replace("3600", "9007199254740993")),
    negative: bytes(good.replace("3600", "-1")),
    boolAsNumber: bytes(good.replace('"terminal":false', '"terminal":0')),
    numberAsBool: bytes(good.replace("3600", "true")),
    stringNumber: bytes(good.replace("3600", '"3600"')),
    missingField: bytes(good.replace(',"next_action":"POLL_STATUS"', "")),
    extraField: bytes(good.replace('{"schema":', '{"extra":1,"schema":')),
    protoKey: bytes(good.replace('{"schema":', '{"__proto__":{"polluted":1},"schema":')),
    deep: bytes('{"a":' + "[".repeat(70) + "]".repeat(70) + "}"),
  };
  for (const [name, body] of Object.entries(cases)) {
    const { client, calls } = clientWith(respondWith(200, body));
    const error = await rejection(client.status(DIGEST));
    assert.equal(calls.length, 1, name);
    assert.equal(error.code, "INVALID_RESPONSE", name);
    assert.equal(error.httpStatus, 200, name);
    assert.equal(error.retryable, false, name);
    assert.equal(error.nextAction, "MANUAL_RECONCILIATION", name);
  }
  assert.equal({}.polluted, undefined);
});

test("digest binding and schema identity: INVALID_RESPONSE with field null (Python parity)", async () => {
  const other = await statusError(respondWith(200, jsonBytes(queued(OTHER_DIGEST))));
  assert.deepStrictEqual(errorShape(other), { ...INVALID, field: null, httpStatus: 200 });
  const v2 = await statusError(respondWith(200, jsonBytes({ ...queued(), schema: "nano.qsearch.job-state.v2" })));
  assert.deepStrictEqual(errorShape(v2), { ...INVALID, field: null, httpStatus: 200 });
});

test("non-2xx bodies are mapped through allowlists; free text discarded", async () => {
  const cases = [
    [404, { error: "no such request s3cr3t", code: "REQUEST_NOT_FOUND", field: "request_digest", retryable: false, next_action: "NONE" },
      { code: "REQUEST_NOT_FOUND", field: "request_digest", retryable: false, nextAction: "NONE", httpStatus: 404 }],
    [401, { error: "bad mac", code: "UNAUTHORIZED" },
      { code: "UNAUTHORIZED", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 401 }],
    [503, { error: "x", code: "STORE_UNAVAILABLE", retryable: true, next_action: "POLL_STATUS" },
      { code: "STORE_UNAVAILABLE", field: null, retryable: true, nextAction: "POLL_STATUS", httpStatus: 503 }],
    [500, { error: "x", code: "SOMETHING_NEW", field: "secret_field", retryable: "true", next_action: "RETRY_NOW" },
      { code: "REMOTE_ERROR", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 500 }],
    [500, { code: 7, field: 7, retryable: 1, next_action: 7 },
      { code: "REMOTE_ERROR", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 500 }],
    [418, {},
      { code: "REMOTE_ERROR", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 418 }],
    [400, { code: "toString", field: "constructor", next_action: "__proto__" },
      { code: "REMOTE_ERROR", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 400 }],
    [199, { code: "NOT_ENABLED" },
      { code: "NOT_ENABLED", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 199 }],
    [599, queued(),
      { code: "REMOTE_ERROR", field: null, retryable: false, nextAction: "POLL_STATUS", httpStatus: 599 }],
  ];
  for (const [status, body, expected] of cases) {
    const error = await statusError(respondWith(status, jsonBytes(body)));
    assert.deepStrictEqual(errorShape(error), expected, JSON.stringify(body));
    assert.equal(error.message, `q-search lifecycle failed (${expected.code})`);
    assert.ok(!JSON.stringify(error).includes("s3cr3t") && !String(error).includes("bad mac"));
  }
  for (const body of [bytes("not json"), bytes("[]"), bytes('{"code":"A","code":"B"}'), bytes("")]) {
    const error = await statusError(respondWith(500, body));
    assert.deepStrictEqual(errorShape(error), { ...INVALID, field: null, httpStatus: 500 });
  }
});

test("errors, timeouts and next_action never cause another call; N reads = N calls", async () => {
  for (const respond of [
    respondWith(503, jsonBytes({ code: "STORE_UNAVAILABLE", retryable: true, next_action: "POLL_STATUS" })),
    respondWith(200, bytes("{")),
    () => Promise.reject(new Error("down")),
  ]) {
    const { client, calls } = clientWith(respond);
    await rejection(client.status(DIGEST));
    await tick();
    await tick();
    assert.equal(calls.length, 1);
  }
  const histories = Object.values(devSet());
  let n = 0;
  const { client, calls } = clientWith((_, i) => Promise.resolve({ status: 200, body: jsonBytes(histories[i % histories.length]) }), {
    nonce: () => (n++).toString(16).padStart(32, "0"),
  });
  const reads = [];
  for (let i = 0; i < histories.length; i += 1) reads.push(await client.status(DIGEST));
  await tick();
  assert.equal(calls.length, histories.length);
  assert.ok(calls.every((c) => c.method === "GET" && c.url === STATUS_URL));
  assert.equal(new Set(calls.map((c) => c.headers["X-Aether-Qsearch-Auth"])).size, histories.length, "fresh nonce per read");
  assert.deepStrictEqual(reads, histories);
});

test("capabilities: status only; no write methods exist", () => {
  const client = new StatusClient(options());
  assert.equal(client.supports("status"), true);
  for (const op of ["cancel", "refresh", "replace", "submit", "native:ibm.cancel_job", "STATUS", "", "__proto__"]) {
    assert.equal(client.supports(op), false, op);
    assert.deepStrictEqual(errorShape(thrown(() => client.requireCapability(op))), {
      code: "UNSUPPORTED_CAPABILITY",
      field: "operation",
      retryable: false,
      nextAction: "NONE",
      httpStatus: null,
    });
  }
  client.requireCapability("status");
  assert.deepStrictEqual(Object.getOwnPropertyNames(StatusClient.prototype).sort(), ["constructor", "requireCapability", "status", "supports"]);
  for (const name of ["cancel", "refresh", "replace"]) assert.equal(name in client, false);
  assert.ok(Object.isFrozen(client));
  assert.deepStrictEqual(Object.keys(client), [], "no capability is reachable from the instance");
});

test("a timer that fires synchronously is CLIENT_CONFIGURATION set_timer; its handle is released", async () => {
  const fake = fakeTransport(respondWith(200, jsonBytes(queued())));
  let released = 0;
  let signed = 0;
  const client = new StatusClient(options({
    transport: fake.transport,
    signer: async () => (signed++, FAKE_MAC.slice()),
    setTimer: (cb) => (cb(), () => void released++),
  }));
  const error = await rejection(client.status(DIGEST));
  assert.deepStrictEqual(errorShape(error), { code: "CLIENT_CONFIGURATION", field: "set_timer", retryable: false, nextAction: "NONE", httpStatus: null });
  assert.equal(fake.calls.length, 0);
  assert.equal(signed, 0);
  assert.equal(released, 1, "cancel returned after a synchronous fire is still called");
});

test("a timer that throws or returns no cancel function is handled", async () => {
  const fake = fakeTransport(respondWith(200, jsonBytes(queued())));
  const throwing = new StatusClient(options({ transport: fake.transport, setTimer: () => { throw new Error("t"); } }));
  const error = await rejection(throwing.status(DIGEST));
  assert.equal(error.code, "CLIENT_CONFIGURATION");
  assert.equal(error.field, "set_timer");
  assert.equal(fake.calls.length, 0);
  const noCancel = new StatusClient(options({ transport: fake.transport, setTimer: () => undefined }));
  assert.deepStrictEqual(await noCancel.status(DIGEST), queued());
  assert.equal(fake.calls.length, 1);
});

test("deterministic: identical injected capabilities give identical requests (no ambient input)", async () => {
  const a = clientWith(respondWith(200, jsonBytes(queued())));
  const b = clientWith(respondWith(200, jsonBytes(queued())));
  await a.client.status(DIGEST);
  await b.client.status(DIGEST);
  assert.deepStrictEqual(
    { url: a.calls[0].url, headers: a.calls[0].headers },
    { url: b.calls[0].url, headers: b.calls[0].headers },
  );
});

test("the deadline also bounds signing: a hung signer is CLIENT_CONFIGURATION signer with zero transport calls", async () => {
  let releaseSigner;
  const { client, calls, timer } = clientWith(respondWith(200, jsonBytes(queued())), {
    signer: () => new Promise((resolve) => (releaseSigner = resolve)),
  });
  const pending = client.status(DIGEST);
  await tick();
  assert.equal(timer.timers.length, 1, "one timer for the whole status() call");
  timer.fire();
  const error = await rejection(pending);
  assert.deepStrictEqual(errorShape(error), { code: "CLIENT_CONFIGURATION", field: "signer", retryable: false, nextAction: "NONE", httpStatus: null });
  releaseSigner(FAKE_MAC.slice()); // a late MAC must not start an exchange
  await tick();
  await tick();
  assert.equal(calls.length, 0);
});

test("a signer rejecting after the deadline is swallowed (no unhandled rejection, no call)", async () => {
  let failSigner;
  const { client, calls, timer } = clientWith(respondWith(200, jsonBytes(queued())), {
    signer: () => new Promise((_, reject) => (failSigner = reject)),
  });
  const pending = client.status(DIGEST);
  await tick();
  timer.fire();
  assert.equal((await rejection(pending)).field, "signer");
  failSigner(new Error("late signer s3cr3t"));
  await tick();
  assert.equal(calls.length, 0);
});

test("transport getters that throw are TRANSPORT_UNAVAILABLE", async () => {
  const error = await statusError(async () => ({
    get status() {
      throw new Error("getter s3cr3t");
    },
    body: new Uint8Array(0),
  }));
  assert.equal(error.code, "TRANSPORT_UNAVAILABLE");
});

test("base URL: an empty port is accepted like Python urlsplit (port None)", async () => {
  const { client, calls } = clientWith(respondWith(200, jsonBytes(queued())), { baseUrl: "http://h:" });
  await client.status(DIGEST);
  assert.equal(calls[0].url, `http://h:/qsearch/v2/requests/${DIGEST}`);
});
