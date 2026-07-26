"""Runs the CLI as `python -m nano.cli`, for checkouts without the console script."""

from .main import main

raise SystemExit(main())
