// Offline example: no network, no environment, no files. A fake transport
// plays the q-search service; the Web Crypto signer uses a SYNTHETIC key.
// Run from the package directory (`node examples/offline-status.mjs`) or
// from any project that installed the package.
import { LifecycleClientError, StatusClient } from "@nano/qsearch-lifecycle";
import { createHmacSha256Signer, createRandomNonce } from "@nano/qsearch-lifecycle/webcrypto";

const requestDigest = "sha256:" + "ab".repeat(32);
// Synthetic demo key. A real caller obtains its request key from its own
// secret store and passes the bytes in; the client never looks for one.
const demoKey = new Uint8Array(32).map((_, i) => i);

const envelope = {
  schema: "nano.qsearch.job-state.v1",
  request: { request_id: "demo-1", request_digest: requestDigest, target: "ibm_fez" },
  lineage: { replaces_request_digest: null, replacement_request_digest: null },
  state: "RUNNING",
  provider: { job_id: "demo-job.01", status: "RUNNING" },
  execution: { status: "RUNNING", outcome: "UNKNOWN", terminal: false },
  accounting: { status: "PENDING" },
  usage: { reservation_status: "HELD", held_quantum_seconds: 3600.0, settled_quantum_seconds: null },
  cancellation: { requested: false, requested_at: null, confirmed: false, confirmed_at: null },
  deadlines: { queue_at: 1700003600.0, queue_expired: false, accounting_at: null, accounting_expired: false },
  next_action: "POLL_STATUS",
};

let calls = 0;
const fakeTransport = async (request) => {
  calls += 1;
  if (request.method !== "GET" || !request.url.endsWith(`/qsearch/v2/requests/${requestDigest}`)) {
    return { status: 404, body: new TextEncoder().encode('{"error":"not found","code":"REQUEST_NOT_FOUND"}') };
  }
  if (!/^v1 ts=\d+ nonce=[0-9a-f]{32} mac=[0-9a-f]{64}$/.test(request.headers["X-Aether-Qsearch-Auth"])) {
    return { status: 401, body: new TextEncoder().encode('{"error":"bad auth","code":"UNAUTHORIZED"}') };
  }
  return { status: 200, body: new TextEncoder().encode(JSON.stringify(envelope)) };
};

const client = new StatusClient({
  baseUrl: "http://127.0.0.1:8080",
  transport: fakeTransport,
  signer: createHmacSha256Signer(demoKey),
  clock: () => Date.now() / 1000,
  nonce: createRandomNonce,
});

try {
  const state = await client.status(requestDigest);
  console.log(`state=${state.state} execution=${state.execution.status} next_action=${state.next_action}`);
  console.log(`held_quantum_seconds=${state.usage.held_quantum_seconds} settled_quantum_seconds=${state.usage.settled_quantum_seconds}`);
  console.log(`supports(cancel)=${client.supports("cancel")} transport_calls=${calls}`);
  console.log("offline-status: ok");
} catch (error) {
  if (error instanceof LifecycleClientError) {
    console.error(`${error.message} field=${error.field} retryable=${error.retryable} next_action=${error.nextAction}`);
    process.exitCode = 1;
  } else {
    throw error;
  }
}
