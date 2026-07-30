"""Shared colors and rendered CSS for the qlab terminal surfaces."""

from string import Template


# ---------------------------------------------------------------------------
# Palette — one Bloomberg-inspired high-contrast scheme, defined once and shared
# by the CSS (below) and every inline markup color. Amber phosphor on true
# black, saturated up/down, a cyan interaction accent. Bright enough to read at
# a glance across a wide terminal; changing a role's colour means changing it
# here only.
# ---------------------------------------------------------------------------
PALETTE_NAME = "qlab amber phosphor"

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

# The stylesheets substitute each token with a reference to itself, so the CSS
# reaches Textual as `$bg` rather than a frozen literal and a theme switch
# repaints chrome. Substitution is retained rather than dropped because it still
# fails loud when a stylesheet names a token that does not exist.
# qlab.tui.design.tokens publishes every one of these names as a theme variable.
TOKEN_REFS = {name: f"${name}" for name in TOKENS}

# One state → (glyph, colour) table shared by the flowchart and the agent rail.
STATE_STYLE = {
    "working": ("●", CYAN),
    "queued": ("◐", GOLD),
    "waiting": ("◐", GOLD),
    "done": ("✓", UP),
    "failed": ("×", DOWN),
    "blocked": ("!", AMBER),
    "interrupted": ("Ⅱ", GOLD),
    "abandoned": ("×", DIM),
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

    /* ── CHANGE #2 ── spine / nav visual hierarchy ─────────────────────── */
    #spine {
        width: 24;
        min-width: 18;
        padding: 0 0 0 0;
        background: $bg_panel;
        border-right: solid $border_hi;
    }
    #wordmark {
        height: 4;
        padding: 1 0 0 1;
        color: $amber_hi;
        text-style: bold;
    }
    #nav {
        /* One row per view. Hard-coding 9 silently truncated the tenth the
           moment a view was added — the row simply was not drawn, with no
           error anywhere. */
        height: 10;
        color: $muted;
        margin-top: 0;
    }
    #universe-label {
        height: 1;
        margin-top: 0;
        padding: 0 1;
        background: $bg_raised;
        color: $amber;
        text-style: bold;
    }
    #universe {
        height: 1fr;
        background: transparent;
        border: none;
        border-top: solid $border;
        scrollbar-size: 0 0;
    }
    #universe ListItem {
        height: 1;
        padding: 0 1;
        color: $muted;
    }
    #universe ListItem.-highlight {
        background: $sel_bg;
        color: $amber_hi;
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
        border-bottom: solid $border;
        padding-bottom: 0;
    }
    /* ── MARKET TAB ── split layout: braille chart left, stat sidebar right */
    #market-split {
        width: 1fr;
        height: 1fr;
    }
    #market-chart-col {
        width: 1fr;
        height: 1fr;
        padding: 0;
    }
    #market-chart {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 0 0;
    }
    #market-stats-col {
        width: 28;
        min-width: 22;
        height: 1fr;
        background: $bg_panel;
        border-left: solid $border_hi;
    }
    #market-stats-header {
        height: 4;
        padding: 1 1 0 1;
        border-bottom: solid $border;
    }
    #market-stats-body {
        height: 1fr;
        padding: 1 1 0 1;
        overflow-y: auto;
        scrollbar-size: 0 0;
    }
    /* legacy id — zero height so it never occupies space or renders a grey box */
    #market-content {
        height: 0;
        display: none;
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
    /* ── CHANGE #3 ── dashboard tile header polish ───────────────────────── */
    .dashboard-tile {
        width: 1fr;
        height: auto;
        min-height: 6;
        background: $bg_panel;
        border: solid $border;
        border-left: solid $amber;
    }
    /* A one-row band has exactly one row for its text, so a border-bottom
       consumes it and the tile renders as an unlabelled grey strip. The raised
       background already separates the band from the body; the rule does not
       need to, and cannot afford to. */
    .tile-title {
        height: 1;
        padding: 0 1;
        background: $bg_raised;
        color: $amber;
        text-style: bold;
    }
    /* Vertical padding is spent per tile, so on an eight-tile grid it costs a
       sixth of the viewport and pushes the last row below the fold. The row of
       breathing space above the content comes from the title band instead. */
    .tile-content {
        height: auto;
        min-height: 3;
        padding: 0 1;
        color: $text;
    }
    #tile-allocation, #tile-market-pulse {
        min-height: 12;
    }
    #dashboard-actions {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }
    .view-action-button {
        width: auto;
        min-width: 10;
        height: 3;
        margin-left: 1;
        padding: 0 1;
        background: $bg_raised;
        color: $text;
        border: round $border;
        text-style: bold;
    }
    .view-action-button:hover {
        background: $sel_bg;
        color: $text_hi;
        border: round $border_hi;
    }
    .view-action-button:focus {
        border: round $amber;
    }
    .view-action-button:disabled {
        background: $disabled_bg;
        color: $disabled_text;
        border: round $disabled_border;
        text-style: none;
    }
    /* ── CHANGE 3 ── Workforce status block: reactive accent border by run state */
    #workforce-content {
        height: auto;
        max-height: 28%;
        padding: 1 2;
        background: $bg_panel;
        border: solid $border;
        border-left: solid $border_hi;
    }
    #workforce-content.-running {
        border-left: solid $cyan;
    }
    #workforce-content.-complete {
        border-left: solid $up;
    }
    #workforce-content.-failed {
        border-left: solid $down;
    }
    #workforce-content.-blocked {
        border-left: solid $amber;
    }
    #workforce-content.-interrupted {
        border-left: solid $gold;
    }

    /* ── CHANGE 1 ── Flowchart: section header strip + legend row */
    #flow-section {
        height: auto;
        margin-top: 1;
    }
    #flow-header {
        height: 1;
        padding: 0 2;
        color: $amber;
        text-style: bold;
    }
    #flow-row {
        height: 7;
        margin-top: 0;
        padding: 0 2;
        align: left middle;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 1 0;
        border-top: solid $border;
        border-bottom: solid $border;
    }
    #flow-legend {
        height: 1;
        padding: 0 2;
        color: $dim;
    }

    /* ── CHANGE 1 ── FlowNode: taller card with role subtitle line */
    FlowNode {
        width: 14;
        height: 5;
        padding: 0;
        border: solid $border;
        border-left: solid $border_hi;
        color: $muted;
        content-align: center middle;
        text-align: center;
    }
    FlowNode.-working {
        border: solid $cyan;
        border-left: solid $cyan;
        background: $flow_working_bg;
        color: $text_hi;
        text-style: bold;
    }
    FlowNode.-queued {
        border: solid $flow_queued_border;
        border-left: solid $gold;
        color: $flow_queued_text;
    }
    FlowNode.-done {
        border: solid $flow_done_border;
        border-left: solid $up;
        color: $flow_done_text;
    }
    FlowNode.-failed {
        border: solid $flow_failed_border;
        border-left: solid $down;
        color: $flow_failed_text;
    }
    FlowNode.-blocked {
        border: solid $flow_blocked_border;
        border-left: solid $amber;
        color: $flow_blocked_text;
    }
    FlowNode.-interrupted {
        border: solid $flow_queued_border;
        border-left: solid $gold;
        color: $flow_queued_text;
    }
    FlowNode.-abandoned {
        border: solid $border;
        border-left: solid $dim;
        color: $dim;
    }
    .flow-arrow {
        width: 4;
        height: 5;
        content-align: center middle;
        text-align: center;
        color: $border_hi;
    }
    #workforce-console {
        height: 1fr;
        margin-top: 0;
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
    /* Auto, not a fixed seven rows: these carry the Atlas panel as well as
       their own copy, which is taller than that — so the block clipped
       mid-sentence and the table below started over the remains of it. The
       cap keeps a long panel from pushing the table off the view. */
    #research-summary, #audit-summary {
        height: auto;
        max-height: 60%;
        overflow-y: auto;
        color: $text;
    }
    #runs-table, #audit-table {
        height: 1fr;
        background: transparent;
        border: none;
        scrollbar-size: 1 1;
    }
    /* Bloomberg table: amber column headers, accent cursor row */
    DataTable > .datatable--header {
        background: $bg_raised;
        color: $amber;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $sel_bg;
        color: $amber_hi;
        text-style: bold;
    }
    DataTable > .datatable--odd-row {
        background: $bg;
    }
    DataTable > .datatable--even-row {
        background: $bg_panel;
    }

    /* CHANGE #2: book section titles as Bloomberg field-group labels */
    .book-section-title {
        height: 1;
        margin-top: 1;
        padding: 0 1;
        background: $bg_raised;
        color: $amber;
        text-style: bold;
    }
    .book-section {
        width: 1fr;
        height: auto;
        min-height: 3;
        padding: 1 2;
        background: $bg_panel;
        border: round $border;
        color: $text;
    }
    #book-plans {
        width: 1fr;
        height: auto;
    }
    .book-plan-card {
        display: none;
        width: 1fr;
        height: 4;
        margin-bottom: 1;
        padding: 0 1;
        background: $bg_panel;
        border: round $border;
    }
    .book-plan-copy {
        width: 1fr;
        height: 3;
        padding: 0 1;
        color: $text;
    }
    .book-execute-button {
        width: 12;
        min-width: 12;
        margin-left: 2;
    }

    #reference-split {
        height: 1fr;
    }
    #reference-list {
        width: 34;
        background: transparent;
        border: none;
        border-right: solid $border;
        scrollbar-size: 1 1;
    }
    #reference-list ListItem {
        height: 1;
        padding: 0 1;
        background: transparent;
        color: $text;
    }
    #reference-list ListItem.-highlight {
        background: $sel_bg;
        color: $text_hi;
    }
    #reference-list ListItem:disabled {
        background: transparent;
    }
    #reference-detail-scroll {
        width: 1fr;
        height: 1fr;
        padding: 0 2;
        scrollbar-size: 1 1;
    }
    #reference-detail {
        width: 1fr;
        height: auto;
        color: $text;
    }

    /* The one interactive card on the page: its buttons need a row of their
       own, and the copy above them must size to its content. */
    #settings-desk {
        width: 1fr;
        height: auto;
    }
    #settings-desk-copy {
        height: auto;
    }
    #settings-desk-actions {
        height: auto;
        min-height: 3;
        padding-top: 1;
    }
    #settings-desk-actions Button {
        margin-right: 1;
    }
    #news-summary {
        height: auto;
        padding: 0 1;
    }
    /* The story list is the only part that should scroll: the window summary
       and the coverage line must stay visible while reading down. */
    #news-scroll {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    #news-stories {
        height: auto;
    }
    #settings-workforce, #settings-workforce-copy {
        width: 1fr;
        height: auto;
    }
    #settings-workforce-actions {
        height: auto;
        min-height: 3;
        padding-top: 1;
    }
    #settings-workforce-actions Button {
        margin-right: 1;
    }
    .settings-card {
        width: 1fr;
        height: auto;
        min-height: 5;
        margin-bottom: 1;
        padding: 1 2;
        background: $bg_panel;
        border: solid $border;
        border-left: solid $border_hi;
        color: $text;
    }

    /* ── CHANGE #2 ── agent rail: Bloomberg field-group headers ─────────── */
    #agent-rail {
        width: 38;
        min-width: 31;
        padding: 0 0 0 0;
        background: $bg_panel;
        border-left: solid $border_hi;
    }
    /* Same one-row constraint as .tile-title: the border would eat the only
       row the label has, leaving three blank strips down the rail. */
    #agent-label, #work-label, #atlas-label {
        height: 1;
        padding: 0 1;
        background: $bg_raised;
        color: $amber;
        text-style: bold;
    }
    /* No border here either — it is the same single row as the labels above,
       and the rail's own left border already separates it from the canvas. */
    #atlas-rail {
        height: auto;
        margin-bottom: 0;
        padding: 1 1 1 2;
        border-bottom: solid $border;
    }
    #agent-list {
        height: 16;
        padding: 1 1 1 2;
    }
    #work-label {
        margin-top: 0;
    }
    #selected-work {
        height: 1fr;
        color: $text;
        overflow-y: auto;
        scrollbar-size: 1 1;
        padding: 1 1 0 2;
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
    /* ── CHANGE #1 ── Bloomberg-style status bar ─────────────────────────── */
    #command-row {
        height: 3;
        padding: 0 0;
        background: $bg_raised;
        border-top: solid $border_hi;
    }
    #command {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 2;
        background: transparent;
        color: $text_hi;
        margin-top: 1;
    }
    #command:focus {
        border: none;
    }
    #conn-chip {
        width: auto;
        padding: 1 1 0 1;
        height: 3;
        text-align: right;
        color: $muted;
        border-left: solid $border;
    }
    /* The only always-visible answer to "whose money is this". Synthetic is
       muted; live prices on a simulated book warn; a real Alpaca book takes
       the alert tone so it can never be mistaken for the demo. */
    #mode-chip {
        width: auto;
        padding: 1 2 0 1;
        height: 3;
        text-align: right;
        color: $muted;
        border-left: solid $border;
    }
    #mode-chip.live-data {
        color: $amber;
    }
    #mode-chip.live-book {
        color: $down;
        text-style: bold;
    }

    Screen.compact #spine {
        width: 20;
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

    /* ── CHANGE 4 ── Atlas view: pinned status strip + scrollable read body */
    #atlas-status-strip {
        height: 3;
        padding: 1 2 0 2;
        background: $bg_raised;
        border-bottom: solid $border_hi;
        color: $text;
    }
    #atlas-read-scroll {
        height: 1fr;
        overflow-y: auto;
        scrollbar-size: 1 1;
        padding: 1 2 1 2;
    }
    #atlas-read {
        height: auto;
        color: $text;
    }
    #atlas-actions {
        height: 3;
        padding: 0 2;
        align-vertical: middle;
        border-top: solid $border;
    }
    """)

APP_CSS = APP_CSS_TEMPLATE.substitute(TOKEN_REFS)


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

PAPER_MODAL_CSS = PAPER_MODAL_CSS_TEMPLATE.substitute(TOKEN_REFS)


ATLAS_DRAWER_CSS_TEMPLATE = Template("""
    /* ── CHANGE 5 ── Atlas drawer: visual section cards + approval blocks */
    AtlasDrawerScreen {
        align: right middle;
        background: $overlay;
    }
    #atlas-drawer {
        width: 80;
        height: 100%;
        padding: 0;
        background: $bg_raised;
        border-left: solid $amber;
    }
    #atlas-drawer-title {
        height: 3;
        padding: 1 3;
        color: $amber_hi;
        text-style: bold;
        background: $bg_panel;
        border-bottom: solid $border_hi;
    }
    #atlas-drawer-body {
        height: 1fr;
        color: $text;
        padding: 1 3;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }
    #atlas-drawer-hint {
        height: 2;
        padding: 0 3;
        color: $dim;
        border-top: solid $border;
    }
    """)

ATLAS_DRAWER_CSS = ATLAS_DRAWER_CSS_TEMPLATE.substitute(TOKEN_REFS)


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

WORKFORCE_MODAL_CSS = WORKFORCE_MODAL_CSS_TEMPLATE.substitute(TOKEN_REFS)


DESK_MODAL_CSS_TEMPLATE = Template("""
    DeskModeScreen {
        align: center middle;
        background: $overlay;
    }
    #desk-dialog {
        width: 62;
        height: auto;
        padding: 1 2;
        background: $bg_raised;
        border: round $border_hi;
    }
    #desk-dialog-title {
        color: $amber;
        text-style: bold;
    }
    #desk-data-row, #desk-book-buttons {
        height: auto;
        padding: 1 0;
    }
    #desk-data-row Button, #desk-book-buttons Button {
        margin-right: 1;
    }
    #desk-credentials {
        color: $muted;
    }
    #desk-book-row {
        height: auto;
    }
    #desk-book-title {
        color: $amber;
        text-style: bold;
        padding-top: 1;
    }
    #desk-book-copy {
        color: $muted;
        padding-top: 1;
    }
    #desk-actions {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    """)

DESK_MODAL_CSS = DESK_MODAL_CSS_TEMPLATE.substitute(TOKEN_REFS)
