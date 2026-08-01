# VHS tape: the completed workforce run — pipeline done, reporter's memo on
# screen. Render after a run finishes:
#   vhs media/demo/qlab-tui-results-demo.tape

Output media/demo/qlab-tui-results-demo.mp4

Set FontSize 18
Set Width 1920
Set Height 1080
Set Padding 12
Set TypingSpeed 70ms
Set Shell zsh

Hide
Type "cd /Users/azainmac/codebases/quant-trading-agent && export QLAB_UI_PORT=8765 && clear"
Enter
Sleep 2s
Show

Type "clients/atlas-tui/target/release/atlas"
Sleep 1s
Enter
Sleep 6s

# 5 Workforce — the finished run: all phase dots green, reporter summary.
Type "5"
Sleep 20s

# 3 Book — plans pane; a checked preview waits for human approval, view-only.
Type "3"
Sleep 8s

# 6 Audit — verdict and decision events at the top of the stream.
Type "6"
Sleep 8s

# 1 Desk — the read absorbs the run's conclusion.
Type "1"
Sleep 8s

Type "q"
Sleep 3s
