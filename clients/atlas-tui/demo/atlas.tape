# The Atlas workstation, recorded.
#
#   vhs demo/atlas.tape          # writes demo/atlas.gif
#
# The tape is what is checked in; the GIF is not. A recording is a build
# artifact of a particular desk on a particular afternoon — it carries live
# numbers, a port, and whatever the owner happened to be holding — and a binary
# blob in git that nobody can regenerate from the repo is a worse record than
# the four lines that produce it.
#
# Needs: `vhs` (github.com/charmbracelet/vhs), a release binary
# (`cargo build --release`), and an owner runtime on $QLAB_UI_PORT. Without the
# owner it still records — the NO OWNER RUNTIME panel is a real surface and one
# worth having a picture of — but the views will be empty.
#
# Read `scripts/qa.sh` first if what you want is to *check* the client rather
# than to show it: that one reconstructs screens as text and needs no vhs.

Output demo/atlas.gif

Set Shell "bash"
Set FontSize 13
Set Width 1500
Set Height 900
Set Padding 10
Set TypingSpeed 55ms
Set Framerate 24

# The port every qlab surface agrees on, set explicitly so the recording does
# not depend on the shell it was started from.
Hide
Type "cd clients/atlas-tui && export QLAB_UI_PORT=8765"
Enter
Show

Type "./target/release/atlas"
Enter

# The first poll. Three seconds is the cadence; the first request goes out
# immediately, so this is the frame where the desk arrives.
Sleep 4s

# DESK: the read, the six tiles, and the always-on rails either side.
Sleep 2s

# The seven views in rail order. Tab rather than the digits, because the tape
# is also a demonstration that the rails never leave the screen.
Tab
Sleep 2.5s
Tab
Sleep 2.5s
Tab
Sleep 2.5s
Tab
Sleep 2.5s
Tab
Sleep 2.5s
Tab
Sleep 2.5s
Tab
Sleep 2s

# Back to the market grid for the effects: a refresh brings a new poll, and a
# quote that moved flashes the cell it moved in. On a quiet tape nothing
# flashes, which is the honest outcome — the effect is a fact about the desk,
# not a decoration the recording can stage.
Type "2"
Sleep 1s
Type "r"
Sleep 3s

# The command line, opened carrying the slash it was opened with, so the first
# frame is already the scope picker.
Type "/"
Sleep 1.5s
Type "ticker SPY"
Sleep 1.5s
Enter
Sleep 2s

# The overlay that takes the keyboard — every binding this window has, read off
# the same table the routers are checked against. This is the modal a glass
# build has; the confirmation box is an operator affordance and does not exist
# in this binary at all.
Type "?"
Sleep 3s
Down
Down
Sleep 1.5s
Escape
Sleep 1.5s

# Out, cleanly: the alternate screen is released and the cursor comes back.
Type "q"
Sleep 2s
