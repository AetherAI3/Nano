# Pinned agent evidence helper

This offline example shares `Evidence.sufficient` with `Task.ready`, then calls
`ready` from an agent strategy. A host supplies evidence count, verification
status and a model-token budget. No model or hardware is called.

From the repository root:

```python
from pathlib import Path
from nano.compiler import compile_package, compile_module

root = Path("examples/agent-package")
build = compile_package(root / "nano.json", lock_path=root / "nano.lock")
inline = compile_module((root / "inline.nano").read_text(encoding="utf-8"))
assert build.module.content_hash() == inline.content_hash()
print(build.to_dict()["packageHash"])
```

Or from the command line (see [docs/package-cli.md](../../docs/package-cli.md)):

```text
nano package check --manifest examples/agent-package/nano.json --lock examples/agent-package/nano.lock -v
nano package build --manifest examples/agent-package/nano.json --lock examples/agent-package/nano.lock -o package.json --module-output module.json
```

`check` is silent on success (`-v` prints both hashes, `--json` emits one
canonical `NanoPackageCheck` document). `build` writes the same canonical,
LF-terminated `NanoPackage` envelope the API returns, so its `packageHash`
matches `build.to_dict()["packageHash"]` above. Both commands require both
paths, never search for files, and refuse to write over the manifest, the lock
or any pinned source.

The lock pins the three manifest sources by exact bytes. `inline.nano` is only
the comparison control, outside the package. Preserve LF line endings: byte
hashes deliberately distinguish CRLF. Changes to package sources require
intentional regeneration of the corresponding SHA-256 pin.

See [RFC 0066](../../docs/rfc-0066-hermetic-packages.md) for syntax, fixed limits,
effects, source maps and the distinction between build and executable identity.
