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
`compile.py`, imports numpy, qiskit, qiskit-aer and `aether_atlas`. One exception:
once every IBM gate is open, `runtime_ibm` compiles in the service process
(today the delegate refuses before that matters).

Samples come from the logical ATLAS program. The native Heron-basis program is
compiled for its gate counts and `compiled_plan_digest` only; for noiseless
statevector and MPS simulation the two give the same distribution.

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

Jobs whose `compile.target` is `fake_fez` or `ibm_fez` also need
`qiskit-ibm-runtime` (for `FakeFez`), which the `quantum` extra does not
install. Without it those jobs are stored as NOT_RUN `engine: TargetUnavailable`.
Predator's default target, `heron_heavy_hex`, needs nothing extra.

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

### Startup order and a single instance per data directory

Startup runs in this order:

1. Read the configuration.
2. Take an exclusive OS lock on `<data_dir>/qsearch.lock` (`msvcrt.locking`
   on Windows, `flock` elsewhere). The lock is held until the service exits.
3. Bind the port. From here on, connections are accepted into the listen
   backlog but not answered yet. The backlog is the server's
   `request_queue_size`, which the service raises from the standard library's
   5 to 64; further connections beyond that may be refused or reset by the OS.
   On Windows the port is claimed with `SO_EXCLUSIVEADDRUSE`, so a second
   process cannot bind a port the service is listening on.
4. Recover pending jobs (see [Durability and recovery](#durability-and-recovery)).
5. Print the startup line and start answering requests.

Only one service may use a data directory at a time. A second
`nano-qsearch-serve` pointed at the same directory fails at step 2: it prints
`nano-qsearch-serve: data directory is already in use by another nano-qsearch-serve`
and exits with code 2, before it binds the port. The lock file is empty and
stays on disk. It does not have to be deleted after a crash, because the OS
releases the lock when the process dies. Only lock contention is reported
this way; any other error taking the lock is a startup failure (below).

Any other startup failure, such as a port that is already in use, an
unreadable data directory or a SQLite error opening the store, prints one line,
`nano-qsearch-serve: startup failed: <ExceptionClass>`, and exits with code 2.
The line names only the exception class, because OS and SQLite messages can
carry paths. A configuration error (a missing required variable, an unreadable
key file, or a cap or timeout that is not a number) also exits with code 2, and
its line names the variable at fault.

Recovery happens after binding, so callers do not get "connection refused"
while the service starts. It also **blocks serving until every pending job
has finished**. Each re-run can take up to its wall-clock budget, so a caller
whose connection is waiting in the backlog may time out first. A caller that
times out can resubmit the same job: once recovery has completed it, the
service returns the stored receipt.

### Configuration

Command-line flags (`--bind --backend --data-dir --key-path --request-key-path --verbose`)
take precedence over the environment.

| Env var | Default | Meaning |
| --- | --- | --- |
| `NANO_QSEARCH_BIND` | `127.0.0.1:8798` | `host:port` to listen on. |
| `NANO_QSEARCH_BACKEND` | `auto` | `auto`, `mps`, `statevector` or `ibm`. With `auto`, the job's own `backend.kind` decides; if that is also `auto`, jobs with up to 20 variables use statevector and larger ones use MPS. Any other value overrides the job. |
| `NANO_QSEARCH_DATA_DIR` | required | Directory for `qsearch.sqlite3`, which holds jobs, receipts and nonces, and for the `qsearch.lock` single-instance lock. |
| `NANO_QSEARCH_KEY_PATH` | required | Receipt-seal key file. |
| `NANO_QSEARCH_REQUEST_KEY_PATH` | required | Request-auth key file. |
| `NANO_QSEARCH_MAX_VARIABLES` | `40` | Service cap on the number of variables (QUBO size). |
| `NANO_QSEARCH_MAX_SHOTS` | `100000` | Service cap on `backend.shots`. |
| `NANO_QSEARCH_MAX_CHI` | `256` | Service cap on `backend.chi_max` (MPS bond dimension). |
| `NANO_QSEARCH_MAX_BODY_BYTES` | `1048576` | Largest request body accepted. |
| `NANO_QSEARCH_MAX_CONCURRENT` | `2` | Most runtime calls (engine runs) in progress at once. Further jobs wait for a free slot. Identical jobs share one run whatever this is set to. |
| `NANO_QSEARCH_REQUEST_TIMEOUT_S` | `30` | Socket idle timeout, in seconds. It applies to each read and each write on the connection separately, not to the request as a whole: a client that sends nothing for this long mid-request is disconnected, but one that keeps trickling bytes is not. It does not limit how long a job runs. |
| `NANO_QSEARCH_ATLAS_PATH` | unset | `aether-atlas` directory, used by the engine child (and by the tests). |
| `NANO_QSEARCH_PYTHON` | the service's own interpreter | Python executable for the engine child. |
| `NANO_QSEARCH_PURPOSE` | `qsearch` | Child purpose recorded on IBM receipts, `qsearch` or `qcomms`; see [IBM](#ibm-hardware). |
| `AETHER_AQRC_HW_GO` | unset | IBM gate for `ibm_fez`; see [IBM](#ibm-hardware). |
| `AETHER_AQRC_AUTHORITY_KEY_PATH` | unset | Host authority key for `ibm_fez`; see [IBM](#ibm-hardware). |
| `AETHER_AQRC_LEDGER_PATH` | unset | Campaign ledger (sqlite) for `ibm_fez`; see [IBM](#ibm-hardware). |
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
| 400 | `{"error": "invalid job", "code": ...}` | The body is not JSON (including `NaN`/`Infinity` and invalid UTF-8), or not a valid job (bad schema, digest or limits). No receipt is issued. |
| 400 | `{"error": "bad content length"}` | `Content-Length` is negative or not a number. |
| 401 | `{"error": "unauthorized"}` | Authentication failed, for any reason. The body is the same for a missing, malformed, stale, forged or replayed header, so an unauthenticated caller cannot use it to probe the clock window. With `--verbose`, the service writes the reason (`MALFORMED`, `STALE`, `MAC` or `REPLAY`) to its own stderr. No receipt is issued and no job is stored. |
| 404 | `{"error": "not found"}` | Any method other than POST, or any path other than exactly `/qsearch/v1/submit`. A trailing slash or a query string counts as a different path. A HEAD request gets the 404 with no body. |
| 413 | `{"error": "body too large"}` | The body (or its declared `Content-Length`) is over `NANO_QSEARCH_MAX_BODY_BYTES`. |
| 503 | `{"error": "job not completed"}` | The job could not be completed: the store failed to record the result, or the nonce store failed during authentication. After a result-store failure the job stays accepted, and the next identical submission, or the next restart, finishes it under the recovery rules below. |

**REFUSED** means policy: the service will not run this job, because of a
service cap, the statevector cap, or the IBM gates. **NOT_RUN** means the
service tried and failed: an engine error, a timeout, a crash that persisted
through every retry, an engine result that failed the audit, an unexpected
exception (`service: <ExceptionClass>`), or a recovery rule (below). A reason
never carries an exception message, only a class name or a fixed sentence.

`TRUNCATED` is an MPS result that may have cut a bond. `backend.chi_reached`
is the peak bond dimension, and `backend.chi_source` records how it was
measured, which also decides `truncated`. An MPS receipt has one of three
values:

- `peak_log`: the per-step bond-dimension log from an extra Aer run with
  `mps_log_data=True`, so it includes bonds that were truncated mid-circuit.
  This is the normal case. `truncated = chi_reached >= chi_max`, which errs on
  the safe side: a state whose rank is exactly `chi_max` is also marked
  truncated.
- `final`: the log was missing or would not parse, so only the final bonds of
  the state were available, and those can under-report a mid-circuit cut.
  `chi_reached` is the largest final bond, and `truncated` is always `true`
  when the program has a two-qubit gate.
- `no_2q`: the program has no two-qubit gate, so every bond stays at 1.
  `chi_reached = 1` and `truncated = false`.

A statevector receipt has `chi_source`, `chi_reached` and `truncated` all
`null`, because it has no bond dimension.

Before an OK or TRUNCATED receipt is built, the engine result is audited.
Every sample's energy is recomputed from the job, `best_sample_energy` must be
the minimum of the sample energies and may not undercut the classical floor,
and every field copied into the receipt must be present and well-typed.

## Durability and recovery

Jobs are keyed by `job_digest` in `qsearch.sqlite3` (WAL mode,
`synchronous=FULL`). A row moves from ACCEPTED to DONE exactly once, and a DONE
receipt is never replaced. An identical resubmission returns the stored
receipt without running again. Identical submissions that arrive concurrently
share one run (single-flight). If storing a result finds the row already DONE,
the service returns the receipt that was stored first.

**Every sealed receipt is permanent for its `job_digest`, including NOT_RUN
and REFUSED.** Resubmitting the same job returns the same NOT_RUN or REFUSED
receipt and never retries it, even after the cause has been fixed (for
example, a wrong `NANO_QSEARCH_ATLAS_PATH`, a missing `quantum` extra, or a
cap that has since been raised). To run the job again, the operator changes
the job so that it gets a new digest (for example, a different
`backend.seed`), or stops the service and deletes that row from the `jobs`
table in `qsearch.sqlite3`.

When the service accepts a job, it records on the row the runtime kind it
selected (`statevector`, `mps` or `ibm`). Every later run of that job uses the
recorded kind, even if `NANO_QSEARCH_BACKEND` has changed since then. Rows
written before this column existed have no recorded kind and use the current
backend. The column is added to an existing `qsearch.sqlite3` automatically on
startup.

The service increments the row's `attempts` counter when a run actually
starts: after the job has acquired a concurrency slot
(`NANO_QSEARCH_MAX_CONCURRENT`), immediately before the runtime is called. A
job that was still waiting for a slot when the process died has not used an
attempt, and a REFUSED job never records one. If recording the attempt fails,
nothing runs and the request gets a 503. On startup, `recover()` handles every
row still marked ACCEPTED, meaning the process died between accepting a job
and completing it:

1. If `attempts >= 2`, both the original run and one recovery re-run have
   already died. The job completes as NOT_RUN with
   `recovery: attempt limit reached` and does not run.
2. Otherwise, if the job's recorded kind is `ibm`, it completes as
   NOT_RUN with `recovery: hardware jobs are never run from recovery` and does not
   run. A second paid hardware submission is never made.
3. Otherwise the job **re-runs once** from its stored body: the same seed, the
   same `job_digest` and the same runtime kind, so the result matches what the
   first run would have produced. Then it is sealed and completed. A row whose
   run never started (`attempts = 0`, for example one still queued for a slot)
   also runs once, and that run counts as its first attempt.

Recovery is idempotent: a second `recover()`, or another restart, finds
nothing pending. The same rules apply when a live submission finds an ACCEPTED
row left behind by a store failure. Recovery runs before the service answers
any request (see [Startup order](#startup-order-and-a-single-instance-per-data-directory)).

## Engine isolation

Every Aer job runs in a fresh child interpreter (`NANO_QSEARCH_PYTHON`, or
the service's own interpreter by default) with `OMP_NUM_THREADS=1`. A crash,
meaning a nonzero exit, no output file, or output that is not a JSON object,
is retried up to 4 times. All runs of one job share a single wall-clock
budget (`limits.max_wall_s`, at most 3600 s; the key is required, and `0`
means the 600 s default). Each run is
given only what is left of it, and no retry starts with less than 1 s left:
once it is spent, the job is NOT_RUN with `engine: TimeoutExpired`, however
many retries remain. The engine reports a deterministic error, including a failed import
such as a wrong `NANO_QSEARCH_ATLAS_PATH`, as an error class only, and it is
not retried.

The child's environment is the service's minus every variable whose name
starts with `NANO_QSEARCH_KEY`, `NANO_QSEARCH_REQUEST_KEY`, `AETHER_AQRC_`,
`QISKIT_IBM`, `IBM_`, `IBMQ`, `QE_TOKEN` or `PREDATOR_QSEARCH_`, so the service's
key paths and the usual IBM credential variables do not reach it. This is a
denylist and defence in depth; the real boundary is the OS user the service runs as. The
child has no pipes to the service: its stdout goes to the null device and its
stderr to a file in the job's temporary directory. On a nonzero exit the
service writes one line to its own stderr with the exit code and the last 400
bytes of that file. That text never enters a receipt. On Windows, run the service and the engine tests from
PowerShell with the system Python: importing qiskit segfaults under Git Bash on
some hosts, and Aer MPS intermittently access-violates, which the retries
absorb.

## IBM hardware

`NANO_QSEARCH_BACKEND=ibm` (or a job asking for `ibm` while the service is in
`auto` mode) selects `IbmRuntime`. The target decides the gate:

- **`fake_fez` is a dry run.** Nothing is spent, so it needs no GO, no authority
  file and no QPU seconds. The job is compiled by `nano_qsearch.compile` and the
  ISA circuit runs through ATLAS `campaign_runtime.run_qsearch_dry`
  (`qiskit_ibm_runtime.SamplerV2(mode=FakeFez())`, local testing mode on Aer),
  the same PUB-in / ordered-samples-out path `ibm_fez` would take. The receipt
  has `backend.kind = "ibm"`, a `hardware` block (`backend_name`, `target`,
  `purpose`, `job_id`, `pub_index`, `register`, `bit_order: "msb_first"`,
  `shots`, the shot-ordered `raw_samples`, `dry_run: true`) and the
  **hardware seal** `hmac-sha256+hardware:<hex>`, an HMAC over
  `b"predator.qsearch.hardware-receipt.v1\n"` + the canonical body. Predator's
  `from_payload` accepts that seal only on a body that claims hardware, and a
  plain seal never on one that does.
- **`ibm_fez` keeps every gate:** `AETHER_AQRC_HW_GO=1`, an existing authority
  file at `AETHER_AQRC_AUTHORITY_PATH`, and `limits.max_qpu_s > 0`. The delegate
  then loads that file as the operator's plan-scoped authority token, the host
  authority key from `AETHER_AQRC_AUTHORITY_KEY_PATH` and the campaign ledger
  from `AETHER_AQRC_LEDGER_PATH`, and calls ATLAS
  `campaign_runtime.submit_qsearch_child`, which re-checks GO, verifies the HMAC
  authority against the ledger and the 600 s cap, and then stops: the frozen
  campaign protocol has no reservable q-search stage (lane L4 hand-off in
  Predator-M `docs/superpowers/plans/2026-10-25-l4-hardware-select.md`). Every
  outcome on this path is `REFUSED ibm: RuntimeError` (gate or BLOCKED) or
  `REFUSED ibm: ValueError` (authority does not verify). **No real QPU job can be
  submitted from the service**, and a recovered IBM job is never resubmitted.
  An `ibm` receipt is sealed with the hardware seal by its `backend.kind` alone;
  one without its `hardware` block is never sealed at all.

The child purpose, `qsearch` (default) or `qcomms`, is a service setting
(`NANO_QSEARCH_PURPOSE`, or `IbmRuntime(purpose=...)`), not a job field: the
job schema is frozen. It is passed to the ATLAS child and recorded in
`hardware.purpose`; any other value is REFUSED.

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

### Timeouts

`NanoRuntimeQSearchBackend` defaults to `timeout_s=130`. That is shorter than
the service's default wall-clock budget per job (600 s when
`limits.max_wall_s` is `0`), so a long job can outlast Predator's call. Predator
operators set the client timeout with `PREDATOR_QSEARCH_TIMEOUT_S` (default
130, at most 3600), or pass `timeout_s` when constructing the backend directly.
Set it to at least the job's `max_wall_s` (600 when that is `0`). Add some
margin if jobs can wait for a slot (`NANO_QSEARCH_MAX_CONCURRENT`) or for
recovery at startup.

A timed-out call does not cancel the job. The service finishes the run and
stores the receipt. If Predator resubmits the same job after that, it gets the
stored receipt immediately. If it resubmits while the run is still going, the
resubmission waits for that same run and gets its receipt.

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
