# Snippet-level review of the desk surface

**Date:** 2026-07-30
**Method:** probing, not reading. Every finding below was produced by running
the code against hostile input and observing what it did — a claim about
behaviour that has not been executed is a guess.

Four defects found, all fixed. Two negative results recorded, because a claim
that did not survive reproduction is worth as much as one that did.

## Fixed

### 1. The coordinator slot was not one slot (concurrency)

The owner is a `ThreadingHTTPServer` with a heartbeat thread beside it, so every
guarantee it makes is a concurrent guarantee or it is not one.

`coordinator_driver` built lazily with no lock. Probing with a 16-way barrier:

```
distinct driver objects handed out: 15
```

Each had its own `threading.Lock` and its own session slot, so "one coordinator
at a time" was not a property of the system — it was a property of never having
had two callers. Under concurrent dispatch this is N Claude trees on one desk.

Fixed with double-checked locking. The fast path stays lock-free because
`coordinator_status()` is on the snapshot path and runs every tick.

### 2. Shutdown leaked the coordinator it was written to kill

`stop()` released the slot *before* the process was gone, and took no lock:

```
stop/drive overlap -> extra coordinators during teardown: 2
```

The only caller is the owner's shutdown hook, so the second tree outlived the
runtime it was talking to and kept billing against a dead URL — precisely the
leak the hook exists to prevent.

`stop()` now holds the lock across the whole teardown, clears the slot only once
the process is gone, and is **terminal**: an owner that has begun shutting down
refuses a new coordinator even if none happened to be running when it started.
An idle `stop()` is still safe, but it also closes.

### 3. The kill switch was still trippable by a book switch on this branch

The fix existed but was stranded on an unmerged PR, so the branch still had it.
Reproduced exactly:

```
simulated book only   cash=  10000.00 hwm=  10000.00
after alpaca marks    cash=  10000.00 hwm=  32626.00
computed drawdown     69.35%   -> HALTED (spurious)
```

69.35% matches the figure observed on the live desk to the digit. One shared
`account` row means an Alpaca paper account (~$32,626) ratchets the high-water
mark, and the $10k simulated book then reads `1 - 10000/32626` and halts without
having lost anything. Cherry-picked onto this branch; the books no longer share
a high-water mark.

### 4. A NaN weight rendered as a real target

`_format_targets` coerced per entry to survive a string weight, but `float(nan)`
succeeds — so a NaN reached the screen as `SPY nan%`, a number-shaped non-number
on a trading surface. Reachable: Python's `json` emits *and* parses `NaN` by
default, so an agent-authored artifact carries one all the way to the render.
Now reported as `[unreadable: SPY]`, the same as a string.

## Verified clean

- **Invariant 3 holds.** Execution requires `human_confirmed=true`, a real
  `approval_id`, *and* a real `plan_id`; each is refused independently. No
  agent-reachable execution tool exists on the MCP surface.
- **Input handling.** Bad enums, wrong types, missing fields, a 100k-character
  message, `'; DROP TABLE workflows; --` as a path segment, and
  `../../etc/passwd` as a decision id all return 4xx with a reason. Queries are
  parameterised; the traversal attempt is simply an unknown id. The registry was
  intact afterwards.
- **Render helpers.** `braille_chart` and `spark` return correctly-shaped output
  for empty, single-point, all-identical, NaN, inf, all-NaN, negative, and
  1e-9-to-1e9 series. No raise, no shape drift.
- **No unreachable code.** A repo-wide sweep for public callables defined once
  and referenced nowhere returned 11 candidates, all of which resolved to
  attribute calls the detector's lookbehind had excluded. The pattern that
  produced three real bugs on this branch — `adjudicate()` with zero callers,
  fast-mode routing nothing passed `True` to, `list_debates()` whose first bug
  proved it had never run — is now clean.

## Claims that did not survive

- **`_format_targets` raises on a non-dict.** True in isolation — `None`, a
  string, and a list all raise `AttributeError`. Not a bug: both call sites go
  through `_extract_targets`, which guards with `isinstance(targets, dict)` and
  returns `{}` otherwise. Unreachable, so left alone.
- **The 335-orphan sweep.** A first pass flagged 335 of 623 public callables as
  uncalled. The detector was counting `name(` and so missed `self.method()`,
  framework dispatch (Textual `action_*`, MCP `@tool`), and decorator
  registration. Recorded because a 54%-orphan result should have been read as a
  broken detector immediately, not as a finding.

## Not covered

The TUI's interactive paths (key handling, screen transitions, the command
palette) were exercised only through the existing test suite, not probed
adversarially. The Rust client has its own 22 tests including a narrow-terminal
survival case, but has never run against a degraded owner mid-session.
