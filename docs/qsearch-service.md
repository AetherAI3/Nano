# Q-search service (`nano-qsearch-serve`)

## What it is

`nano-qsearch-serve` is the service Predator Drive's `NanoRuntimeQSearchBackend`
calls. It accepts one endpoint, `POST /qsearch/v1/submit`, whose body is a
`predator.qsearch.job.v1` job (a QUBO, its compile and backend settings, limits,
and the classical minimum energy Predator already knows). The service checks
the request signature and replay protection, validates the job and its
`job_digest`, compiles the QUBO through ATLAS `build_nucleus_program`, samples
it on Qiskit Aer in a child process, and replies with a
`predator.qsearch.receipt.v1` sealed with HMAC-SHA256 that Predator's
`QSearchReceiptV1.from_payload` verifies. The wire contract is frozen on the
Predator side: Predator-M `QSearchJobV1.to_payload()` produces the job, and
`QSearchReceiptV1.from_payload` consumes the receipt.

**Receipts are classical simulation.** Every OK or TRUNCATED receipt today
comes from Qiskit Aer running on a CPU, either as an exact statevector or as
a matrix product state (MPS). The receipt says which one: `backend.kind` is
`statevector` or `mps`, and `backend.id` names the simulator, for example
`qiskit-aer 0.17.2 mps`. No receipt claims to come from quantum hardware. The
IBM path described below refuses every job.

### Where the code lives, and why

The service is the top-level package `nano_qsearch/`, next to the `nano/`
package rather than inside it. `nano/` is the deterministic rule engine: its
compiled programs must replay the same way every time, and
`tests/test_determinism_guards.py` forbids clocks, environment reads,
subprocesses, network access and third-party imports anywhere under `nano/`.
A service needs every one of those: it reads the wall clock for request
timestamps, reads its configuration from the environment, spawns engine
children, listens on a socket, and its engine imports Qiskit. Keeping it in a
separate package leaves those guards intact. `nano_qsearch` imports nothing
from `nano/`.

Inside `nano_qsearch/`, everything the parent process runs uses only the
standard library: `canonical`, `job`, `receipt`, `auth`, `store`, `service`,
`runtime_mps` and `runtime_ibm`. Only the child entry point `_engine.py`, with
`compile.py`, imports numpy, qiskit, qiskit-aer and `aether_atlas`.

## Install

```powershell
pip install -e ".[quantum]"          # qiskit==2.3.1, qiskit-aer==0.17.2, numpy>=1.26
$env:NANO_QSEARCH_ATLAS_PATH = "C:\path\to\aether-atlas"   # the directory that contains aether_atlas
```

ATLAS is not on PyPI. The engine child finds it through
`NANO_QSEARCH_ATLAS_PATH`, which is prepended to the child's `PYTHONPATH`. The
base `aether-nano` install still has no dependencies, and the service starts
without the `quantum` extra. In that case every run becomes a NOT_RUN receipt
whose reason names the engine error class.

## Keys

The service uses two different keys, each kept in its own file:

| File | Env var | Used for |
| --- | --- | --- |
| receipt key | `NANO_QSEARCH_KEY_PATH` | HMAC seal on every receipt. Predator holds the same bytes and passes them as `key=` to `NanoRuntimeQSearchBackend`. |
| request key | `NANO_QSEARCH_REQUEST_KEY_PATH` | HMAC on every request (`X-Aether-Qsearch-Auth`). Predator's opener signs with it (see [Predator side](#predator-side)). |

Each file holds at least 16 raw bytes; 32 random bytes is the recommendation.
The service refuses to start if the two files contain the same bytes.

```powershell
python -c "import os; open('receipt.key','wb').write(os.urandom(32))"
python -c "import os; open('request.key','wb').write(os.urandom(32))"
```

## Run

```powershell
nano-qsearch-serve --data-dir C:\qsearch\data --key-path C:\qsearch\receipt.key --request-key-path C:\qsearch\request.key
```

The default bind is `127.0.0.1:8798`. On startup the service prints one line to
stderr, `nano-qsearch-serve: backend=<kind> bind=<host>:<port> recovered=<n>`.
That line never mentions keys.

### Configuration

Command-line flags (`--bind --backend --data-dir --key-path --request-key-path --verbose`)
take precedence over the environment.

| Env var | Default | Meaning |
| --- | --- | --- |
| `NANO_QSEARCH_BIND` | `127.0.0.1:8798` | `host:port` to listen on. |
| `NANO_QSEARCH_BACKEND` | `auto` | `auto`, `mps`, `statevector` or `ibm`. With `auto`, the job's own `backend.kind` decides; if that is also `auto`, jobs with up to 20 variables use statevector and larger ones use MPS. Any other value overrides the job. |
| `NANO_QSEARCH_DATA_DIR` | required | Directory for `qsearch.sqlite3`, which holds jobs, receipts and nonces. |
| `NANO_QSEARCH_KEY_PATH` | required | Receipt-seal key file. |
| `NANO_QSEARCH_REQUEST_KEY_PATH` | required | Request-auth key file. |
| `NANO_QSEARCH_MAX_VARIABLES` | `40` | Service cap on the number of variables (QUBO size). |
| `NANO_QSEARCH_MAX_SHOTS` | `100000` | Service cap on `backend.shots`. |
| `NANO_QSEARCH_MAX_CHI` | `256` | Service cap on `backend.chi_max` (MPS bond dimension). |
| `NANO_QSEARCH_MAX_BODY_BYTES` | `1048576` | Largest request body accepted. |
| `NANO_QSEARCH_ATLAS_PATH` | unset | `aether-atlas` directory, used by the engine child (and by the tests). |
| `NANO_QSEARCH_PYTHON` | the service's own interpreter | Python executable for the engine child. |
| `AETHER_AQRC_HW_GO` | unset | IBM gate; see [IBM](#ibm-hardware). |
| `AETHER_AQRC_AUTHORITY_PATH` | unset | IBM gate; see [IBM](#ibm-hardware). |

### Service caps

The caps protect this host. A job that breaks one is valid, so it does not
get a 400. It gets a **sealed, stored REFUSED receipt** whose `reason` is a
fixed sentence naming the cap, and nothing runs:

| Check | Reason |
| --- | --- |
| variables > `NANO_QSEARCH_MAX_VARIABLES` | `limits: <n> variables exceed the service cap of <cap>` |
| `backend.shots` > `NANO_QSEARCH_MAX_SHOTS` | `limits: backend.shots exceeds the service cap of <cap>` |
| `backend.chi_max` > `NANO_QSEARCH_MAX_CHI` | `limits: backend.chi_max exceeds the service cap of <cap>` |
| `backend.seed` outside `0..4294967295` | `limits: backend.seed outside 0..4294967295` |
| `compile.seed` outside `0..4294967295` | `limits: compile.seed outside 0..4294967295` |

Predator accepts any integer as a seed, but Aer and the Qiskit transpiler take
unsigned 32-bit seeds, so seeds outside that range are refused rather than
silently wrapped. The statevector runtime has its own 20-qubit cap: a larger
job sent to `statevector` explicitly is REFUSED with a `statevector:` reason.
`NANO_QSEARCH_MAX_BODY_BYTES` works differently. An oversized body gets an HTTP
413 with no receipt, because no job has been read at that point.

## Request auth

Every request carries this header:

```text
X-Aether-Qsearch-Auth: v1 ts=<unix seconds> nonce=<16..64 lowercase hex> mac=<64 lowercase hex>
```

`mac` is HMAC-SHA256, under the request key, of these five fields joined by `\n`:

```text
POST
/qsearch/v1/submit
<sha256 hex of the exact body bytes>
<ts>
<nonce>
```

`ts` must be within ±120 s of the server clock, and each nonce is accepted
only once. Nonces are stored in the SQLite file, so replay protection survives
a restart. They are pruned after twice the window. The MAC is checked before
the nonce is recorded, so a forged request cannot use up a nonce. The body
size check runs before any of this, so an oversized body costs no MAC and
consumes no nonce.

## Responses

| Status | Body | When |
| --- | --- | --- |
| 200 | sealed receipt | The job was valid. The receipt `status` is one of `OK`, `TRUNCATED`, `NOT_RUN` or `REFUSED`. |
| 400 | `{"error": "invalid job", "code": ...}` | The body is not JSON, or not a valid job (bad schema, digest or limits). No receipt is issued. |
| 401 | `{"error": "unauthorized", "code": ...}` | `code` is one of `MALFORMED`, `STALE`, `MAC` or `REPLAY`. No receipt is issued and nothing is stored. |
| 404 | `{"error": "not found"}` | Any other path or method. |
| 413 | `{"error": "body too large"}` | The body (or its declared `Content-Length`) is over `NANO_QSEARCH_MAX_BODY_BYTES`. |
| 503 | `{"error": "job not completed"}` | The store could not record the result. The job stays accepted, and the next identical submission, or the next restart, finishes it. |

**REFUSED** means policy: the service will not run this job, because of a
service cap, the statevector cap, or the IBM gates. **NOT_RUN** means the
service tried and failed: an engine error, a timeout, a crash that persisted
through every retry, an engine result that failed the audit, an unexpected
exception (`service: <ExceptionClass>`), or a recovery rule (below). A reason
never carries an exception message, only a class name or a fixed sentence.

`TRUNCATED` is an MPS result whose peak bond dimension reached `chi_max`.
`backend.chi_reached` is that peak. `backend.chi_source` records how it was
measured:

- `peak_log`: the per-step bond-dimension log from an extra Aer run with
  `mps_log_data=True`, so it includes bonds that were truncated mid-circuit.
  This is the normal case.
- `final`: the log was missing or would not parse, so only the final bonds of
  the state were available. Mid-circuit growth may be under-reported.
- `null`: a statevector receipt, which has no bond dimension.

`truncated = chi_reached >= chi_max`. This errs on the safe side: a state
whose rank is exactly `chi_max` is also marked truncated.

Before an OK or TRUNCATED receipt is built, the engine result is audited.
Every sample's energy is recomputed from the job, `best_sample_energy` must be
the minimum of the sample energies and may not undercut the classical floor,
and every field copied into the receipt must be present and well-typed.

## Durability and recovery

Jobs are keyed by `job_digest` in `qsearch.sqlite3` (WAL mode,
`synchronous=FULL`). A row moves from ACCEPTED to DONE exactly once, and a DONE
receipt is never replaced. An identical resubmission returns the stored
receipt without running again. Identical submissions that arrive concurrently
share one run (single-flight).

Before each run the service increments the row's `attempts` counter. On
startup, `recover()` handles every row still marked ACCEPTED, meaning the
process died between accepting a job and completing it:

1. If `attempts >= 2`, both the original run and one recovery re-run have
   already died. The job completes as NOT_RUN with
   `recovery: attempt limit reached` and does not run.
2. Otherwise, if the job would go to the `ibm` runtime, it completes as
   NOT_RUN with `recovery: hardware jobs are never resubmitted` and does not
   run. A second paid hardware submission is never made.
3. Otherwise the job **re-runs once** from its stored body: the same seed and
   the same `job_digest`, so the result matches what the first run would have
   produced. Then it is sealed and completed.

Recovery is idempotent: a second `recover()`, or another restart, finds
nothing pending. The same rules apply when a live submission finds an ACCEPTED
row left behind by a store failure.

## Engine isolation

Every Aer job runs in a fresh child interpreter (`NANO_QSEARCH_PYTHON`, or
the service's own interpreter by default) with `OMP_NUM_THREADS=1`. A crash,
meaning a nonzero exit, no output file, or output that is not a JSON object,
is retried up to 4 times. The engine reports a deterministic error as an
error class only. On Windows, run the service and the engine tests from
PowerShell with the system Python: importing qiskit segfaults under Git Bash on
some hosts, and Aer MPS intermittently access-violates, which the retries
absorb.

## IBM hardware

`NANO_QSEARCH_BACKEND=ibm` (or a job asking for `ibm` while the service is in
`auto` mode) selects `IbmRuntime`. It refuses unless all of these hold:
`AETHER_AQRC_HW_GO=1`, `AETHER_AQRC_AUTHORITY_PATH` names an existing authority
file, the job grants QPU seconds (`limits.max_qpu_s > 0`), and the target is
`fake_fez` or `ibm_fez`. Even when every gate is open, submission is delegated
to ATLAS `aqrc.campaign_runtime`. That runtime today admits only
compiler-validation children, so the delegate still refuses. **In practice,
every IBM job is REFUSED today**, and a recovered IBM job is never resubmitted.

## Predator side

Predator's default `NanoRuntimeQSearchBackend` opener (`_urllib_opener`) sends
only `Content-Type`. It sends **no `X-Aether-Qsearch-Auth` header**, so every
request it makes gets a 401, which Predator turns into a
`NOT_RUN transport: HTTPError` receipt. Construct the backend with an
`opener=` that signs each request. Predator must not import Nano, so the
signer below uses only the standard library and reproduces
`nano_qsearch.auth.sign_request`:

```python
import hashlib, hmac, secrets, time, urllib.parse, urllib.request

def signing_opener(request_key: bytes):
    def opener(url: str, data: bytes, timeout: float) -> bytes:
        ts, nonce = int(time.time()), secrets.token_hex(16)
        msg = "\n".join(("POST", urllib.parse.urlsplit(url).path, hashlib.sha256(data).hexdigest(), str(ts), nonce))
        mac = hmac.new(request_key, msg.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Aether-Qsearch-Auth": f"v1 ts={ts} nonce={nonce} mac={mac}"}
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST", headers=headers), timeout=timeout) as resp:
            return resp.read(1_048_577)
    return opener

backend = NanoRuntimeQSearchBackend(endpoint, key=receipt_key, opener=signing_opener(request_key))
```

`key=` is the **receipt** key (used to verify seals). The opener closes over
the **request** key. The path that gets signed is the path the service sees,
so if a proxy rewrites the path, sign the rewritten path instead.

## Testing

```powershell
$py = "C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe"
$env:NANO_QSEARCH_ATLAS_PATH = "C:\path\to\aether-atlas"
$env:OMP_NUM_THREADS = "1"
& $py -m pytest -q -W ignore::DeprecationWarning                     # everything
& $py -m pytest -q -m "not engine and not integration"               # stdlib only, no qiskit or ATLAS needed
& $py -m pytest tests -q -m engine -W ignore::DeprecationWarning     # engine and compile tests
```

In an environment without qiskit, numpy or `NANO_QSEARCH_ATLAS_PATH`, the
engine and compile tests skip and everything else runs.
