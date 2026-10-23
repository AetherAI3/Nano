"""``python -m nano_qsearch`` runs the q-search service (same as ``nano-qsearch-serve``)."""

import sys

from .service import main

sys.exit(main())
