# qlab tutorial voiceover — script and shot cues

Eight segments, ~2:50 total at a natural TTS pace. Each segment is one MP3 so
you can slide them independently against your screen recording. Timings assume
~150 words per minute; ElevenLabs usually lands a touch faster.

| # | Segment | ~Length | What's on screen |
|---|---|---|---|
| 1 | Hook | 0:18 | Title card / desk already running |
| 2 | Launch | 0:22 | Terminal: `qlab tui`, workstation opens, flip through views 1–7 |
| 3 | The read | 0:24 | Atlas read panel; hover the tensions line |
| 4 | Ask your quant | 0:27 | Type a question, answer appears with citations |
| 5 | The workforce | 0:29 | Workforce view: flowchart, roles lighting up, audit bus |
| 6 | The gate | 0:28 | Referee PASS, confirmation prompt — then decline it |
| 7 | Honest results | 0:25 | README "Honest results" table / planning-docs write-up |
| 8 | Close | 0:12 | Desk wide shot, fade |

---

## 1 — Hook

> Everyone can rent intelligence now. The hard part is trusting it with your
> money. This is qlab — a personal quant desk where an AI reads the market,
> remembers everything, argues with itself — and can never touch the money
> without you. Let me show you.

## 2 — Launch

> One command starts the desk. This is the Atlas workstation — seven views:
> your desk, live markets, the paper book, research, the AI workforce, and a
> full audit trail. Everything you're seeing runs through one governed
> runtime. One process owns the data, and every screen is just a window onto
> it.

## 3 — The read

> Meet Atlas, the desk manager. On a heartbeat, it composes a read across
> three things: a five-indicator market regime panel, the news record, and
> what its research agents concluded. And it leads with tensions — the places
> where the evidence disagrees with itself. "Prices are calm, but the coverage
> isn't." That's the thing a single number can never tell you.

## 4 — Ask your quant

> And you can ask it questions. The desk keeps a durable, point-in-time news
> archive — every story hashed, clustered, and stamped with the moment the
> desk actually knew it. So when I ask what's driving a name this morning, the
> answer comes back with citations bound to real archived records. If the
> model tries to cite something the archive doesn't hold, the entire answer is
> refused. No hallucinated sources. Ever.

## 5 — The workforce

> When a question needs real research, Atlas dispatches a governed workforce:
> five AI roles walking a pipeline the database itself enforces. An analyst
> defends a regime call. A challenger argues the opposite case — every
> decision on this desk carries its own rebuttal. An optimizer runs a
> cataloged algorithm, and exercises zero judgment. Then a referee
> independently checks the result. The AI owns the judgment. The algorithms
> own the numbers. Deterministic code owns the rigor.

## 6 — The gate

> Here's the part that matters. The referee's pass is bound to the exact
> target weights it reviewed — change a single number, and the pass is void.
> And even then, nothing executes without two things no AI can supply:
> propose mode, and my explicit confirmation. Watch — I can just say no. The
> plan dies. Paper trading only, by construction. There is no raw-order tool
> anywhere in this system.

## 7 — Honest results

> One more thing: this desk tells the truth about itself. Its sophisticated
> four-moment optimizer lost to simple benchmarks out of sample — so it's
> benched, and the desk runs the method the evidence actually supports. A
> feature that first measured a four-x win turned out to be a caching bug —
> and the desk's own robustness checks caught it. Negative results are
> recorded here, not deleted.

## 8 — Close

> qlab. An AI that reads everything, remembers everything, argues with
> itself, and answers with receipts — while the last word stays yours. That's
> what a personal quant should be.

---

## Generating the audio

```bash
cd media/voiceover
ELEVENLABS_API_KEY=sk_... ./generate.sh          # MP3s land in out/
```

Options via environment variables:

```bash
VOICE_ID=21m00Tcm4TlvDq8ikWAM ./generate.sh      # Rachel instead of Adam
MODEL_ID=eleven_turbo_v2_5 ./generate.sh         # cheaper/faster model
./generate.sh --list-voices                      # print your account's voices
```

The `segments/*.txt` files are the TTS inputs — they use speech-friendly
spellings ("Q-Lab", "T-U-I") so the narration sounds right; edit those, not
this file, to change what is spoken, then re-run the script. Only re-run the
segments you changed if you want to save credits: `./generate.sh 04` renders
just `04_ask.txt`.
