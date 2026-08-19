#!/usr/bin/env python3
"""Regenerate the strategy catalog from canonical source headers and pinned IR."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nano.library.catalog import (  # noqa: E402
    CatalogValidationError,
    write_catalog,
)


def main() -> int:
    try:
        output = write_catalog(ROOT / "nano" / "library")
    except CatalogValidationError as error:
        for diagnostic in error.diagnostics:
            print(diagnostic.render(), file=sys.stderr)
        return 1
    print(f"wrote {output.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
