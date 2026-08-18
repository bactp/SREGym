"""Importing this package registers every problem's milestone detector.

One module per ``problem_id`` (mirrors the layout convention of
``sregym/conductor/problems/registry.py``). Add a new problem by adding a
module here and importing it below -- ``sregym.traces.sft.detectors.register``
does the rest.
"""

from . import target_port_misconfig  # noqa: F401 (import order matters: register hand-tuned detectors first)
from . import generic_kagent  # noqa: F401 (registers the generic fallback for every other problem_id)

__all__: list[str] = []
