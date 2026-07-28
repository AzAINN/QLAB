# qlab operator TUI: interface design system

Date: 2026-07-26
Status: token/glyph/primitive layer implemented and wired into the two
workforce state renderers; layout grid, breakpoints, and the remaining views
are still proposed
Scope: layout grid, visual language, design-token enforcement, state and
failure rendering, and the visual test strategy for the Textual operator client

## Implementation status

Landed, with 54 tests in `tests/test_design.py` and `tests/test_tui.py`:

- `qlab/tui/design/tokens.py` -- four themes as `textual.theme.Theme` objects,
  WCAG helpers, contrast and sign-separation invariants;
- `qlab/tui/design/glyphs.py` -- eleven states, Unicode and ASCII tables, and a
  capability probe that tests codec round-tripping rather than an
  encoding allowlist;
- `qlab/tui/design/primitives.py` -- `section`, `field`, `num`, `state_badge`,
  `absent`, `rule`;
- theme registration on `QlabTui` with a `: theme <name>` command;
- `FlowNode` and `AgentRail` render through `state_badge` and return `Text`.

Chrome and inline markup now follow the theme, which was the change that made
the light theme usable rather than merely present:

- `qlab/tui/theme.py` substitutes each CSS token with a reference to *itself*,
  so the stylesheets reach Textual as `$bg` rather than a frozen literal.
  Substitution was kept rather than dropped because it still fails loud when a
  stylesheet names a token that does not exist.
- `qlab/tui/design/tokens.py` publishes all 42 legacy token names as theme
  variables aliased onto semantic roles, so the 645-line stylesheet and roughly
  five hundred inline markup sites became theme-reactive without editing a call
  site.
- `qlab/tui/design/markup.py` binds the legacy colour constant names to
  `$variable` references. `app.py` changed its import source only.
- Themes register in `QlabTui.__init__`, not `on_mount`: the stylesheet is
  parsed during startup and an unregistered variable is a hard
  `UnresolvedVariableError`.

Two consumers cannot take variables and keep real colours by design:
`qlab/desk_cli.py` (prints through Rich) and `RichLog` (renders with
`rich.text.Text.from_markup`). The console path resolves variables itself via
`markup.resolve`.

Not yet done, and each is a separate slice:

- the layout grid, the alignment contract, and the breakpoint table are
  specified below but not applied to any view;
- `_render_workforce` still assembles markup strings rather than composing
  `field`/`section`/`num`; its colours are theme-reactive now, but its layout
  does not yet obey the 14-column contract;
- the alias layer is a migration surface, not an API. It shrinks as views move
  onto the primitives, and `amber`/`gold`/`cyan` should disappear entirely once
  no view names them.

Companion to `2026-07-26-tui-code-review-and-architecture.md`. That document
owns ownership boundaries, state flow, the owner services, the migration
phases, and the governance regression set. This document owns only what that
one deliberately left open: what the interface looks like, and how the look is
prevented from drifting. Where the two disagree, the architecture document wins
on structure and this one wins on presentation.

## Decisions taken

Four decisions were settled before design, and they constrain everything below.

1. **Protocol-first hybrid.** The typed owner protocol is the product boundary,
   not the Textual widget tree. Textual remains the only client built now. The
   protocol is specified well enough that a second client (Ink, Ratatui, or the
   existing web client) becomes additive work against a stable contract rather
   than a rewrite. This supersedes the earlier framing of the framework choice
   as Textual-versus-Ink: the client language stops being the important
   question once the protocol is the boundary.
2. **Calm institutional visual direction.** Rules and whitespace instead of
   nested boxes, strong typographic hierarchy, one accent. Density is available
   on demand through the evidence drawer, never by default. This is the
   positive form of the architecture document's negative constraints
   ("visual density without hierarchy", "amber/black as decoration").
3. **Cool neutral accent, with real dark and light themes.** The amber-phosphor
   scheme is retired as the canvas identity. Amber is retained as a semantic
   role. Both themes are first-class and both are contrast-validated.
4. **`qlab-dark` is the default.** `qlab-light` is opt-in via `:theme light`.
   A colour-vision-safe variant of each ships alongside them.

## Non-goals

- No rewrite in another language or framework. See the architecture document's
  framework decision matrix.
- No change to any governance behaviour. Nothing here touches the referee
  binding, `targets_hash`, human confirmation, the single-writer rule, or the
  algorithm stage gate.
- No new information on screen. This is a presentation and enforcement layer
  over projections the owner already produces.
- No animation beyond the existing working-state pulse.

## Layout system

The three-region workstation from the architecture document is unchanged. What
follows is the grid it was missing.

### Reference layout, wide (>= 140 columns)

```text
┌ nav 18 ─────┬ canvas · fluid ─────────────────────────┬ rail 34 ────────┐
│             │                                         │                 │
│  Atlas      │  WORKFORCE                              │  ATLAS          │
│  Market     │                                         │  observing      │
│› Workforce  │  workflow      portfolio_v3             │  since 09:14    │
│  Research   │  policy        fast research            │                 │
│  Book       │  elapsed               4m12s            │  attention      │
│  Audit      │                                         │  SPY    -4.2%   │
│             │  ─────────────────────────────────────  │  QQQ    -2.8%   │
│  Universe   │                                         │                 │
│             │  ✓ analyst        done         1m02s    │  ─────────────  │
│             │  ● challenger     working      0m48s    │                 │
│             │  · optimizer      queued           —    │  WORKFORCE      │
│             │  · referee        queued           —    │  challenger     │
│             │  · reporter       queued           —    │  s  stop        │
│             │                                         │                 │
│             │  ─────────────────────────────────────  │  ─────────────  │
│             │                                         │                 │
│             │  evidence      ⏎ open                   │  APPROVALS      │
│             │                                         │  1 pending      │
├─────────────┴─────────────────────────────────────────┴─────────────────┤
│ : command · ask Atlas                   LIVE   DATA · PAPER   qlab-dark │
└─────────────────────────────────────────────────────────────────────────┘
  ↑↓ select   ⏎ evidence   s stop   ^P palette   ? help
```

The nav rail marks the active view with `›` and no highlight bar; the canvas
heading already names the view, so the rail does not need to repeat the
emphasis. The key-help row sits outside the frame and is contextual to the
focused pane.

### The alignment contract

Four rules, applied in every pane of every view. They account for most of the
difference between a terminal that reads as designed and one that reads as
assembled.

1. **The label column is 14 columns, fixed, muted.** Values begin at column 15
   everywhere. Cross-pane alignment is the point; per-pane optimisation is not
   permitted.
2. **Numbers are right-aligned, tabular, and fixed-decimal.** A left-aligned
   number is a defect.
3. **Trailing meta right-aligns to the pane edge** — elapsed, timestamps,
   counts.
4. **One border level maximum.** The pane frame is the only box. Interior
   structure uses rules and blank rows. No nested panels.

### Spacing

Three values only, so vertical rhythm cannot drift:

| Token | Rows | Use |
|---|---|---|
| `space.0` | 0 | adjacent rows within a group |
| `space.1` | 1 | between groups |
| `space.2` | 2 | before a rule-separated section |

### Breakpoints

| Class | Width | nav | canvas | right rail |
|---|---|---|---|---|
| `wide` | >= 140 | 18 cols | fluid | 34 cols |
| `standard` | 100–139 | 16 cols | fluid | 30 cols |
| `compact` | 80–99 | hidden; `^P` palette only | full | bottom strip, 5 rows |
| `narrow` | < 80 | hidden | single column | folded into canvas |

Height is also a breakpoint and is usually forgotten: below 30 rows every
`space.2` collapses to `space.1` and sparklines are hidden. Content degrades,
never chrome — the command row, connection state, and key help are retained at
every size.

## Visual language

### Palette

All values are semantic roles. No widget refers to a colour by name or hex.

**`qlab-dark`** — canvas `#101419`. Deliberately lifted off pure black: on a
true-black field every chromatic value reads as neon, which is most of why the
retiring amber scheme reads as retro rather than calm.

| Role | Hex |
|---|---|
| `bg` | `#101419` |
| `surface` | `#161b22` |
| `panel` | `#1d242e` |
| `border` | `#262f3a` |
| `text` | `#c6d0da` |
| `muted` | `#8895a4` |
| `faint` | `#76828f` |
| `accent` | `#86aed4` |
| `up` | `#6ed3a6` |
| `down` | `#cf6b7d` |
| `blocked` | `#d9a55a` |

**`qlab-light`** — canvas `#f7f9fb`, not pure white; white leaves no room for a
legible muted ramp. Hues are held constant from the dark theme, then darkened
and desaturated. Dark-theme values are never reused on a light field.

| Role | Hex |
|---|---|
| `bg` | `#f7f9fb` |
| `surface` | `#eef2f6` |
| `panel` | `#e4eaf1` |
| `border` | `#d0d9e2` |
| `text` | `#1b242e` |
| `muted` | `#54626f` |
| `faint` | `#6e7b88` |
| `accent` | `#2d5f8c` |
| `up` | `#0b6b4e` |
| `down` | `#9c2a40` |
| `blocked` | `#8a5a12` |

**Colour-vision-safe variants** override three roles each and inherit the rest:

| Variant | `up` | `down` | `accent` |
|---|---|---|---|
| `cvd-dark` | `#bcdcf5` | `#d98428` | `#9aa7b4` |
| `cvd-light` | `#1f5f96` | `#8f4e07` | `#5a6672` |

Blue and orange replace green and red; the accent steps back to neutral slate
so it cannot be confused with the positive sign.

`cvd-dark` uses a pale blue rather than a mid blue, which was found during
implementation: orange sits naturally high in luminance, so a mid blue against
this orange separated by only 1.23:1 and failed the dark-theme sign floor.
Deepening the orange instead dropped it under the 4.5 text target on a raised
panel. Lifting the blue was the only move satisfying both, and it lands at
2.02:1 separation with 5.42:1 worst-case contrast.

### Contrast targets and validation results

| Role | Minimum ratio against both `bg` and `panel` |
|---|---|
| `text` | 7.0 (AAA body) |
| `muted` | 4.5 |
| `faint` | 3.5 (non-essential text) |
| `accent`, `up`, `down`, `blocked` | 4.5 (these are text, not decoration) |

Verified before this document was written: **all four themes pass every
role-against-base pair, zero failures.** Worst case is `faint` on `panel` —
3.99:1 in the dark themes and 3.57:1 in the light themes, both above the 3.5
target. The exact per-cell matrix is not reproduced here because the contrast
test regenerates it; the test is the authority, not this table.

### Hue rationale

The hues are functional choices, not preferences.

- **The accent is blue, hue ~212, because it must be chromatically distant from
  both P&L signs.** A teal or slate-green accent collides with `up`; any warm
  accent collides with `down`. This constraint, not taste, is what rules out
  most of the more distinctive accent candidates.
- **`up` is teal-green (~165) rather than hue-120 green, and `down` is rose-red
  (~350) rather than hue-0 red.** Shifting both away from the classic pair
  widens their separation under deuteranomaly and protanomaly while still
  reading as green and red to everyone else.
- **Amber is retained as `blocked`.** It stops being the canvas identity and
  becomes the single "this needs your attention" signal. The project's existing
  colour keeps a job instead of being the wallpaper.

### What is actually wrong with the current palette

The existing amber-phosphor scheme was audited before it was replaced, and it
holds up better than expected. Recording the real findings so this change is
justified by evidence rather than by preference:

| Measure | Current value | Assessment |
|---|---|---|
| `TEXT` `#cdd9e6` on canvas | 14.17:1 | excellent |
| `UP` `#1fe07b` on canvas | 11.60:1 | excellent |
| `DOWN` `#ff5257` on canvas | 6.37:1 | passes |
| `AMBER` `#ffb020` on canvas | 11.10:1 | passes |
| `DIM` `#586777` on `BG_RAISED` | **3.05:1** | **fails the 3.5 target** |
| `up`/`down` luminance separation | 1.82:1 | already monochrome-safe |

Only one measured defect: **tertiary text is too dim on raised panels**, where
table headers, dialogs, and input wells live. The new `faint` role reaches
3.99:1 in the same position.

Everything else about this change is a capability gain rather than a repair —
light-theme support, colour-vision-safe variants, a canvas lifted off pure
black so chromatic values stop glowing, and a smaller token set. The sign
colours in the current theme are not defective.

### Sign must be encoded twice

Measured luminance separation between `up` and `down`:

| Palette | Separation |
|---|---|
| current amber-phosphor theme | 1.82:1 |
| `qlab-dark` | 1.91:1 |
| `qlab-light` | 1.14:1 |

The dark themes are comfortably monochrome-distinguishable and the proposed one
is marginally better. The light theme reaches only 1.14:1, and that is
structural rather than a tuning failure: on a light canvas both signs must be
dark to pass contrast, so luminance is not available as a channel.

This is a real cost of adding a light theme — it is the weakest sign encoding
in the system, weaker than what exists today. It is accepted rather than solved,
which is precisely why the redundant encoding below is mandatory rather than
advisory:

> **Sign is always encoded twice — an explicit `+` or `-` and a colour. The
> sign character is load-bearing, never decorative. No value anywhere in qlab
> communicates direction by colour alone.**

### Colour discipline

- **Budget: at most three chromatic elements visible per pane at rest**, where a
  chromatic element is one contiguous styled run carrying a non-neutral colour.
  A column of eight signed returns is one element, not eight.
- **All chrome is neutral, always.** Borders, rules, the nav rail, and the
  frame are never accent-tinted.
- **The accent appears in exactly three places**: section headings, the
  `working` state, and the focused pane's title.
- **Sign colour applies to deltas only, never to levels.** `-4.2%` is coloured;
  `412.55` is not. A price is not an opinion.
- **Background inversion is reserved exclusively for error chips and
  confirmation modals.** Nothing else may invert, so an inverted region always
  means stop and read. This also removes the need for a second red: `error`
  uses the `down` hue plus inversion rather than competing as its own colour.

### Type roles

Terminals have no font sizes, so hierarchy comes from case, weight, and the
muted ramp. Six roles, and no seventh:

| Role | Treatment | Example |
|---|---|---|
| `section` | bold, accent, uppercase | `WORKFORCE` |
| `label` | muted, lowercase | `regime` |
| `value` | text | `elevated turbulence` |
| `value.key` | bold, text | the number that matters |
| `meta` | faint | `since 09:14`, `f4a9…` |
| `hint` | faint, lowercase | `s stop` |

**At most one bold element per row, and at most one accent element per
section.** This single constraint prevents the emphasis creep that makes dense
terminals unreadable.

### State glyphs

State is encoded redundantly — glyph and colour — so it survives a monochrome
terminal. An ASCII table covers terminals and code pages without the Unicode
forms.

| State | Glyph | ASCII | Colour |
|---|---|---|---|
| queued | `·` | `.` | `faint` |
| working | `●` | `o` | `accent` |
| done | `✓` | `+` | `up` |
| failed | `✗` | `x` | `down` + inversion |
| blocked | `!` | `!` | `blocked` |
| stopping | `◐` | `~` | `blocked` |
| stale | `~` | `?` | `faint` |

Both tables must have identical keys and equal display widths.

### Degradation

- **16-colour ANSI terminals** use Textual's `Theme(ansi=True)` path:
  `accent`→blue, `up`→green, `down`→red, `blocked`→yellow, `muted`→bright
  black. The hue tuning is lost; every glyph and sign character is retained,
  so no meaning is lost.
- **Unicode-incapable terminals** fall back to the ASCII glyph table by
  capability probe.

### Absent values

Three distinct renderings, mapping directly onto the fail-loud invariant:

| Meaning | Render |
|---|---|
| measured zero | `0.00` |
| not computed or unknown | `—`, faint |
| refused by a gate | `gated`, `blocked` colour, reason in the evidence drawer |

An unknown value is never rendered as `0.00`.

## Enforcement in code

A terminal design system fails when each widget formats its own strings. Two
small modules prevent that, and three tests keep them honest.

```text
qlab/tui/design/
  tokens.py       # the four Theme objects and semantic names; the only file with hex
  primitives.py   # pure render functions; the only sanctioned way to emit styled content
  glyphs.py       # unicode and ascii tables, selected by capability probe
```

`primitives.py` has no Textual dependency. It is pure functions returning Rich
renderables, so it unit-tests without a terminal:

```python
section("WORKFORCE")                      # bold, accent, uppercase
field("regime", "elevated turbulence",    # label padded to 14, value at column 15
      meta="since 09:14")                 # meta right-aligned, faint
num(-0.042, "pct", signed=True)           # "-4.20%", right-aligned, down colour
state_badge("working")                    # glyph plus colour, ascii-aware
absent("gated")                           # the 0.00 / — / gated distinction
rule(width)
```

Every view renders through these. CSS is generated from tokens using Textual
`$variable` references, so `App.theme = "qlab-light"` restyles the application
with no Python change.

Three containment tests:

1. **No hex literal outside `tokens.py`.** The repository already has this
   property — all 42 current hex values are in `qlab/tui/theme.py` — so the
   test preserves an existing invariant rather than repairing a violation.
2. **No inline colour markup under `qlab/tui/`.** Current inline markup is
   eight `[bold]` and `[dim]` uses and no hex, so this is enforceable
   immediately.
3. **Alignment invariants.** `field()` pads the label to 14 and starts the
   value at column 15 at every width and in every theme.

## State and failure rendering

The architecture document records that stream failures are invisible, that a
stop control can display its desired end state, and that no status may be
inferred from model prose. Each becomes a defined rendering.

- **Connection state** is always present at the right of the command row:
  `LIVE`, `RECONNECTING n` in `blocked`, or `OFFLINE` in `error` with
  inversion. Retry count and last error are reachable via `:why connection`.
- **Staleness is muted, never hidden.** When a projection slice exceeds twice
  the refresh interval its pane title takes the `stale` glyph and its values
  drop to `muted`. Hiding stale data misrepresents it; dimming it does not.
- **Optimistic control states are first-class**: `starting`, `stopping`,
  `resuming` each have a glyph. The interface never renders a control's desired
  end state, which is precisely the `stop` shown as `stopped` defect.
- **Approvals are visually quarantined**: a full-width rule, their own heading,
  and the only region in which confirmation styling may appear. Nothing that
  implies execution renders adjacent to a research result.
- **Every rendered status resolves from a protocol field**, never from
  coordinator prose. This is what makes the architecture document's success
  criterion checkable rather than aspirational.

## Amendment to the migration plan

The architecture document places visual work in Phase 6. For the design
*system* that sequencing is wrong. Phases 2 and 3 extract every widget, which
means touching every render site. Writing those widgets against primitives from
the start is nearly free; applying tokens in Phase 6 is a second full pass over
the same code.

**`qlab/tui/design/` lands at the start of Phase 2, before widget extraction.**
Each widget extracted in Phases 2 and 3 is written against the primitives.

Phase 6 retains what genuinely belongs there: linked symbol/workflow/decision
context, the evidence drawer, command history and completions, saved layout
preferences, and cross-platform PTY hardening.

This is a resequencing, not added scope.

## Test strategy

Extends, and does not replace, the architecture document's test strategy.

### Pure tests

- **Contrast**: every role against `bg` and `panel`, across all four themes,
  against the targets above. Currently zero failures.
- **Sign separation**: the `up`/`down` luminance ratio is recorded per theme and
  the dark themes assert a floor of 1.8:1, so a palette edit cannot quietly
  regress below what the current theme already achieves. No useful floor exists
  for the light themes, so the test asserts the redundant encoding instead:
  `num(..., signed=True)` must always emit an explicit `+` or `-` character,
  independent of theme.
- **Glyph parity**: the Unicode and ASCII tables have identical keys and equal
  display widths.
- **Primitives**: `field()` alignment invariants, `num()` formatting and
  right-alignment, `absent()` returning three distinguishable renderings.
- **Containment lint**: no hex outside `tokens.py`, no inline colour markup
  under `qlab/tui/`.

### Pilot visual fixtures

Four breakpoints times two themes times nine views is 72 snapshots, which is
more than can be maintained and would decay into noise. The covering subset is:

- all nine views at one reference combination (`wide`, `qlab-dark`);
- three representative views — workforce, book, market — at all eight
  breakpoint/theme combinations.

That is approximately 33 fixtures, and it covers both layout collapse and
theme inversion. The reduction is deliberate and recorded here so the coverage
gap is explicit rather than implied.

### Real terminal

Adds to the architecture document's PTY set: theme switch during streaming
output, and ASCII glyph fallback on a code page without the Unicode forms.

## Sizing

Against the architecture document's four-to-seven week estimate:

| Item | Delta |
|---|---|
| design tokens and primitives | +2–3 days, largely offset by the Phase 6 retrofit it removes |
| dual-theme contrast and fixture testing | +2–3 days, genuinely new, attributable to choosing two themes over one |

The total remains four to seven weeks, sitting in the upper half of that range.

## Follow-on items

- The default theme is `qlab-dark`; whether the operator's choice persists
  across sessions belongs to the Phase 6 saved-layout work, not here.
- The CVD variants ship with the initial token set. Exposing them in the
  settings view, rather than only via `:theme`, is Phase 6.
- The web client in `qlab/ui/index.html` continues to use its own styling. If
  the token set is later shared with it, that is a separate piece of work and
  requires its own contrast validation against browser rendering.
