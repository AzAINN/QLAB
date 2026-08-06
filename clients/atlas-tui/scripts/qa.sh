#!/usr/bin/env bash
#
# The manual QA pass for the Atlas workstation, made reproducible.
#
# Drives the real binary through a pty and prints the screens it painted: the
# desk, each of the seven views, the command line, the help overlay, and how it
# left the terminal on the way out. Read the output. Nothing here asserts what
# is *on* a screen — the golden frames do that offline — so a PASS from this
# script means "it ran under a terminal and cleaned up after itself", which is
# precisely the half `cargo test` cannot reach.
#
#   usage:  scripts/qa.sh [--armed|--glass] [--size WxH] [--port N] [--debug]
#
#   --armed     capture the armed scenes. Arming is no longer something the
#               command line can grant — it is the owner's persisted answer to
#               the startup door — so this flag passes NOTHING to the binary.
#               It asserts a precondition (operator-feature build, armed desk)
#               and picks the door script that walks an armed window. Run it
#               against a desk that is actually armed, or the door scene will
#               show a read-only question and the capture is worthless.
#   --glass     pass --glass: this window declines authority for one session.
#   --operator  retired; refused loudly rather than silently mis-capturing.
#   --size      terminal size to emulate; default 120x40
#   --port      owner port; defaults to $QLAB_UI_PORT, then 8765
#   --debug     use target/debug/atlas instead of target/release/atlas
#
#   $ATLAS_BIN overrides binary resolution entirely.
#
# It runs with or without an owner, and says which. Both are real states an
# operator meets: the no-owner path is the NO OWNER RUNTIME panel, the refusal
# of every command that needs the desk, and the client staying alive and quittable
# while it cannot see anything — a surface with its own tests, exercised here
# against the real socket rather than a stubbed store.
#
# `tmux` is not installed on the machine this client was built on; this pty
# harness is the substitute, and it is in-repo rather than in a scratchpad so
# the next person can run the same pass.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

profile="release"
size="120x40"
port="${QLAB_UI_PORT:-8765}"
extra=""
armed=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --armed)    armed="yes"; shift ;;
    --glass)    extra="--glass"; shift ;;
    # A silent no-op would be the worst outcome here: the harness would keep
    # printing "armed" screens taken from whatever posture the desk happened to
    # hold. Refuse, and name what replaced it.
    --operator) echo "qa.sh: --operator is retired; arming is the desk's persisted answer to the startup door, not a flag." >&2
                echo "  --armed  capture the armed scenes against a desk that is already armed" >&2
                echo "  --glass  drive a window that declines authority for one session" >&2
                exit 2 ;;
    --debug)    profile="debug"; shift ;;
    --size)     size="$2"; shift 2 ;;
    --port)     port="$2"; shift 2 ;;
    -h|--help)  sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          echo "qa.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$armed" && -n "$extra" ]]; then
  # --glass is this window declining authority; --armed claims it has some.
  # Together they would capture a glass window under an "armed" heading.
  echo "qa.sh: --armed and --glass are opposite postures; pick one" >&2
  exit 2
fi

bin="${ATLAS_BIN:-$root/target/$profile/atlas}"
if [[ ! -x "$bin" ]]; then
  # Fail loud, and name the command that fixes it. A QA script that silently
  # built would hide which binary the screens below came from, which is the one
  # thing a QA pass has to be certain of.
  echo "qa.sh: no atlas binary at $bin" >&2
  echo "  build it first:  cd $root && cargo build --$profile${armed:+ --features operator}" >&2
  exit 1
fi

python="${PYTHON:-python3}"
capture="$here/qa_capture.py"

echo "== atlas QA =================================================="
echo "commit  $(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "built   $(date -r "$bin" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown)"
echo

# One process per scene rather than one long session. A scene that wedges then
# costs one screen instead of every screen after it, and each starts from the
# state an operator actually opens the client in.
scene() {
  local label="$1" script="$2" more="${3:-}"
  echo
  echo "############ $label"
  # `--args=` and never `--args "…"`: a value that starts with a dash and
  # holds no space is read as another flag, and argparse then refuses the
  # whole invocation. Every armed scene had been dying on that line — the
  # unarmed pass sends an empty string and never met it, so the QA pass this
  # script exists to be had run on one posture only.
  "$python" "$capture" "$bin" --port "$port" --size "$size" \
    --args="$extra${more:+ $more}" --script "$script" || return 1
}

failed=0

# The desk as it opens, after one poll has had time to land.
scene "boot · the desk" \
  "wait:3,shot:DESK,quit" || failed=1

# Every view the nav rail offers. Two polls' worth of settle before the first
# shot, then each digit in rail order — the seven-view sweep is what catches a
# view that renders only when it is the one the client opened on.
scene "the seven views" \
  "wait:3,shot:1 DESK,key:2,shot:2 MKTS,key:3,shot:3 BOOK,key:4,shot:4 RSCH,key:5,shot:5 WORK,key:6,shot:6 AUDIT,key:7,shot:7 SETT,quit" \
  || failed=1

# The command line: the picker, a scope with its trailing space, and the note
# it leaves behind when the desk cannot answer.
scene "the command line" \
  "wait:2,key:/,shot:the picker,key:view,shot:the scope,esc,shot:back to the desk,quit" \
  || failed=1

# The overlay that owns the keyboard, and the Esc that gives it back.
scene "the help overlay" \
  "wait:2,key:?,shot:the keys,esc,shot:the desk under it,quit" || failed=1

# The refresh key, which is the one keystroke that reaches the network.
scene "refresh" \
  "wait:2,key:r,wait:2,shot:after r,quit" || failed=1

# The startup door, which is the one scene that needs no owner at all: `--pick`
# asks whatever the desk says, and a machine with nothing listening still gets
# the question.
#
# Two scripts, because the two postures are two surfaces rather than one with a
# key greyed out. An armed window is walked — the data row, the row that moves
# on, then Esc out of the models — while a glass one is a statement, and the
# first key it is handed dismisses it.
if [[ -n "$armed" ]]; then
  door="wait:2,shot:the first question,down,down,enter,shot:the second question,esc,shot:the desk behind it,quit"
else
  door="wait:2,shot:the read-only door,key:x,shot:the desk behind it,quit"
fi
scene "the startup door (--pick)" "$door" "--pick" || failed=1

echo
if [[ $failed -eq 0 ]]; then
  echo "== every scene ran and exited clean =========================="
else
  echo "== A SCENE FAILED — see above ================================" >&2
fi
exit $failed
