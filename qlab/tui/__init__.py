"""Textual operator console for qlab.

The TUI is an HTTP client of :mod:`qlab.ui.server`; it never opens the registry
or broker directly.
"""

from qlab.tui.app import QlabTui
from qlab.tui.client import ApiClient

__all__ = ["ApiClient", "QlabTui"]
