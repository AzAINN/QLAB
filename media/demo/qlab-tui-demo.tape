# VHS tape: silent b-roll of the Atlas workstation for the tutorial voiceover.
# Render with:  vhs media/demo/qlab-tui-demo.tape   (from the repo root)
#
# The desk runs fully isolated — its own QLAB_STATE_DIR and port — so the
# recording can never touch .lab/registry.duckdb or clash with a live owner.

Output media/demo/qlab-tui-demo.mp4

Set FontSize 18
Set Width 1920
Set Height 1080
Set Padding 12
Set TypingSpeed 70ms
Set Shell zsh

# Invisible setup: venv on PATH, isolated state, demo port.
Hide
Type "cd /Users/azainmac/codebases/quant-trading-agent && export PATH=$PWD/.venv/bin:$PATH QLAB_STATE_DIR=/private/tmp/qlab-demo-state QLAB_UI_PORT=8799 && clear"
Enter
Sleep 2s
Show

# Launch exactly the command the README shows.
Type "qlab tui --offline --port 8799 --claude off"
Sleep 1s
Enter

# Owner boot + first synthetic snapshot + client attach (measured ~1s; margin).
Sleep 8s

# 1 Desk — the Atlas read; let it breathe.
Sleep 6s

# 2 Markets
Type "2"
Sleep 5s

# 3 Book
Type "3"
Sleep 5s

# 4 Research
Type "4"
Sleep 5s

# 5 Workforce
Type "5"
Sleep 5s

# 6 Audit
Type "6"
Sleep 5s

# 7 Settings — shows DESK MODE and the posture line.
Type "7"
Sleep 5s

# Back to the Desk for the closing shot.
Type "1"
Sleep 7s

Type "q"
Sleep 3s
