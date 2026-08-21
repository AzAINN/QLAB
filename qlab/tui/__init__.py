"""HTTP-client plumbing shared by qlab's terminal surfaces.

Everything here is a client of :mod:`qlab.ui.server`; nothing opens the
registry or a broker directly. The Textual operator console this package was
named for is retired — the Atlas workstation (`clients/atlas-tui`) is the
desk's one terminal client — and what remains is what other surfaces still
build on: the owner API client, the Claude session machinery, and the theme
constants the CLI renders with.
"""

from qlab.tui.client import ApiClient

__all__ = ["ApiClient"]
