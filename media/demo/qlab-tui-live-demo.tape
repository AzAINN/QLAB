# VHS tape: the Atlas workstation on a LIVE desk with a real workforce run
# in flight. Render while a run is active for content in views 5 and 6:
#   QLAB_UI_PORT=8765 qlab workforce run "GOAL" &   # then:
#   vhs media/demo/qlab-tui-live-demo.tape
#
# The client is read-only by construction (GLASS), so recording against the
# real owner observes and cannot write.

Output media/demo/qlab-tui-live-demo.mp4

Set FontSize 18
Set Width 1920
Set Height 1080
Set Padding 12
Set TypingSpeed 70ms
Set Shell zsh

Hide
Type "cd "$(git rev-parse --show-toplevel)" && export QLAB_UI_PORT=8765 && clear"
Enter
Sleep 2s
Show

Type "clients/atlas-tui/target/release/atlas"
Sleep 1s
Enter

# Attach + first paint.
Sleep 6s

# 1 Desk — live tickers, the Atlas read.
Sleep 8s

# 2 Markets — real prices.
Type "2"
Sleep 6s

# 4 Research — registry history.
Type "4"
Sleep 5s

# 5 Workforce — the run's flowchart; let phases visibly advance.
Type "5"
Sleep 25s

# 6 Audit — the bus, busy with tool calls.
Type "6"
Sleep 10s

# 5 again — catch movement since last look.
Type "5"
Sleep 15s

# 7 Settings — DESK MODE shows the LIVE lane.
Type "7"
Sleep 5s

# Closing shot on the Desk.
Type "1"
Sleep 8s

Type "q"
Sleep 3s
