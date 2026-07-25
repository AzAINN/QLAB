# Alpaca OAuth login and the explicit desk mode — design

Operators should be able to run qlab against real Alpaca paper data without
ever handling an API key, and should choose — visibly, at startup — whether the
desk is synthetic, live-on-a-simulated-book, or live-on-their-Alpaca-book.

## Why

Today qlab reaches Alpaca only through `ALPACA_API_KEY` / `ALPACA_API_SECRET`,
copied by hand from the web dashboard. Two problems:

1. **Key handling is the wrong ask.** The operator already authenticates with
   `alpaca profile login`, the official CLI's browser OAuth flow. That flow is
   **paper-only by construction** — live trading in that CLI requires the
   separate `--api-key --live` path — so consuming it is *safer* than pasting
   keys, not merely easier.
2. **Credential presence silently decides the venue.** `get_broker` returns the
   Alpaca broker whenever both env vars exist. Once a CLI token is discoverable,
   "just look at synthetic data for a minute" would route to the real paper
   account without anyone choosing that. Mode must be a decision, not a
   side effect of what happens to be on disk.

### Verified facts this design rests on

Probed against the operator's live account before writing this:

- `~/.config/alpaca/profiles/paper.yaml` (mode `600`) holds
  `api_key: ""`, `secret_key: ""`, `access_token` (36 chars),
  `scopes: "account:write trading data"`.
- `~/.config/alpaca/config.yaml` holds `default_profile: paper`.
- That OAuth token authenticates **both** lanes: `TradingClient(oauth_token=…,
  paper=True).get_account()` returned `ACTIVE`, equity `100000`, account number
  prefixed `PA` (paper); `StockHistoricalDataClient(oauth_token=…)` returned 8
  daily `ACWI` bars.
- Both `TradingClient.__init__` and `StockHistoricalDataClient.__init__` accept
  `oauth_token` (alpaca-py 0.43.5, already installed).
- The CLI names its secret env var `ALPACA_SECRET_KEY`; qlab uses
  `ALPACA_API_SECRET`. Different names for the same thing — a real trap.
- Nothing in the repo loads `.env` (no `dotenv` dependency), despite
  `.env.example` telling operators to copy it. Out of scope here, but it is why
  this design never relies on `.env`.

## The three reachable states

```
┌─ step 1 ─────────────────────────┐   ┌─ step 2 (only when LIVE) ────────────┐
│  DATA SOURCE                     │   │  WHICH BOOK                          │
│  ▸ SYNTHETIC   offline, det.     │──▶│  ▸ SIMULATED   qlab's own book       │
│    LIVE        Alpaca real bars  │   │    ALPACA      your paper account    │
└──────────────────────────────────┘   └──────────────────────────────────────┘
         │
         └─▶ book forced to SIMULATED
```

| data | book | meaning |
|---|---|---|
| `synthetic` | `simulated` | the offline demo; deterministic, no network |
| `live` | `simulated` | real prices, qlab's own book at mandate capital — strategy work that never touches the Alpaca account |
| `live` | `alpaca` | the full live paper desk: real prices, real paper positions and fills |

`synthetic` + `alpaca` is unreachable by construction: step 2 only appears after
LIVE is chosen. Progressive disclosure, not a validation rule to enforce.

**The middle state earns its place.** Live prices on a simulated book gives a
controlled starting capital from `mandate.yaml` and a book the operator can
reset freely, without mutating their Alpaca account or its history.

## Components

### 1. `qlab/trader/alpaca_auth.py` (new)

One module, no network, whose only job is answering *what Alpaca credential do
we have and where did it come from*.

```python
@dataclass(frozen=True)
class AlpacaCredentials:
    kind: Literal["api_key", "oauth"]
    api_key: str | None
    secret_key: str | None
    oauth_token: str | None
    profile_name: str | None      # None for env-sourced credentials
    source: str                   # "env" or the profile path, for display

def resolve_alpaca_credentials() -> AlpacaCredentials | None
def describe_credentials() -> str          # operator-facing, never the secret
```

Resolution order — **env wins**, so nothing changes for anyone already using
keys:

1. `ALPACA_API_KEY` + `ALPACA_API_SECRET` → `kind="api_key"`. Exactly one set
   stays the existing loud refusal.
2. Otherwise the active CLI profile: config dir from `ALPACA_CONFIG_DIR` else
   `~/.config/alpaca`; profile name from `ALPACA_PROFILE` else `config.yaml`'s
   `default_profile` else `"paper"`. A profile with a non-empty `api_key` and
   `secret_key` yields `kind="api_key"`; one with `access_token` yields
   `kind="oauth"`.
3. Nothing found → `None`.

Rules: the token is never logged, echoed, or included in a `repr`; a malformed
profile raises with the offending path rather than returning `None` (a parse
error is not the same as "not logged in"); a profile that declares itself live
(`live: true`, or a `--live`-created profile) is **refused** — this adapter has
no live path and must not appear to have one.

### 2. OAuth reaches the SDK clients

`AlpacaPaperBroker.__init__` and the data client construction switch on
`credentials.kind`, passing either `oauth_token=` or `api_key=/secret_key=`.
`paper=True` stays hard-coded and un-parameterised (invariant 3). The existing
"credentials set but broker failed to build" loud refusal is preserved.

### 3. Desk mode becomes explicit

New `qlab/core/desk_mode.py`:

```python
@dataclass(frozen=True)
class DeskMode:
    data: Literal["synthetic", "live"]
    book: Literal["simulated", "alpaca"]

    def __post_init__(self):   # synthetic data can only have a simulated book
        ...

DEFAULT = DeskMode("synthetic", "simulated")

def load_desk_mode() -> DeskMode | None      # state_path("desk_mode.json")
def save_desk_mode(mode: DeskMode) -> None
```

`get_broker` gains an explicit `book` parameter. **This is a deliberate
behaviour change**: credential presence no longer selects the venue.

- `book="alpaca"` → Alpaca broker, or a loud refusal (never a silent downgrade).
- `book="simulated"` → the simulator **even when a valid token exists**.
- The parameter defaults to the current inferred behaviour so existing callers
  (autopilot, MCP server, scheduler) keep working until each is passed a mode;
  the plan updates the owner runtime explicitly and leaves the rest on the
  default.

Data side: `data="live"` resolves to `DataPolicy.alpaca_operational(feed)` —
the existing no-fallback, execution-eligible policy — with `ALPACA_FEED`
defaulting to `iex` (the free tier; `sip` needs a paid subscription and will
refuse rather than degrade). `data="synthetic"` is today's `DataPolicy.demo`.

### 4. The startup modal

`DeskModeScreen(ModalScreen[DeskMode])` in a new `qlab/tui/desk_mode_screen.py`,
following `PaperConfirmScreen`'s shape (its own `CSS`, `Binding("escape", …)`,
`dismiss(value)`). Two steps in one screen: choosing LIVE reveals the book row;
choosing SYNTHETIC hides it and forces `simulated`.

Credential probing (an account call) runs in a Textual worker — the same
pattern as `_start_atlas_fetch` — so a slow or hanging network call can never
freeze the modal. Until it resolves, LIVE shows `checking…`; on success it shows
the profile name, masked account number and equity; on failure it shows the
reason and the remedy.

Shown when: no persisted mode exists, or the persisted mode is `live` and the
credential no longer validates. Otherwise startup is silent.

### 5. Flags and persistence

`qlab tui` / `qlab ui` gain `--live` and `--alpaca-book` (the latter implies
`--live`). Any explicit flag skips the modal, keeping `qlab ui` and scripted
runs headless. Precedence: flags → persisted `desk_mode.json` → modal →
`DEFAULT`. The safe choice is the default: bare `--live` keeps the simulated
book, so reaching the real paper account always takes an extra explicit word.

The TUI already sends `offline` per request and the owner resolves it per
request (`_qbool(query, "offline", session.offline_default)`), so switching the
data lane needs no owner restart. The book lane is resolved inside
`UISession.portfolio()` per call, so it is equally dynamic.

### 6. The status strip shows both, always

`LIVE DATA · ALPACA BOOK` / `LIVE DATA · SIM BOOK` / `SYNTHETIC`, alongside the
existing connection chip. This is load-bearing, not decoration: "real prices,
simulated book" is precisely the state an operator could misread as real P&L,
and the whole desk's honesty discipline depends on never letting that happen.

## Error handling

Fail loud, never degrade silently (invariant 4). Every refusal names the
remedy:

| situation | behaviour |
|---|---|
| LIVE chosen, no credential | refuse, naming `alpaca profile login` |
| token expired or rejected | refuse, naming `alpaca profile login` to re-authenticate |
| exactly one env var set | existing loud refusal, unchanged |
| profile file malformed | raise, naming the path |
| profile declares live | refuse: this adapter is paper-only |
| `book=alpaca` but broker won't build | existing loud refusal, no downgrade |

A failed probe never rewrites a persisted mode to something safer-looking; it
surfaces and lets the operator choose.

## How this composes with `equity_marks.book`

Switching between the simulated and Alpaca book is exactly the
two-books-in-one-series hazard that mark stamping was added for. Because every
mark already carries its book and `performance()` scopes to the current one
while disclosing exclusions, the equity curve will not splice two unrelated
equity levels into a fabricated return. No further work needed — but the plan
adds a test pinning that mode switching produces scoped series rather than a
spliced one, because this feature makes that path routine rather than
theoretical.

## Testing

Everything offline; no test touches the network or `.lab/registry.duckdb`.

- **Resolver** against a temp `ALPACA_CONFIG_DIR`: env precedence over profile;
  OAuth profile; api-key profile; `ALPACA_PROFILE` override; `default_profile`
  honoured; missing profile → `None`; malformed YAML → raises with the path;
  live-declaring profile → refuses; and that neither the token nor the secret
  appears in `repr`, `str`, or `describe_credentials()`.
- **DeskMode**: `synthetic` + `alpaca` rejected at construction; JSON round
  trip; an unreadable or unknown-valued state file falls back to `DEFAULT`
  rather than crashing the desk.
- **Broker selection**: `book="simulated"` returns the simulator *with a valid
  token present* (the regression this design exists to prevent);
  `book="alpaca"` builds the Alpaca client with `oauth_token` when the profile
  is OAuth and with key/secret when it is not — SDK monkeypatched, no network.
- **Modal** via pilot with a stubbed resolver: SYNTHETIC hides the book row and
  yields `("synthetic", "simulated")`; LIVE reveals it; a failing probe renders
  the remedy text and does not return a live mode; the worker pattern means the
  modal stays responsive while probing.
- **Status strip** renders all three states distinctly.
- **Integration** (opt-in, unchanged gate `QLAB_ALPACA_INTEGRATION=1`): the
  existing `tests/test_alpaca_integration.py` gains an OAuth-path case that
  builds the broker from the resolved profile and reads the account.

## Out of scope

`.env` auto-loading (real, separate, and now less necessary); the web client's
copy of this choice; live (non-paper) trading, which remains unimplemented by
design; and any change to execution gating — human confirmation for paper
execution is untouched.
