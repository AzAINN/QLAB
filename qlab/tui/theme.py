"""Shared colors and rendered CSS for the qlab terminal surfaces."""

from string import Template


# ---------------------------------------------------------------------------
# Palette — one Bloomberg-inspired high-contrast scheme, defined once and shared
# by the CSS (below) and every inline markup color. Amber phosphor on true
# black, saturated up/down, a cyan interaction accent. Bright enough to read at
# a glance across a wide terminal; changing a role's colour means changing it
# here only.
# ---------------------------------------------------------------------------
BG        = "#03060b"   # canvas — near-black
BG_PANEL  = "#070c13"   # side rails / raised panels
BG_RAISED = "#0f1926"   # table headers, dialogs, input wells
SEL_BG    = "#14273b"   # selected row / highlighted list item
BORDER    = "#1d2b3b"   # quiet dividers
BORDER_HI = "#3a5069"   # active dividers / focus
TEXT      = "#cdd9e6"   # default body text (cool white)
TEXT_HI   = "#f6fafe"   # bright emphasis
MUTED     = "#8797a8"   # secondary text
DIM       = "#586777"   # tertiary / captions
AMBER     = "#ffb020"   # primary Bloomberg amber (accent, titles bar)
AMBER_HI  = "#ffcf66"   # bright amber (section titles, focus)
GOLD      = "#eb9a2e"   # secondary amber/orange (queued, warnings)
UP        = "#1fe07b"   # positive / done
DOWN      = "#ff5257"   # negative / failed
CYAN      = "#38ccff"   # interactive accent / working

LABEL_GOLD = "#9a7f4a"
DISABLED_TEXT = "#55657a"
DISABLED_BORDER = "#16212e"
DISABLED_BG = "#060a10"
ALLOCATION_TRACK = "#33475a"
CHART_AXIS = "#33465b"
FLOW_WORKING_BG = "#0a2233"
FLOW_QUEUED_BORDER = "#6b5836"
FLOW_QUEUED_TEXT = "#bda879"
FLOW_DONE_BORDER = "#3f6b53"
FLOW_DONE_TEXT = "#bfe0cd"
FLOW_FAILED_BORDER = "#7d3a3a"
FLOW_FAILED_TEXT = "#edb6b6"
FLOW_BLOCKED_BORDER = "#8a6a2f"
FLOW_BLOCKED_TEXT = "#ecd4a5"
OVERLAY = "#03060bcc"
SUCCESS_PALE = "#7cf0b4"
SUCCESS_BG = "#08160f"
SUCCESS_BORDER = "#1f6a44"
DANGER_PALE = "#ff9c9e"
DANGER_BG = "#1a0a0b"
DANGER_BORDER = "#6a2325"
WARNING_PALE = "#ffd98a"
WARNING_BORDER = "#6a4c18"
QUEUED_BORDER = "#5a4420"
CYAN_PALE = "#d7f4ff"

TOKENS = {
    "bg": BG,
    "bg_panel": BG_PANEL,
    "bg_raised": BG_RAISED,
    "sel_bg": SEL_BG,
    "border": BORDER,
    "border_hi": BORDER_HI,
    "text": TEXT,
    "text_hi": TEXT_HI,
    "muted": MUTED,
    "dim": DIM,
    "amber": AMBER,
    "amber_hi": AMBER_HI,
    "gold": GOLD,
    "up": UP,
    "down": DOWN,
    "cyan": CYAN,
    "label_gold": LABEL_GOLD,
    "disabled_text": DISABLED_TEXT,
    "disabled_border": DISABLED_BORDER,
    "disabled_bg": DISABLED_BG,
    "allocation_track": ALLOCATION_TRACK,
    "chart_axis": CHART_AXIS,
    "flow_working_bg": FLOW_WORKING_BG,
    "flow_queued_border": FLOW_QUEUED_BORDER,
    "flow_queued_text": FLOW_QUEUED_TEXT,
    "flow_done_border": FLOW_DONE_BORDER,
    "flow_done_text": FLOW_DONE_TEXT,
    "flow_failed_border": FLOW_FAILED_BORDER,
    "flow_failed_text": FLOW_FAILED_TEXT,
    "flow_blocked_border": FLOW_BLOCKED_BORDER,
    "flow_blocked_text": FLOW_BLOCKED_TEXT,
    "overlay": OVERLAY,
    "success_pale": SUCCESS_PALE,
    "success_bg": SUCCESS_BG,
    "success_border": SUCCESS_BORDER,
    "danger_pale": DANGER_PALE,
    "danger_bg": DANGER_BG,
    "danger_border": DANGER_BORDER,
    "warning_pale": WARNING_PALE,
    "warning_border": WARNING_BORDER,
    "queued_border": QUEUED_BORDER,
    "cyan_pale": CYAN_PALE,
}

# One state → (glyph, colour) table shared by the flowchart and the agent rail.
STATE_STYLE = {
    "working": ("●", CYAN),
    "queued": ("◐", GOLD),
    "waiting": ("◐", GOLD),
    "done": ("✓", UP),
    "failed": ("×", DOWN),
    "blocked": ("!", AMBER),
    "idle": ("◌", DIM),
}



APP_CSS_TEMPLATE = Template("""
    Screen {
        layout: vertical;
        background: $bg;
        color: $text;
    }

    #workspace {
        height: 1fr;
    }

    #spine {
        width: 24;
        min-width: 18;
        padding: 1 1 0 1;
        background: $bg_panel;
        border-right: solid $border;
    }
    #wordmark {
        height: 3;
        color: $text_hi;
        text-style: bold;
    }
    #nav {
        height: 7;
        color: $muted;
    }
    #universe-label {
        height: 2;
        margin-top: 1;
        color: $label_gold;
    }
    #universe {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 0 0;
    }
    #universe ListItem {
        height: 1;
        padding: 0;
        color: $muted;
    }
    #universe ListItem.-highlight {
        background: $sel_bg;
        color: $text_hi;
        text-style: bold;
    }

    #canvas {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: $bg;
    }
    .canvas-view {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    .canvas-title {
        height: 2;
        color: $amber_hi;
        text-style: bold;
    }
    #market-content {
        height: 1fr;
    }
    #dashboard-grid {
        width: 1fr;
        height: auto;
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-rows: auto;
        grid-gutter: 1 2;
    }
    .dashboard-tile {
        width: 1fr;
        height: auto;
        min-height: 6;
        background: $bg_panel;
        border: round $border;
    }
    .tile-title {
        height: 2;
        padding: 0 1;
        background: $bg_raised;
        color: $muted;
        text-style: bold;
    }
    .tile-content {
        height: auto;
        min-height: 3;
        padding: 1;
        color: $text;
    }
    #tile-allocation, #tile-market-pulse {
        min-height: 12;
    }
    #workforce-content {
        height: auto;
        max-height: 30%;
        padding: 1 2;
        background: $bg_panel;
        border: round $border;
    }
    #flow-row {
        height: 6;
        margin-top: 1;
        align: left middle;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 1 0;
    }
    FlowNode {
        width: 12;
        height: 4;
        padding: 0;
        border: round $border;
        color: $muted;
        content-align: center middle;
        text-align: center;
    }
    FlowNode.-working {
        border: round $cyan;
        background: $flow_working_bg;
        color: $text_hi;
        text-style: bold;
    }
    FlowNode.-queued { border: round $flow_queued_border; color: $flow_queued_text; }
    FlowNode.-done { border: round $flow_done_border; color: $flow_done_text; }
    FlowNode.-failed { border: round $flow_failed_border; color: $flow_failed_text; }
    FlowNode.-blocked { border: round $flow_blocked_border; color: $flow_blocked_text; }
    .flow-arrow {
        width: 3;
        height: 4;
        content-align: center middle;
        text-align: center;
        color: $dim;
    }
    #workforce-console {
        height: 1fr;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
        border: none;
        border-top: solid $border;
        color: $text;
        scrollbar-size: 1 1;
    }
    #chat-row {
        height: 3;
        margin-top: 1;
    }
    #chat-mode {
        width: 6;
        height: 3;
        padding: 1 0 0 1;
    }
    #chat-input {
        width: 1fr;
        height: 3;
        border: round $border;
        padding: 0 1;
        background: $bg_panel;
        color: $text_hi;
    }
    #chat-input:focus {
        border: round $amber;
    }
    #chat-input:disabled {
        border: round $disabled_border;
        background: $disabled_bg;
        color: $disabled_text;
    }
    #chat-exit {
        width: 10;
        height: 3;
        min-width: 8;
        margin-left: 1;
        background: $bg_raised;
        color: $text;
        border: round $border;
    }
    #chat-exit:hover {
        background: $border;
        color: $text_hi;
    }
    #research-summary, #audit-summary {
        height: 7;
        color: $text;
    }
    #runs-table, #audit-table {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: $bg_raised;
        color: $muted;
        text-style: none;
    }
    DataTable > .datatable--cursor {
        background: $sel_bg;
        color: $text_hi;
    }

    #agent-rail {
        width: 38;
        min-width: 31;
        padding: 1 1 0 2;
        background: $bg_panel;
        border-left: solid $border;
    }
    #agent-label, #work-label {
        height: 2;
        color: $label_gold;
    }
    #agent-list {
        height: 16;
    }
    #work-label {
        margin-top: 1;
    }
    #selected-work {
        height: 1fr;
        color: $text;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }

    #timeline {
        height: 10;
        display: none;
        padding: 0 2;
        background: $bg_panel;
        border-top: solid $border;
        color: $muted;
        scrollbar-size: 1 1;
    }
    #event-strip {
        height: 1;
        padding: 0 1;
        background: $bg_raised;
        color: $muted;
    }
    #command-row {
        height: 2;
        padding: 0 1;
        background: $bg_panel;
    }
    #command {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
        color: $text_hi;
    }
    #command:focus {
        border: none;
    }
    #system-status {
        width: auto;
        min-width: 28;
        height: 1;
        text-align: right;
        color: $muted;
    }

    Screen.compact #spine {
        width: 19;
    }
    Screen.compact #agent-rail {
        width: 31;
        padding-left: 1;
    }
    Screen.narrow #agent-rail {
        display: none;
    }
    Screen.narrow #spine {
        width: 18;
    }
    Screen.narrow.agent-focus #spine,
    Screen.narrow.agent-focus #canvas {
        display: none;
    }
    Screen.narrow.agent-focus #agent-rail {
        display: block;
        width: 1fr;
        border-left: none;
        padding-left: 2;
    }
    """)

APP_CSS = APP_CSS_TEMPLATE.substitute(TOKENS)


PAPER_MODAL_CSS_TEMPLATE = Template("""
    PaperConfirmScreen {
        align: center middle;
        background: $overlay;
    }
    #paper-dialog {
        width: 68;
        height: auto;
        padding: 2 3;
        background: $bg_raised;
        border: solid $gold;
    }
    #paper-dialog-title {
        color: $text_hi;
        text-style: bold;
        margin-bottom: 1;
    }
    #paper-dialog-copy {
        color: $text;
        margin-bottom: 2;
    }
    #paper-dialog-actions {
        height: 3;
        align-horizontal: right;
    }
    #paper-dialog-actions Button {
        margin-left: 1;
    }
    """)

PAPER_MODAL_CSS = PAPER_MODAL_CSS_TEMPLATE.substitute(TOKENS)


WORKFORCE_MODAL_CSS_TEMPLATE = Template("""
    ClaudeWorkforceScreen {
        align: center middle;
        background: $overlay;
    }
    #workforce-dialog {
        width: 72;
        height: auto;
        padding: 2 3;
        background: $bg_raised;
        border: solid $border_hi;
    }
    #workforce-dialog-title {
        color: $text_hi;
        text-style: bold;
        margin-bottom: 1;
    }
    #workforce-dialog-copy {
        color: $text;
        margin-bottom: 2;
    }
    #workforce-dialog-actions {
        height: 3;
        align-horizontal: right;
    }
    #workforce-dialog-actions Button {
        margin-left: 1;
    }
    """)

WORKFORCE_MODAL_CSS = WORKFORCE_MODAL_CSS_TEMPLATE.substitute(TOKENS)

