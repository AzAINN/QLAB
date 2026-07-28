"""Design system for the operator client: tokens, glyphs, render primitives.

Views never name a colour and never format a styled string themselves. They
call the primitives in this package, which resolve semantic roles from the
active theme. That is what keeps a theme switch a token change rather than a
sweep through every widget.

Submodules are imported directly (``from qlab.tui.design import tokens``) so
this package stays free of import-order coupling.
"""
