// v2 LifecycleClient: capabilities, POST framing, exact call counts, binding,
// lost acknowledgement / timeout semantics, no leakage. Fake transport only.
import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { test } from "node:test";

import { LifecycleClient, LifecycleClientError, StatusClient } from "../dist/index.js";
import { createHmacSha256Signer, createSha256 } from "../dist/webcrypto.js";
import {
  BASE_URL,
  CLOCK_S,
  DIGEST,
  FAKE_MAC,
  NONCE,
  OTHER_DIGEST,
  bytes,
  clone,
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
const ALL = ["status", "refresh", "cancel", "replace"];
const nodeSha256 = async (data) => new Uint8Array(createHash("sha256").update(data).digest());
const hexOf = (data) => createHash("sha256").update(data).digest("hex");

// A synthetic replacement: text deliberately unsorted and spaced.
const REPLACEMENT_TEXT = '{ "lineage": {"replaces": "x"}, "descriptor": {"target": "ibm_fez", "request_id": "req-new_2", "max_execution_time": 60} }';
const CANONICAL_TEXT = '{"descriptor":{"max_execution_time":60,"request_id":"req-new_2","target":"ibm_fez"},"lineage":{"replaces":"x"}}';
const DESCRIPTOR_TEXT = '{"max_execution_time":60,"request_id":"req-new_2","target":"ibm_fez"}';
const NEW_DIGEST = `sha256:${hexOf(DESCRIPTOR_TEXT)}`;

/** The new request's state after a replace: bound to NEW_DIGEST, lineage names DIGEST. */
function replaced(newDigest = NEW_DIGEST, replaces = DIGEST) {
  const value = queued(newDigest);
  value.request.request_id = "req-new_2";
  value.lineage.replaces_request_digest = replaces;
  return value;
}

function lifecycle(respond, overrides = {}) {
  const fake = fakeTransport(respond);
  const timer = fakeTimer();
  const client = new LifecycleClient(
    options({ transport: fake.transport, setTimer: timer.setTimer, capabilities: ALL, sha256: nodeSha256, ...overrides }),
  );
  return { client, calls: fake.calls, timer };
}

const UNSUPPORTED = { code: "UNSUPPORTED_CAPABILITY", field: "operation", retryable: false, nextAction: "NONE", httpStatus: null };
const configuration = (field) => ({ code: "CLIENT_CONFIGURATION", field, retryable: false, nextAction: "NONE", httpStatus: null });
const LOST_ACK = { code: "TRANSPORT_UNAVAILABLE", field: null, retryable: true, nextAction: "MANUAL_RECONCILIATION", httpStatus: null };

test("refresh and cancel: exactly one POST, exactly three headers, empty body, MAC over the empty-body constant", async () => {
  for (const action of ["refresh", "cancel"]) {
    let message;
    const key = new Uint8Array(32).fill(7);
    const signer = createHmacSha256Signer(key);
    const { client, calls, timer } = lifecycle(respondWith(200, jsonBytes(queued())), {
      signer: async (m) => {
        message = m;
        return signer(m);
      },
    });
    const state = await client[action](DIGEST);
    assert.equal(calls.length, 1, action);
    const [request] = calls;
    assert.equal(request.method, "POST");
    assert.equal(request.url, `${BASE_URL}/qsearch/v2/requests/${DIGEST}/${action}`);
    assert.deepStrictEqual(Object.keys(request.headers), ["X-Aether-Qsearch-Auth", "Accept", "Content-Type"]);
    assert.equal(request.headers.Accept, "application/json");
    assert.equal(request.headers["Content-Type"], "application/json");
    assert.ok(request.body instanceof Uint8Array);
    assert.equal(request.body.length, 0);
    const ts = Math.floor(CLOCK_S);
    const path = `/qsearch/v2/requests/${DIGEST}/${action}`;
    const expected = ["POST", path, EMPTY_SHA256, String(ts), NONCE].join(String.fromCharCode(10));
    assert.equal(Buffer.from(message).toString("utf8"), expected);
    const mac = createHmac("sha256", Buffer.from(key)).update(expected).digest("hex");
    assert.equal(request.headers["X-Aether-Qsearch-Auth"], `v1 ts=${ts} nonce=${NONCE} mac=${mac}`);
    assert.deepStrictEqual(Object.keys(request).sort(), ["body", "headers", "maxResponseBytes", "method", "signal", "timeoutMs", "url"]);
    assert.ok(Object.isFrozen(request) && Object.isFrozen(request.headers));
    assert.deepStrictEqual(state, queued());
    assert.ok(Object.isFrozen(state));
    assert.equal(timer.timers[0].cancelled, true);
  }
});

test("replace: canonical body bytes, MAC over their SHA-256, response bound to the descriptor digest and lineage", async () => {
  let message;
  const hashed = [];
  const { client, calls } = lifecycle(respondWith(200, jsonBytes(replaced())), {
    signer: async (m) => {
      message = m;
      return FAKE_MAC.slice();
    },
    sha256: async (data) => {
      hashed.push(Buffer.from(data).toString("utf8"));
      return nodeSha256(data);
    },
  });
  const state = await client.replace(DIGEST, REPLACEMENT_TEXT);
  assert.equal(calls.length, 1);
  const [request] = calls;
  assert.equal(request.method, "POST");
  assert.equal(request.url, `${BASE_URL}/qsearch/v2/requests/${DIGEST}/replace`);
  assert.equal(Buffer.from(request.body).toString("utf8"), CANONICAL_TEXT);
  assert.deepStrictEqual(hashed, [CANONICAL_TEXT, DESCRIPTOR_TEXT]);
  const path = `/qsearch/v2/requests/${DIGEST}/replace`;
  const framing = ["POST", path, hexOf(CANONICAL_TEXT), String(Math.floor(CLOCK_S)), NONCE].join(String.fromCharCode(10));
  assert.equal(Buffer.from(message).toString("utf8"), framing);
  assert.deepStrictEqual(Object.keys(request.headers), ["X-Aether-Qsearch-Auth", "Accept", "Content-Type"]);
  assert.deepStrictEqual(state, replaced());
  // Bytes input gives the identical request body.
  const second = lifecycle(respondWith(200, jsonBytes(replaced())));
  await second.client.replace(DIGEST, bytes(REPLACEMENT_TEXT));
  assert.equal(Buffer.from(second.calls[0].body).toString("utf8"), CANONICAL_TEXT);
});

test("the request body is a private copy: mutating it in the transport changes nothing signed or retried", async () => {
  const { client, calls } = lifecycle((request) => {
    request.body.fill(0x20);
    return Promise.resolve({ status: 200, body: jsonBytes(replaced()) });
  });
  await client.replace(DIGEST, REPLACEMENT_TEXT);
  assert.equal(calls.length, 1);
});

test("capabilities: default is status only; not granted = UNSUPPORTED_CAPABILITY with zero transport calls", async () => {
  const fake = fakeTransport(respondWith(200, jsonBytes(queued())));
  const client = new LifecycleClient(options({ transport: fake.transport }));
  assert.equal(client.supports("status"), true);
  for (const op of ["refresh", "cancel", "replace", "submit", "native:ibm.cancel_job", "STATUS", "", "__proto__", "constructor"]) {
    assert.equal(client.supports(op), false, op);
    assert.deepStrictEqual(errorShape(thrown(() => client.requireCapability(op))), UNSUPPORTED, op);
  }
  for (const call of [() => client.refresh(DIGEST), () => client.cancel(DIGEST), () => client.replace(DIGEST, REPLACEMENT_TEXT)]) {
    assert.deepStrictEqual(errorShape(await rejection(call())), UNSUPPORTED);
  }
  // Refusal comes before digest or replacement checks.
  assert.deepStrictEqual(errorShape(await rejection(client.cancel("not-a-digest"))), UNSUPPORTED);
  assert.deepStrictEqual(errorShape(await rejection(client.replace("x", "{"))), UNSUPPORTED);
  assert.equal(fake.calls.length, 0);
  await client.status(DIGEST);
  assert.equal(fake.calls.length, 1);

  const cancelOnly = new LifecycleClient(options({ transport: fake.transport, capabilities: ["cancel"] }));
  assert.deepStrictEqual(errorShape(await rejection(cancelOnly.status(DIGEST))), UNSUPPORTED);
  assert.deepStrictEqual(errorShape(await rejection(cancelOnly.refresh(DIGEST))), UNSUPPORTED);
  assert.equal(fake.calls.length, 1);
  await cancelOnly.cancel(DIGEST);
  assert.equal(fake.calls.length, 2);

  const none = new LifecycleClient(options({ transport: fake.transport, capabilities: [] }));
  for (const op of ALL) assert.equal(none.supports(op), false);
  assert.deepStrictEqual(errorShape(await rejection(none.status(DIGEST))), UNSUPPORTED);
  assert.equal(fake.calls.length, 2);
});

test("StatusClient still refuses every control and has no control method", () => {
  const client = new StatusClient(options());
  for (const op of ["refresh", "cancel", "replace"]) {
    assert.equal(client.supports(op), false);
    assert.deepStrictEqual(errorShape(thrown(() => client.requireCapability(op))), UNSUPPORTED);
    assert.equal(op in client, false);
  }
});

test("construction: capabilities and sha256 are validated", () => {
  for (const bad of ["status", ["status", "submit"], ["STATUS"], [1], [null], { 0: "status", length: 1 }, null, new Set(["status"])]) {
    assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(options({ capabilities: bad })))), configuration("capabilities"), String(bad));
  }
  const hostile = new Proxy(["status"], {
    get(target, key) {
      if (key === "0") throw new Error("boom");
      return Reflect.get(target, key);
    },
  });
  assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(options({ capabilities: hostile })))), configuration("capabilities"));
  assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(options({ capabilities: ["replace"] })))), configuration("sha256"));
  for (const bad of [null, "sha256", 42, {}]) {
    assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(options({ capabilities: ["replace"], sha256: bad })))), configuration("sha256"));
    assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(options({ sha256: bad })))), configuration("sha256"));
  }
  // Duplicates are harmless; the caller's array is snapshotted.
  const caps = ["cancel", "cancel"];
  const client = new LifecycleClient(options({ capabilities: caps }));
  caps.push("refresh");
  assert.equal(client.supports("refresh"), false);
  assert.equal(client.supports("cancel"), true);
  // Base checks are shared with StatusClient.
  assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(options({ baseUrl: "https://h.test/path" })))), configuration("base_url"));
  assert.deepStrictEqual(errorShape(thrown(() => new LifecycleClient(null))), configuration(null));
  assert.ok(Object.isFrozen(client));
  assert.deepStrictEqual(Object.keys(client), []);
  assert.deepStrictEqual(
    Object.getOwnPropertyNames(LifecycleClient.prototype).sort(),
    ["cancel", "constructor", "refresh", "replace", "requireCapability", "status", "supports"],
  );
});

test("a bad sha256 result at call time is CLIENT_CONFIGURATION sha256 with zero transport calls", async () => {
  for (const sha256 of [
    async () => new Uint8Array(31),
    async () => new Uint8Array(33),
    async () => "00".repeat(32),
    async () => null,
    async () => {
      throw new Error("secret-hash-failure");
    },
    () => {
      throw new Error("sync");
    },
    async () => ({ length: 32 }),
  ]) {
    const { client, calls } = lifecycle(respondWith(200, jsonBytes(replaced())), { sha256 });
    const error = await rejection(client.replace(DIGEST, REPLACEMENT_TEXT));
    assert.deepStrictEqual(errorShape(error), configuration("sha256"));
    assert.equal(calls.length, 0);
    assert.ok(!error.message.includes("secret"));
  }
});

test("a hung sha256 is bounded by the deadline: CLIENT_CONFIGURATION sha256, zero calls, late result ignored", async () => {
  let release;
  const { client, calls, timer } = lifecycle(respondWith(200, jsonBytes(replaced())), {
    sha256: () =>
      new Promise((resolve) => {
        release = resolve;
      }),
  });
  const pending = client.replace(DIGEST, REPLACEMENT_TEXT);
  await tick();
  timer.fire();
  assert.deepStrictEqual(errorShape(await rejection(pending)), configuration("sha256"));
  release(new Uint8Array(32));
  await tick();
  await tick();
  assert.equal(calls.length, 0);
});

test("review M1: the sha256 capability is never invoked after the deadline", async () => {
  const releases = [];
  let invocations = 0;
  const { client, calls, timer } = lifecycle(respondWith(200, jsonBytes(replaced())), {
    sha256: () => {
      invocations += 1;
      return new Promise((resolve) => releases.push(resolve));
    },
  });
  const pending = client.replace(DIGEST, REPLACEMENT_TEXT);
  await tick();
  timer.fire();
  assert.deepStrictEqual(errorShape(await rejection(pending)), configuration("sha256"));
  const atDeadline = invocations;
  for (const release of releases.splice(0)) release(new Uint8Array(32));
  for (let i = 0; i < 4; i += 1) await tick();
  for (const release of releases.splice(0)) release(new Uint8Array(32));
  await tick();
  assert.equal(invocations, atDeadline, "sha256 called again after the deadline had already settled the call");
  assert.equal(calls.length, 0);
});

test("replacement that cannot be canonicalised: INVALID_REQUEST field replacement, zero transport calls", async () => {
  const BS = String.fromCharCode(92);
  const INVALID_REQUEST = { code: "INVALID_REQUEST", field: "replacement", retryable: false, nextAction: "NONE", httpStatus: null };
  for (const bad of [
    "",
    "{",
    "[]",
    '{"descriptor":[]}',
    '{"descriptor":null}',
    '{"other":{}}',
    '{"descriptor":{"a":1,"a":2}}',
    '{"descriptor":{"a":NaN}}',
    '{"descriptor":{"a":1e400}}',
    `{"descriptor":{"a":"${BS}ud800"}}`,
    `{"descriptor":${"[".repeat(64)}${"]".repeat(64)}}`,
    { descriptor: {} },
    null,
    undefined,
    new Uint8Array([0xef, 0xbb, 0xbf, 0x7b, 0x7d]),
  ]) {
    const { client, calls } = lifecycle(respondWith(200, jsonBytes(replaced())));
    assert.deepStrictEqual(errorShape(await rejection(client.replace(DIGEST, bad))), INVALID_REQUEST, String(bad));
    assert.equal(calls.length, 0);
  }
});

test("malformed digests are IDENTITY with zero transport calls for every operation", async () => {
  const { client, calls } = lifecycle(respondWith(200, jsonBytes(queued())));
  for (const bad of ["", "sha256:" + "AB".repeat(32), DIGEST + "/x", DIGEST.slice(0, -1), null]) {
    for (const call of [() => client.status(bad), () => client.refresh(bad), () => client.cancel(bad), () => client.replace(bad, REPLACEMENT_TEXT)]) {
      const error = await rejection(call());
      assert.equal(error.code, "IDENTITY");
      assert.equal(error.field, "request_digest");
    }
  }
  assert.equal(calls.length, 0);
});

test("lost acknowledgement on a control: TRANSPORT_UNAVAILABLE + MANUAL_RECONCILIATION, never a state, never a follow-up", async () => {
  for (const respond of [
    () => {
      throw new Error("connection reset after send");
    },
    () => Promise.reject(new Error("socket hang up")),
    async () => null,
    async () => "200",
  ]) {
    for (const run of [(c) => c.refresh(DIGEST), (c) => c.cancel(DIGEST), (c) => c.replace(DIGEST, REPLACEMENT_TEXT)]) {
      const { client, calls } = lifecycle(respond);
      const error = await rejection(run(client));
      assert.deepStrictEqual(errorShape(error), LOST_ACK);
      await tick();
      await tick();
      assert.equal(calls.length, 1, "no retry, no status read");
    }
  }
  // GET keeps POLL_STATUS.
  const { client } = lifecycle(() => Promise.reject(new Error("down")));
  const error = await rejection(client.status(DIGEST));
  assert.equal(error.nextAction, "POLL_STATUS");
  assert.equal(error.retryable, true);
});

test("timeout on a control: TRANSPORT_UNAVAILABLE + MANUAL_RECONCILIATION, signal aborted, late success ignored", async () => {
  for (const run of [(c) => c.refresh(DIGEST), (c) => c.cancel(DIGEST), (c) => c.replace(DIGEST, REPLACEMENT_TEXT)]) {
    let deliver;
    const { client, calls, timer } = lifecycle(
      () =>
        new Promise((resolve) => {
          deliver = resolve;
        }),
    );
    const pending = run(client);
    for (let i = 0; i < 5 && calls.length === 0; i += 1) await tick();
    assert.equal(calls.length, 1);
    timer.fire();
    assert.deepStrictEqual(errorShape(await rejection(pending)), LOST_ACK);
    assert.equal(calls[0].signal.aborted, true);
    deliver({ status: 200, body: jsonBytes(queued()) });
    await tick();
    assert.equal(calls.length, 1);
  }
});

test("cancel returns the server's envelope as is: CANCELLATION_REQUESTED is reported, not upgraded or followed", async () => {
  const requested = queued();
  requested.state = "CANCELLATION_REQUESTED";
  requested.cancellation = { requested: true, requested_at: 1_700_000_100.0, confirmed: false, confirmed_at: null };
  requested.next_action = "POLL_STATUS";
  const running = clone(devSet().running);
  for (const body of [requested, running, devSet().cancelled, queued()]) {
    const { client, calls } = lifecycle(respondWith(200, jsonBytes(body)));
    const state = await client.cancel(DIGEST);
    assert.deepStrictEqual(state, body);
    await tick();
    await tick();
    assert.equal(calls.length, 1);
  }
});

test("binding negatives: refresh/cancel digest, replace descriptor digest and lineage", async () => {
  const INVALID = { code: "INVALID_RESPONSE", field: null, retryable: false, nextAction: "MANUAL_RECONCILIATION", httpStatus: 200 };
  for (const action of ["refresh", "cancel"]) {
    const { client, calls } = lifecycle(respondWith(200, jsonBytes(queued(OTHER_DIGEST))));
    assert.deepStrictEqual(errorShape(await rejection(client[action](DIGEST))), INVALID, action);
    assert.equal(calls.length, 1);
  }
  const cases = [
    ["response is the original request", queued(DIGEST)],
    ["response digest is another request", replaced(OTHER_DIGEST)],
    ["lineage does not name the original", replaced(NEW_DIGEST, OTHER_DIGEST)],
    ["lineage is null", replaced(NEW_DIGEST, null)],
    ["the original digest with lineage to itself", replaced(DIGEST, DIGEST)],
  ];
  for (const [label, body] of cases) {
    const { client, calls } = lifecycle(respondWith(200, jsonBytes(body)));
    assert.deepStrictEqual(errorShape(await rejection(client.replace(DIGEST, REPLACEMENT_TEXT))), INVALID, label);
    assert.equal(calls.length, 1, label);
  }
  // Full v1 validation applies to every 2xx (an invariant violation is refused).
  const broken = replaced();
  broken.execution.terminal = true;
  const { client } = lifecycle(respondWith(200, jsonBytes(broken)));
  assert.equal((await rejection(client.replace(DIGEST, REPLACEMENT_TEXT))).code, "INVALID_RESPONSE");
});

test("remote errors on controls are mapped like v1, without a follow-up call", async () => {
  const { client, calls } = lifecycle(
    respondWith(409, jsonBytes({ code: "NEW_IDENTITY_REQUIRED", field: "descriptor.request_id", retryable: false, next_action: "NONE", error: "secret text" })),
  );
  const error = await rejection(client.replace(DIGEST, REPLACEMENT_TEXT));
  assert.deepStrictEqual(errorShape(error), {
    code: "NEW_IDENTITY_REQUIRED",
    field: "descriptor.request_id",
    retryable: false,
    nextAction: "NONE",
    httpStatus: 409,
  });
  assert.ok(!JSON.stringify(error).includes("secret"));
  const redirect = lifecycle(respondWith(307, new Uint8Array(0)));
  assert.equal((await rejection(redirect.client.cancel(DIGEST))).code, "UNEXPECTED_REDIRECT");
  await tick();
  assert.equal(calls.length + redirect.calls.length, 2);
});

test("errors never leak URL, key, header, nonce, replacement content or exception text", async () => {
  const secretKey = new Uint8Array(32).fill(0x5e);
  const secretHex = Buffer.from(secretKey).toString("hex");
  const secretNonce = "feedfacecafebeef0011223344556677";
  const secretBase = "https://secret-host.internal.test:9443";
  const secretReplacement = '{"descriptor":{"request_id":"s3cr3t-request"}, "x": [}';
  const secrets = [secretHex, secretNonce, "secret-host", "s3cr3t", "X-Aether", "mac=", "ts=", DIGEST, "request_id"];
  const runs = [
    (c) => c.cancel(DIGEST),
    (c) => c.refresh(DIGEST),
    (c) => c.replace(DIGEST, secretReplacement),
    (c) => c.replace(DIGEST, '{"descriptor":{"request_id":"s3cr3t-ok"}}'),
  ];
  for (const respond of [
    () => {
      throw new Error("s3cr3t-exception at https://secret-host.internal.test");
    },
    async () => ({ status: 200, body: bytes('{"s3cr3t": 1') }),
    async () => ({ status: 500, body: jsonBytes({ code: "s3cr3t", error: "s3cr3t body" }) }),
  ]) {
    for (const run of runs) {
      const { client } = lifecycle(respond, {
        baseUrl: secretBase,
        nonce: () => secretNonce,
        signer: createHmacSha256Signer(secretKey),
        sha256: createSha256(),
      });
      const error = await rejection(run(client));
      assert.ok(error instanceof LifecycleClientError);
      assert.equal("cause" in error, false);
      const own = Object.getOwnPropertyNames(error).map((k) => String(error[k]));
      const views = [String(error), JSON.stringify(error), error.message, String(error.stack), ...own];
      for (const view of views) for (const secret of secrets) assert.ok(!view.includes(secret), `${secret} leaked in ${view}`);
      assert.ok(Object.isFrozen(error));
    }
  }
});

test("N controls = N transport calls with fresh nonces; nothing else is ever sent", async () => {
  let n = 0;
  const { client, calls } = lifecycle((request) => {
    const body = request.url.endsWith("/replace") ? replaced() : queued();
    return Promise.resolve({ status: 200, body: jsonBytes(body) });
  }, { nonce: () => (n++).toString(16).padStart(32, "0") });
  await client.status(DIGEST);
  await client.refresh(DIGEST);
  await client.cancel(DIGEST);
  await client.replace(DIGEST, REPLACEMENT_TEXT);
  await client.cancel(DIGEST);
  await tick();
  assert.deepStrictEqual(
    calls.map((c) => `${c.method} ${c.url.slice(BASE_URL.length)}`),
    [
      `GET /qsearch/v2/requests/${DIGEST}`,
      `POST /qsearch/v2/requests/${DIGEST}/refresh`,
      `POST /qsearch/v2/requests/${DIGEST}/cancel`,
      `POST /qsearch/v2/requests/${DIGEST}/replace`,
      `POST /qsearch/v2/requests/${DIGEST}/cancel`,
    ],
  );
  assert.equal(new Set(calls.map((c) => c.headers["X-Aether-Qsearch-Auth"])).size, 5);
  // The v2 GET carries an empty body member and only the two GET headers.
  assert.equal(calls[0].body.length, 0);
  assert.deepStrictEqual(Object.keys(calls[0].headers), ["X-Aether-Qsearch-Auth", "Accept"]);
});

test("createSha256 (Web Crypto) agrees with node:crypto", async () => {
  const sha256 = createSha256();
  for (const text of ["", "abc", CANONICAL_TEXT]) {
    const digest = await sha256(bytes(text));
    assert.ok(digest instanceof Uint8Array);
    assert.equal(Buffer.from(digest).toString("hex"), hexOf(text));
  }
  assert.equal(Buffer.from(await sha256(bytes("abc"))).toString("hex"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  await assert.rejects(sha256("abc"), (error) => error instanceof LifecycleClientError && error.field === "sha256");
});
