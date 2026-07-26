"""The `nano` developer workflow.

`check` and `compile` answer "is this correct, and what is the artifact",
`replay` answers "what would it have proposed", and `visualize` answers "what does
the graph look like". Together they are the difference between a language
specification and something you can work in.

Installed as the `nano` console script; also runnable as `python -m nano.cli`.
"""

from .commands import EXIT_DIAGNOSTICS, EXIT_IO, EXIT_OK, EXIT_USAGE, Console
from .main import build_parser, main
from .render import FORMATS, graph_document, render

__all__ = [
    "Console",
    "EXIT_DIAGNOSTICS",
    "EXIT_IO",
    "EXIT_OK",
    "EXIT_USAGE",
    "FORMATS",
    "build_parser",
    "graph_document",
    "main",
    "render",
]
