# Installing qlab

Everything needed to get the desk running: Python, the Rust
toolchain for the Atlas workstation, and the optional Alpaca CLI for real data
and a real paper book.

qlab has two moving parts: the **Python desk** (the owner runtime, the quant
core, the `qlab` CLI) and the **Atlas workstation** (a Rust/Ratatui terminal
client). The whole offline demo runs on the Python side alone; the Rust client
is what `qlab tui` draws. You need **Python 3.10+** and, for the workstation, a
**Rust toolchain**. Real prices and a real paper book additionally need the
**Alpaca CLI** (or API keys). Everything below is one-time.

> **Python: the package floor and the suite floor differ.** The package
> installs and runs on **3.10+**, which is what `requires-python` declares. The
> *test suite* wants **3.13**, which is what CI runs: one assertion pins a JSON
> error message only CPython 3.13 words that way. On 3.10–3.12 qlab works and
> `python -m pytest` shows a failure that is about your interpreter, not your
> install. Use 3.13 if you intend to run the suite.

**On Windows, choose a lane before you start.** Everything here works natively
in PowerShell, and the Windows blocks below are written for it. The alternative
is **WSL2**, where you follow the *Linux* instructions throughout and get the
same desk — no MSVC toolchain, and the environment CI actually builds on.
Section 0 sets up either.

### 0 · Get the code

qlab is a public repository, so cloning needs no account or token:

```bash
git clone https://github.com/AzAINN/QLAB && cd QLAB
```

No git installed? The repository's **Code ▸ Download ZIP** button gives the same
tree — but `git clone` is worth the install, since `git pull` is how you take
updates and the ZIP has no history to update from.

Everything after this point runs from that directory.

<details>
<summary><b>Windows via WSL2</b> — install once, then follow every Linux block</summary>

WSL2 runs a real Ubuntu kernel alongside Windows. It is the shortest path on
Windows because it skips the MSVC toolchain entirely and matches CI.

1. **Install WSL** in PowerShell **as Administrator**, then reboot:
   ```powershell
   wsl --install
   ```
   That enables the feature and installs Ubuntu by default. On an older Windows
   10 build, run `wsl --update` first. Afterwards launch **Ubuntu** from the
   Start menu and create your Linux username and password (it is not your
   Windows one).

2. **Clone into the Linux filesystem, not `/mnt/c/`.** This is the mistake worth
   avoiding up front: the Windows drives are mounted over a network protocol, so
   a `cargo build` or a pytest run under `/mnt/c/...` is several times slower and
   file-watching misbehaves. Keep the repo in your Linux home:
   ```bash
   cd ~ && git clone https://github.com/AzAINN/QLAB && cd QLAB
   ```
   You can still reach it from Windows at `\\wsl$\Ubuntu\home\<user>\QLAB`, and
   VS Code opens it directly with the **WSL** extension.

3. **Python.** Ubuntu ships Python but not the venv module or pip:
   ```bash
   sudo apt update && sudo apt install -y python3-venv python3-pip
   ```
   If `python3 -V` is below 3.13 and you want the suite green, add the deadsnakes
   PPA (`sudo add-apt-repository ppa:deadsnakes/ppa`) and install
   `python3.13-venv`, then build the venv with `python3.13 -m venv .venv`.

4. **Follow the Linux blocks** from here — section 1 for Python and qlab, the
   Linux toolchain block in section 2 for Rust, and section 3's prebuilt-binary
   or Go route for the Alpaca CLI (Homebrew on Linux works too).

Two WSL-specific notes for later:

- **The browser login works**, but WSL has no browser of its own. Install
  `wslu` (`sudo apt install -y wslu`) so `alpaca profile login` can hand the
  OAuth URL to your Windows browser; otherwise copy the printed URL across by
  hand.
- **The owner's port is reachable from Windows.** The desk listens on
  `127.0.0.1:8765` inside WSL, and WSL2 forwards localhost, so
  `http://localhost:8765` works from a Windows browser. Run the terminal client
  inside the WSL shell — Windows Terminal renders it correctly.

</details>

### 1 · Python and qlab

A virtual environment is recommended:

```bash
python -m venv .venv
# Windows (PowerShell):   .venv\Scripts\Activate.ps1
# macOS / Linux:          source .venv/bin/activate
```

Install qlab in editable mode. The core is deliberately light and everything
heavy is an optional extra, so pick the extras you actually need:

```bash
python -m pip install -e ".[operator]"                        # minimum for the `qlab` desk
python -m pip install -e ".[operator,data,trader]"            # + live data + your Alpaca paper book
python -m pip install -e ".[operator,data,optimize,mcp,dev]"  # the dev setup (tests + convex solver)
python -m pip install -e ".[all]"                             # data, hmm, optimize, mcp, trader, viz, tui, dev
```

| Extra | Pulls in | Needed for |
|---|---|---|
| `operator` | httpx, fastmcp | the `qlab` desk, the CLI verbs, the Claude MCP proxy |
| `data` | yfinance, pyarrow | live/cached daily bars (the default lane) |
| `trader` | alpaca-py | the Alpaca paper book (`--alpaca-book`) and Alpaca market data |
| `optimize` | cvxpy | the faster convex solver path |
| `hmm` | hmmlearn | the Gaussian-HMM regime posterior (the deterministic ensemble runs without it) |
| `news` | feedparser | the RSS, EDGAR, macro and GDELT providers (stdlib; feedparser only for RSS) |
| `mcp` | fastmcp | headless MCP sessions |
| `viz` | matplotlib | the scaling / ablation charts |
| `dev` | pytest | the test suite |
| `offline-quantum` | qiskit\* | the isolated offline quantum lane (excluded from `all`) |

Confirm the core is live — no network, no accounts:

```bash
python -m pytest             # full offline suite
qlab recommend --offline     # print an allocation from synthetic data, no trade
```

### 2 · Rust toolchain (for the Atlas workstation)

`qlab tui` draws the Rust client in `clients/atlas-tui` and **refuses rather
than falling back** if the binary is not built. Install a recent stable Rust
with `rustup`, plus your platform's C linker / build tools.

<details>
<summary><b>Windows</b> — MSVC build tools (or MSYS2 / GNU) + rustup</summary>

Rust on Windows needs a C linker and the system libraries to link against. Pick
**one** of the two toolchains below — MSVC is the default and best-supported;
MSYS2/GNU is the option if you would rather not install Visual Studio.

*Third option:* the **WSL2** route in section 0 avoids this section entirely —
inside WSL you install the Linux toolchain below instead, and build
`atlas` rather than `atlas.exe`.

**Option A — MSVC (default, recommended).** Rust's default `stable-msvc`
toolchain uses the Microsoft C++ build tools: the **MSVC v143 compiler/linker**
(`link.exe`) and a **Windows 10/11 SDK**. Both ship in the *Desktop development
with C++* workload.

1. **C++ build tools.** Install *Build Tools for Visual Studio 2022* from
   <https://visualstudio.microsoft.com/downloads/> (under "Tools for Visual
   Studio") and select the **Desktop development with C++** workload — or via
   winget:
   ```powershell
   winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
   ```
   `--includeRecommended` pulls in the matching MSVC toolset and Windows SDK. A
   Build Tools install *without* the C++ workload has no linker, and `cargo
   build` then fails with `error: linker 'link.exe' not found`.
2. **Rust.** Download and run **`rustup-init.exe`** from <https://rustup.rs> and
   accept the default toolchain (if the build tools are missing it offers to
   install them for you) — or via winget:
   ```powershell
   winget install --id Rustlang.Rustup
   ```
   Open a **new** terminal so `cargo` is on `PATH`, then check `cargo --version`.

**Option B — MSYS2 / GNU (no Visual Studio).** Use MinGW-w64 GCC from MSYS2 with
Rust's `x86_64-pc-windows-gnu` target instead of the MSVC toolchain.

1. Install MSYS2 from <https://www.msys2.org> (or `winget install --id MSYS2.MSYS2`).
2. Open the **MSYS2 UCRT64** shell and install the toolchain with `pacman`:
   ```bash
   pacman -Syu                                          # update once, then reopen the shell
   pacman -S --needed mingw-w64-ucrt-x86_64-toolchain   # gcc, ld, make, ar, …
   ```
   Add its `bin` directory to your Windows `PATH`: `C:\msys64\ucrt64\bin`.
   (Prefer the classic environment? Use the **MINGW64** shell and
   `mingw-w64-x86_64-toolchain`, on `PATH` at `C:\msys64\mingw64\bin`.)
3. Install rustup (as in Option A step 2), then make the GNU toolchain the
   default:
   ```powershell
   rustup default stable-x86_64-pc-windows-gnu
   cargo --version
   ```

</details>

<details>
<summary><b>macOS</b> — Command Line Tools + rustup</summary>

```bash
xcode-select --install                                          # the clang linker (once)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh  # Rust via rustup
source "$HOME/.cargo/env"                                       # or open a new terminal
cargo --version
```

Homebrew alternative for Rust: `brew install rustup-init && rustup-init`.

</details>

<details>
<summary><b>Linux</b> — C toolchain + OpenSSL headers + rustup</summary>

Install a C toolchain and the headers the client's HTTP stack builds against
(`pkg-config`, OpenSSL), then Rust:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y build-essential pkg-config libssl-dev curl
# Fedora / RHEL:  sudo dnf install -y gcc gcc-c++ make pkg-config openssl-devel curl
# Arch:           sudo pacman -S --needed base-devel pkg-config openssl curl

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
cargo --version
```

</details>

Then build the workstation once (from the repo root):

```bash
cd clients/atlas-tui && cargo build --release && cd -
```

The launcher looks for the binary at `clients/atlas-tui/target/release/atlas`
(`atlas.exe` on Windows); `$QLAB_ATLAS_BIN` overrides where. `cargo test` inside
that directory runs fully offline and needs no owner.

### 3 · Alpaca CLI (optional — real data and a real paper book)

The desk opens on synthetic data with a simulated book and needs no account. To
read real prices or trade your **Alpaca paper** account, install the Alpaca CLI
and sign in. Everything stays paper-only — the OAuth login cannot reach a live
venue.

<details>
<summary><b>macOS / Linux</b> — Homebrew</summary>

```bash
brew install alpacahq/tap/cli
```

</details>

<details>
<summary><b>Windows</b> (and any OS) — prebuilt binary</summary>

Download the latest release from <https://github.com/alpacahq/cli/releases> and
put `alpaca` on your `PATH`:

- **Windows:** `cli_<version>_windows_amd64.zip` (or `_arm64`) → unzip → move
  `alpaca.exe` onto `PATH`.
- **Linux:** `cli_<version>_linux_amd64.tar.gz` → `tar xzf …` →
  `sudo mv alpaca /usr/local/bin/`.
- **macOS:** `cli_<version>_darwin_arm64.tar.gz` (Apple Silicon) or `_amd64`
  (Intel).

</details>

<details>
<summary><b>Any OS with a Go toolchain</b></summary>

```bash
go install github.com/alpacahq/cli/cmd/alpaca@latest
```

</details>

Then confirm the install and sign in:

```bash
alpaca version
alpaca doctor          # checks the install and any stored profile
alpaca profile login   # browser OAuth, paper-only; writes ~/.config/alpaca/profiles/
```

qlab reads the active profile from `~/.config/alpaca` when no env credentials
are set (`ALPACA_CONFIG_DIR` overrides the location). Prefer keys? Export them
instead — mind the name difference: the CLI stores the secret as
`ALPACA_SECRET_KEY`, qlab reads `ALPACA_API_SECRET` (the same value).

```bash
export ALPACA_API_KEY=...
export ALPACA_API_SECRET=...     # the value the CLI calls ALPACA_SECRET_KEY
```

→ [data lanes and whose book](data-and-book.md) · [news setup](news-setup.md)
