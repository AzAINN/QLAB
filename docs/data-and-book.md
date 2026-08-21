# Data lanes, brokers, and state

Which prices the desk reads, whose book it trades, and where it writes. See the
[README](../README.md) for the short version and [news setup](news-setup.md) for the news lane.

## Two independent switches

The desk opens on offline synthetic data with a simulated book, so it runs with
no account at all. Two independent things can be switched on: the data lane and
the book.

    alpaca profile login      # browser OAuth; paper-only by construction
    qlab                      # online market data, qlab's own simulated book
    qlab tui --alpaca-book    # online market data and your Alpaca paper book

The live lane is the default — `--offline` is the synthetic demo — and which
provider serves it is `QLAB_DATA_PROVIDER`, and that defaults to `yfinance`.
Alpaca market data is a separate, additional choice: it needs
`QLAB_DATA_PROVIDER=alpaca` **and** exported `ALPACA_API_KEY` /
`ALPACA_API_SECRET`, because the daily-bar provider reads those environment
variables directly. The `alpaca profile login` session reaches the **book**
lane only — with an OAuth-only login, `--alpaca-book` trades your real Alpaca
paper account while prices still come from yfinance.

`--alpaca-book` implies live data: reaching the real paper account is never a
side effect of asking for real prices. The same three modes are offered by the
startup modal on first launch — the flags only skip the question — and the
choice persists, so later launches reopen the desk in the mode you left it in.
The desk-mode chip in the command row names the mode currently in force.

Every mode is paper-only; there is no live-trading path to select, and the
browser login cannot grant one (the Alpaca CLI puts live behind its separate
`--api-key --live` flow). That login is preferred over `ALPACA_API_KEY` /
`ALPACA_API_SECRET` because it leaves no secret to paste or store. If you use
keys anyway, either export them or put them in `.env`, which the CLI loads at
startup — an already-exported variable outranks the file, and a blank entry in
the file is ignored rather than treated as empty credentials. Note that qlab's
`ALPACA_API_SECRET` is spelled `ALPACA_SECRET_KEY` by the Alpaca CLI.

## Data and broker limits

The current operator surface is research and paper-first:

- Online mode uses cached, adjusted daily bars from `QLAB_DATA_PROVIDER`;
  `yfinance` is the default and `alpaca` is optional.
- Offline mode uses cache or deterministic synthetic fixtures.
- Market provenance records the producing provider (`yfinance`, `alpaca`, or
  `synthetic`), and the as-of date and bar age are shown to the operator.
- Alpaca support requires the trader extra plus one credential source: an
  `alpaca profile login` session, or `ALPACA_API_KEY` and `ALPACA_API_SECRET`
  either exported or set in `.env`, which the CLI loads at startup. The browser
  login
  currently reaches the **broker** only — the `alpaca` daily-bar provider reads
  the two environment variables directly, so Alpaca market data still needs
  exported keys. It remains paper-only and daily-bar-only: there is no
  streaming quote tape or complete order-lifecycle integration.
- Selecting Alpaca without its package or credentials fails loudly; qlab does
  not silently switch the request back to yfinance.
- The simulated broker remains the zero-account default.

News follows the data lane: on a live desk with a resolvable Alpaca credential
it uses Alpaca automatically, with nothing to configure.
[news setup](news-setup.md) covers the three providers, the
`ALPACA_SECRET_KEY` / `ALPACA_API_SECRET` naming trap, and how to tell a news
outage from a genuinely quiet market — the desk never conflates them.

## Configuration and state

Editable checkout configuration:

- configs/universe.yaml — core and candidate universes.
- configs/specs/ablation_v1.yaml — staged experiment matrix.
- mandate.yaml — deterministic paper-trading limits and operational policy.
- agents/ — neutral role definitions.

Optional path overrides:

| Variable | Purpose |
|---|---|
| QLAB_WORKSPACE | Project-local output and adapter root |
| QLAB_STATE_DIR | Registry, cache, artifacts, and summaries |
| QLAB_CONFIG_ROOT | Alternate mandate/config/agent bundle |
| QLAB_UI_PORT | Owner-runtime guard port |
| QLAB_OFFLINE | Default MCP data mode |
| QLAB_DATA_PROVIDER | Online daily-bar provider (`yfinance` or `alpaca`) |
| QLAB_NEWS_PROVIDER | Force a news provider (`alpaca`, `rss`, `synthetic`) |
| QLAB_ATLAS_AUTONOMOUS | `0` makes Atlas queue work instead of starting it |
| QLAB_ATLAS_DRIVE | `0` dispatches workflows without driving a coordinator |
| QLAB_LLM_FAST | `1` runs judgment roles on the quick model (referee exempt) |

An installed wheel defaults its writable state to .lab under the current
workspace, not the Python environment.

