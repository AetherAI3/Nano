"""Regenerate the checked-in golden receipts.

    py -3.11 tests/regen_goldens.py

Run this after an *intentional* change to the receipt shape — and bump
``RECEIPT_VERSION`` in the same commit — or after a Nano version bump, which
moves every golden because ``identity.nanoVersion`` is part of the artifact.

This is a script rather than a `__main__` block inside the test module because
running a file in `tests/` puts `tests/` on `sys.path`, not the repository root,
so `import nano` fails. It inserts the root itself, which keeps the command the
same on every shell and platform.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_receipts import GOLDEN, golden_cases  # noqa: E402
from nano.runtime.receipt import canonical_bytes  # noqa: E402


def main() -> None:
    GOLDEN.mkdir(exist_ok=True)
    for name, receipt in golden_cases().items():
        path = GOLDEN / f"receipt_{name}.json"
        payload = canonical_bytes(receipt)
        changed = not path.exists() or path.read_bytes() != payload
        path.write_bytes(payload)
        print(f"{'wrote  ' if changed else 'same   '} {path.name}  ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
