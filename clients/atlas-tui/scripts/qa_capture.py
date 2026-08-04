#!/usr/bin/env python3
"""Drive the atlas binary through a pty and print the screens it painted.

This is the reproducible half of the manual QA pass. `tmux` is not installed on
the development machine this client was built on, so the substitute is a pty of
a fixed size plus a small VT parser: the binary runs exactly as it does under a
terminal — alternate screen, raw mode, resize and all — and what comes back is
reconstructed into a grid rather than grepped as an escape stream.

Reconstruction matters. Ratatui writes only the cells that *changed* since the
last frame, so a run of text on screen arrives split at every column where the
frame underneath already agreed. `frame.contains("RESEARCH")` against the raw
stream is a check that passes or fails on how the previous frame happened to
look; against the grid it is a check about what an operator can read.

Not a test. It is the thing a human looks at before believing the tests, and the
one surface that exercises the real terminal path the golden frames cannot.

Usage
-----
    qa_capture.py BINARY [--port N] [--size WxH] [--script STEP,STEP,...]

Steps, comma-separated, run in order:

    wait:SECONDS    let the client paint (the poll is 3 s; the first frame is
                    immediate)
    key:CHARS       send each character as a keystroke
    esc             send Escape on its own — never fold this into `key`, see
                    below
    shot:LABEL      mark the stream here; the screen is reconstructed and
                    printed under that heading after the client exits
    quit            send `q`, wait for exit, and report how it left the terminal

`esc` is its own step for a reason recorded in Task 20: ESC immediately followed
by a printable byte in a single write is an escape *sequence* to any input
parser, so the two have to reach the client separately or the harness is testing
crossterm's disambiguation rather than this client's routing.

Exit status is 0 when the client exited cleanly and left the terminal restored,
1 otherwise. Nothing here asserts what is *on* the screens — that is the human's
job, and the golden frames' — so a passing run means "it ran and cleaned up",
not "it drew the right thing".

Known limitation, stated because a QA tool that lies quietly is worse than none
-------------------------------------------------------------------------------
Against a *busy* owner — live quote stream, effects firing, several shots a
second — a single character is occasionally missing from a word on a
reconstructed screen, in a different place on every run. It is a read-path
artifact of this harness, not the client: replaying a captured byte stream
through the same parser reconstructs those words perfectly, every time, and the
golden frames pin the same panes offline. Three designs have narrowed it (parse
off the reader thread, then off the main thread too, then capture-then-replay,
which is what runs now) and it is down from most runs to roughly one word in
five runs.

So: read a screen for layout, keys, refusals and the exit path. Do **not** read
it as a character-exact pin — that is what `cargo test`'s golden frames are for,
and they are exact by construction.
"""

from __future__ import annotations

import argparse
import codecs
import os

if os.name == "nt":
    raise SystemExit(
        "qa_capture.py requires a POSIX pseudo-terminal (pty/fcntl/termios); "
        "run the cargo golden tests on Windows and this manual QA capture on "
        "macOS or Linux."
    )

import fcntl
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time



class Screen:
    """A character grid with just enough VT to follow what ratatui emits.

    Deliberately small: cursor addressing, the two erases, and printable runs.
    Colour is dropped — a reconstructed screen answers "what does this say",
    and what it looks like is what the golden frames and the theme tests pin.
    """

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.cells = [[" "] * width for _ in range(height)]
        self.row = self.col = 0
        self.alternate = False
        self.cursor_visible = True
        # An escape sequence split across two reads. Without this the tail of a
        # `\x1b[38;2;…m` lands in the next chunk with no `\x1b` in front of it
        # and is painted as text — `;8;8;8m` turns up in the middle of a pane,
        # which is a colour the terminal never showed anybody.
        self.tail = ""

    def text(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.cells)

    def put(self, ch: str) -> None:
        if self.col >= self.w:
            self.col = 0
            self.row += 1
        if 0 <= self.row < self.h and 0 <= self.col < self.w:
            self.cells[self.row][self.col] = ch
        self.col += 1

    def erase_display(self, mode: int) -> None:
        if mode == 2:
            self.cells = [[" "] * self.w for _ in range(self.h)]
        elif mode == 0:
            self.erase_line(0)
            for r in range(self.row + 1, self.h):
                self.cells[r] = [" "] * self.w
        elif mode == 1:
            for r in range(0, self.row):
                self.cells[r] = [" "] * self.w
            self.erase_line(1)

    def erase_line(self, mode: int) -> None:
        if not (0 <= self.row < self.h):
            return
        if mode == 0:
            for c in range(self.col, self.w):
                self.cells[self.row][c] = " "
        elif mode == 1:
            for c in range(0, min(self.col + 1, self.w)):
                self.cells[self.row][c] = " "
        else:
            self.cells[self.row] = [" "] * self.w

    def feed(self, data: str) -> None:
        data, self.tail = self.tail + data, ""
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            if ch == "\x1b":
                nxt = self._escape(data, i)
                if nxt < 0:
                    self.tail = data[i:]
                    return
                i = nxt
                continue
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self.row += 1
                if self.row >= self.h:
                    # Scroll rather than grow: a client in the alternate screen
                    # should never do this, and silently growing the grid would
                    # hide it if one did.
                    self.cells.pop(0)
                    self.cells.append([" "] * self.w)
                    self.row = self.h - 1
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch == "\t":
                self.col = min(self.w - 1, (self.col // 8 + 1) * 8)
            elif ch >= " ":
                self.put(ch)
            i += 1

    def _escape(self, data: str, i: int) -> int:
        """Consume one escape sequence at `i`.

        Returns the next index, or -1 when the sequence is cut off by the end of
        this chunk and has to be carried into the next read.
        """
        if i + 1 >= len(data):
            return -1
        kind = data[i + 1]
        if kind == "]":  # OSC — runs to BEL or ST
            end = i + 2
            while end < len(data) and data[end] not in ("\x07", "\x1b"):
                end += 1
            if end >= len(data):
                return -1
            if data[end] == "\x1b":
                end += 1
            return end + 1
        if kind != "[":  # a two-character escape (charset selection, RIS, …)
            return i + 2

        end = i + 2
        while end < len(data) and not ("@" <= data[end] <= "~"):
            end += 1
        if end >= len(data):
            return -1
        body, final = data[i + 2 : end], data[end]
        private = body.startswith("?")
        raw = body[1:] if private else body
        args = [int(p) if p.isdigit() else 0 for p in raw.split(";")] or [0]

        if private:
            if 1049 in args:
                self.alternate = final == "h"
            if 25 in args:
                self.cursor_visible = final == "h"
        elif final in "Hf":
            # Both parameters default to 1, and a bare `CSI H` is home.
            row = args[0] if args and args[0] else 1
            col = args[1] if len(args) > 1 and args[1] else 1
            self.row = min(self.h - 1, max(0, row - 1))
            self.col = min(self.w - 1, max(0, col - 1))
        elif final == "A":
            self.row = max(0, self.row - max(1, args[0]))
        elif final == "B":
            self.row = min(self.h - 1, self.row + max(1, args[0]))
        elif final == "C":
            self.col = min(self.w - 1, self.col + max(1, args[0]))
        elif final == "D":
            self.col = max(0, self.col - max(1, args[0]))
        elif final == "J":
            self.erase_display(args[0])
        elif final == "K":
            self.erase_line(args[0])
        # `m` (colour) and everything else is deliberately ignored.
        return end + 1


def owner_is_up(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def run(
    binary: str,
    port: int,
    size: tuple[int, int],
    script: list[str],
    extra: list[str],
) -> int:
    width, height = size
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    env = dict(os.environ, QLAB_UI_PORT=str(port), TERM="xterm-256color")
    # Truecolor is *not* forced: which palette this terminal gets is one of the
    # things a QA pass is looking at, and forcing it here would hide the
    # 256-colour ramp the fallback exists for.
    child = subprocess.Popen(
        [binary, *extra],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    screen = Screen(width, height)
    # An incremental decoder rather than a decode-and-retry: a multi-byte glyph
    # split across two reads has to be *held*, not guessed at, and every panel
    # header and every braille row on this client is multi-byte. Hand-rolling
    # the boundary lost a character often enough to be noticed and rarely enough
    # to be blamed on the client.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    # While the client is running, this process reads and does nothing else.
    #
    # Both softer versions of that lost characters — one per word, somewhere
    # different on every run (`vol_pred ction_ridge`, `work low abandoned`).
    # Parsing inside the reader was the first; parsing on the main thread while
    # the reader ran was the second, because printing a screen holds the GIL in
    # runs long enough to starve the reader against a busy desk. Replaying a
    # stream that *was* captured by a do-nothing-but-read loop reconstructs
    # every one of those words perfectly, which is what pinned the loss to the
    # read path rather than to the grammar.
    #
    # So a shot is a *mark* — a byte offset into the stream — and the screens
    # are reconstructed and printed after the client has exited. The screen an
    # operator would have been looking at at that moment is exactly the stream
    # up to that offset, so nothing is lost by deferring it.
    raw = bytearray()
    raw_lock = threading.Lock()
    stop = threading.Event()
    marks: list[tuple[int, str]] = []

    def pump() -> None:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with raw_lock:
                raw.extend(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    ok = True
    time.sleep(1.0)
    for step in script:
        verb, _, arg = step.partition(":")
        if verb == "wait":
            time.sleep(float(arg or 1.0))
        elif verb == "key":
            for ch in arg:
                os.write(master, ch.encode())
                time.sleep(0.25)
        elif verb == "esc":
            os.write(master, b"\x1b")
            time.sleep(0.4)
        # The startup door is the first surface an operator walks with the
        # arrows and Enter before any view is on screen, so the harness has to
        # be able to send them.
        elif verb in ("up", "down", "enter"):
            os.write(master, {"up": b"\x1b[A", "down": b"\x1b[B",
                              "enter": b"\r"}[verb])
            time.sleep(0.4)
        elif verb == "shot":
            with raw_lock:
                marks.append((len(raw), arg or "screen"))
        elif verb == "quit":
            # A client that has already gone is not a failed quit — the door
            # scene's Esc can be the last key an unarmed window needs — but a
            # write that fails while it is still running is.
            if child.poll() is None:
                os.write(master, b"q")
                time.sleep(1.0)
        else:
            print(f"qa_capture: unknown step {step!r}", file=sys.stderr)
            ok = False

    if child.poll() is None:
        os.write(master, b"q")
        time.sleep(1.0)
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)
        time.sleep(1.0)
    if child.poll() is None:
        child.kill()
        ok = False
        print("qa_capture: the client had to be killed", file=sys.stderr)
    stop.set()
    reader.join(1.0)
    os.close(master)

    # Replay: the stream up to each mark, then everything after the last one —
    # the restore sequences are at the very end, so the exit report below is
    # about a screen that has seen them.
    at = 0
    for offset, label in [*marks, (len(raw), "")]:
        screen.feed(decoder.decode(bytes(raw[at:offset])))
        at = offset
        if label:
            print(f"\n=== {label} " + "=" * max(0, 60 - len(label)))
            print(screen.text())

    code = child.wait()
    print("\n=== exit " + "=" * 56)
    print(f"exit code           {code}")
    print(f"alternate screen    {'still held' if screen.alternate else 'released'}")
    print(f"cursor              {'visible' if screen.cursor_visible else 'HIDDEN'}")
    # Both are the restore path, and a client that quit without them leaves the
    # operator's shell in a state they have to `reset` out of.
    if screen.alternate or not screen.cursor_visible:
        ok = False
    if code not in (0, -signal.SIGTERM, 128 + signal.SIGTERM):
        ok = False
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("binary")
    ap.add_argument("--port", type=int, default=int(os.environ.get("QLAB_UI_PORT", "8765")))
    ap.add_argument("--size", default="120x40")
    ap.add_argument("--script", default="wait:2,shot:desk,quit")
    ap.add_argument(
        "--args",
        default="",
        help="arguments passed to the binary, space separated (e.g. --operator)",
    )
    args = ap.parse_args()
    width, height = (int(part) for part in args.size.lower().split("x"))
    extra = args.args.split()
    print(f"binary  {args.binary} {' '.join(extra)}".rstrip())
    print(f"port    {args.port}  ({'owner up' if owner_is_up(args.port) else 'NO OWNER'})")
    print(f"size    {width}x{height}")
    return run(args.binary, args.port, (width, height), args.script.split(","), extra)


if __name__ == "__main__":
    raise SystemExit(main())
