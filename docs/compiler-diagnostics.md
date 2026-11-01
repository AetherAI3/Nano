# Versioned compiler diagnostics

`nano check --json FILE [FILE ...]` writes exactly one JSON document to stdout,
followed by one newline. It compiles and validates IR without evaluating a
strategy, calling a provider, applying a repair, or submitting an effect.
The default plain-text check interface remains silent on success and reports
`file:line:column: error: message` on source rejection.

```sh
nano check --json strategy.nano
nano check --json --ir-version 1.0.0 first.nano second.nano
```

The envelope has `schemaVersion: "1.0.0"`, compiler identity, aggregate
`success`, and an ordered `results` array (one entry per input argument,
including unreadable files). Results preserve input order and paths as supplied
to the command. Each result contains:

| Field | Contract |
| --- | --- |
| `input` | Input path as displayed by the CLI. |
| `sourceDigest` | `sha256:` plus the SHA-256 of the UTF-8 source text actually compiled, or `null` if unreadable or not UTF-8 encodable. |
| `success` | Compilation and the corresponding IR loader both accepted the program. |
| `diagnostics` | Empty on success; the compiler's first rejection on failure. |
| `irVersion` | Output IR version on success, otherwise `null`. |

`compiler` contains `name` and `version` from the compiler identity embedded in
v1 IR, and `packageVersion` from the installed Nano package. These are separate
version domains. `sourceDigest` addresses consumed source, not raw file bytes:
the CLI strips a UTF-8 BOM and normalizes CRLF/CR newlines while reading, as its
plain-text reader already does. Direct Python calls hash the supplied string
exactly. Comments and whitespace participate in that hash.
An editor/Python string containing an unpaired Unicode surrogate cannot be
encoded as UTF-8. The report rejects it with a positioned `SYNTAX_ERROR` and
`sourceDigest: null`; it does not invent a digest or let hashing raise before
diagnostics. UTF-8 input validation precedes compilation. Invalid UTF-8 file
bytes remain a CLI read failure (`IO_ERROR`, no source span).

Diagnostics have required `code`, `category`, `severity`, `message`, and `span`.
The message is human text; automation must branch on codes and fields instead
of parsing that text. Severity is currently `error`.

| Code | Category | Meaning |
| --- | --- | --- |
| `SYNTAX_ERROR` | `syntax` | Lexing or parsing rejected source. |
| `TYPE_ERROR` | `type` | Name, type, arity, or other semantic rejection. |
| `LOOKAHEAD_ERROR` | `lookahead` | Attempt to read future data. |
| `RESOURCE_LIMIT` | `resource` | Source exceeds the explicit [nesting policy](compiler-limits.md). |
| `CAPABILITY_ERROR` | `capability` | Required tier or manifest capability is unavailable. |
| `IR_VERSION_ERROR` | `version` | Unsupported requested IR version, or a version unable to represent the source. |
| `IR_VALIDATION_ERROR` | `ir` | Lowered IR violates its validation contract. |
| `IO_ERROR` | `io` | The CLI could not read an input. |

A source span is `{start: {line, column}, end: {line, column}}`. Coordinates are
1-based Unicode character positions, not bytes or UTF-16 offsets. Current spans
are points (`start == end`) because the compiler records token starts, not token
extents. EOF positions may refer to the insertion point immediately after the
last character. IR-only, version, and I/O failures use `span: null`; no source
position is guessed. The legacy editor adapter retains its historical
`line=1,column=1` compatibility locator for positionless errors, but its shared
`span` remains `None`.

Optional `expected`, `actual`, and `hint` fields are omitted when unavailable.
Parser expectations supply them at the raising site; they are never inferred
by parsing the message. Hints are at most 240 characters. Current punctuation
hints cite the expected punctuation and the actual source token. Capability
diagnostics do not populate the optional hint field; their existing human
messages still explain which language tier a construct requires. A hint is
explanatory data, never permission to modify source or bypass a host gate.

JSON keys are sorted, separators compact, and non-ASCII text escaped for stable
ASCII bytes. `--verbose` adds nothing to JSON stdout. Read errors and other
operational messages stay on stderr. Exits remain `0` for success, `1` for a
source/IR rejection, `2` for an invalid requested IR version, and `3` for I/O
failure. With mixed inputs, source rejection takes precedence, then version
usage, then I/O. Unsupported versions are compiler diagnostics rather than
argparse choices, so `--json --ir-version unknown` still emits JSON.

The machine schema is [compiler-diagnostics.schema.json](compiler-diagnostics.schema.json).
Compatible optional fields may be added within this schema major version;
consumers should ignore unknown optional fields. Incompatible field, coordinate,
or code-meaning changes require a new major schema version. Codes above are
stable; message wording is not a protocol.

## Python and editor adapters

`nano.compiler.check_report(source, ir_version=None, limits=...)` returns an
immutable report with shared `CompilerDiagnostic` objects. `diagnostic_for_error`
converts known compiler/IR failures only; unexpected implementation exceptions
remain visible. `nano.aethercode.diagnostics` projects the same representation
into its compatible editor fields and accepts typed v1 programs. The independent
editor-preview work in PR #68 remains separate.

## Bounded repair fixture

The offline fixture consumes the CLI document, permits at most one allowlisted
missing-`)` insertion at the actual `{` token, then rechecks once. It does not
execute hints, invent symbols/evidence, change a tier, or invoke a model.
Tests include an unrepairable program and a program which recovers syntax while
still failing an independent semantic task check. Valid-program recovery and
semantic task correctness are reported separately; a successful compiler check
alone cannot establish that an agent solved its task.

Run the deterministic fixture with:

```text
python scripts/diagnostic_repair_fixture.py --output repair-results.json
```

The checked fixture run on Python 3.11.9 produced:

| Fixture | Corrections / rechecks | Final valid | Semantic task correct |
| --- | ---: | --- | --- |
| Missing `)` in the requested observe rule | 1 / 1 | yes | yes |
| Valid rule buying instead of observing | 0 / 0 | yes | no |
| Missing `)` in a rule buying instead of observing | 1 / 1 | yes | no |

Both invalid programs recovered validity; only one of the three final programs
matched the fixed task oracle. This measures this allowlisted fixture policy,
not a model's repair rate or general task correctness. Regression tests also
cover refused edits, stale digests, forged metadata, and a failed single recheck.
