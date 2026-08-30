# Primary sources, Stream D — completion record (2026-08-28)

Closes Stream D of `2026-08-28-primary-sources-plan.md` (tasks 1–6). Tasks 1–5
built the provider stack (`parse_provider_stack`, `fetch_news_stacked`, plugin
discovery) and the three first-party primary/secondary providers (`edgar`,
`macro`, `gdelt`). Task 6 wired the owner to it.

## What the owner does now

- `UISession.news_provider_for(offline)` returns a **stack**, `tuple[str, ...]`.
  Offline is always `("synthetic",)`; otherwise `QLAB_NEWS_PROVIDERS` then
  `QLAB_NEWS_PROVIDER` (both parsed by `feed.parse_provider_stack`, so the desk
  and a bare `fetch_news` cannot disagree about what a stack is), then
  `("alpaca",)` when a credential resolves, then `("synthetic",)`.
- `fetch_desk_news` calls `fetch_news_stacked`. The window gains `outcomes`,
  `providers` and `provider`; `provider_name` is kept as the joined string
  because `compose_desk_read` and `news_payload` label from it.
- `archive_desk_news` files **one `ArchiveBatch` and one `news_archive` event
  per member**, carrying that member's `outcome`. An `ArchiveBatch` holds one
  provenance, so a merged batch would file an SEC filing and a wire story under
  one attribution and neither could be replayed. Members that returned nothing
  are still walked: "this source answered with nothing" and "this source was not
  read" are different facts.
- The `/api/tui` news block gains `providers` and `outcomes`.
- `check_news` returns `members: {name: report}` plus the first member's fields
  at the top level, so a stack of one reads exactly as it always did. `ok` is
  `any(member ok)` — one living member is still a record. `render()` prints one
  block per member.
- `fetch_news_stacked` now publishes the **merged** window to the in-process
  cache under `_CACHE_LOCK`. Before this, each member's `fetch_news` overwrote
  the cache, so `cached_news_provenance` described only the last member's
  records under the last member's name.

## Tests

Full offline suite: **1410 passed, 10 skipped, exit 0** (`pytest -q`, no
network). New tests, each seen failing first:

- `tests/test_news_stack.py::test_the_merged_window_is_what_provenance_reports`
  — failed `('two', 1) == ('one', 3)` before the cache merge.
- `tests/test_ui.py::test_the_desk_reads_a_stack_and_archives_each_member`
  — failed `'synthetic' == ('one', 'two')` before the tuple contract.
- `tests/test_news.py::test_news_check_reports_each_member_of_a_stack`
  — failed `KeyError: 'members'`.

## Live check — a negative result (invariant 11)

Run from the worktree with `QLAB_STATE_DIR` pointed at a scratch directory, so
no `.lab` was touched and no owner was started:

```
$ python -m qlab.autopilot.cli news-check --provider macro,gdelt
news integration: NOT WORKING
  stack              macro,gdelt

  provider           macro  [NOT WORKING]
  error              RuntimeError: rss news provider requires reachable network feeds;
                     source 'BLS' at 'https://www.bls.gov/feed/bls_latest.rss' is
                     unavailable (HTTP Error 403: Forbidden)

  provider           gdelt  [NOT WORKING]
  error              URLError: <urlopen error timed out>
```

**Neither live member answered today.** The integration behaved correctly — each
member is named with its own reason, nothing was absorbed into a smaller window
— but the sources themselves are the finding:

| feed | status today | note |
|---|---|---|
| `https://www.bls.gov/feed/bls_latest.rss` | **403 Forbidden** | 403 with a browser User-Agent too (`curl -A "Mozilla/5.0"`), so it is not a UA problem — bls.gov is refusing programmatic requests outright. `https://www.bls.gov/feed/news_release.rss` is also 403. |
| `https://home.treasury.gov/news/press-releases/rss` | **404 Not Found** | the path no longer exists. |
| `https://apps.bea.gov/rss/rss.xml` | 200 | the only reachable macro feed. |
| GDELT `api/v2/doc/doc` | 200, but **31s** | `curl` succeeded in 31.3s; the provider's `_TIMEOUT_S = 10` expires first, so it fails as a timeout. |

No URL was changed to make this pass. The correct replacement for either dead
feed is not unambiguous (BLS appears to be blocking automation at the edge
rather than having moved the file; Treasury's press-release RSS has no obvious
official successor path), and guessing one would put an unverified source in a
primary-tier lane. This is recorded as work, not repaired silently.

Follow-ups this implies, none of them done here:

1. Decide what `macro` does when ONE of its feeds is dead. Today
   `_fetch_rss_feeds` fails loud on the first unreachable feed, so a single 403
   takes down a member that has two other live publishers. That is the correct
   default for a single-feed provider and probably the wrong one for a
   multi-feed one — but changing it is a fail-loud decision, not a bug fix.
2. Re-verify the BLS and Treasury URLs, and replace them from the publisher's
   own feed index rather than by search.
3. `gdelt`'s 10s timeout is tight for the doc API's current latency. Measure
   before raising it; a slow source that is always slow is a different problem
   from one that is occasionally slow.

### Not run: EDGAR

`edgar` requires `QLAB_EDGAR_CONTACT`, an identity only the operator can set
(the SEC asks that automated requests identify a contact, and the provider
refuses rather than sending an anonymous one). **User-run step:**

```bash
export QLAB_EDGAR_CONTACT="Your Name your@email"
qlab news-check --provider edgar
# and the whole stack:
qlab news-check --provider alpaca,edgar,macro,gdelt
```

Then, to put the desk on the stack: set `QLAB_NEWS_PROVIDERS=alpaca,edgar,macro,gdelt`
and `QLAB_EDGAR_CONTACT` in `.env` and restart the owner (invariant 8 — a
long-lived owner keeps serving pre-change imports).

## Docs

`docs/news-setup.md` gains the six-provider table with tiers, a "Stacking
providers" section, and the per-member `news-check` output. The README extras
table's `news` row now names all four network providers.

## Second run — 2026-08-28, after the fix round

Three of the four findings above were acted on, and the live check was re-run
with the same scratch `QLAB_STATE_DIR`, from the worktree, with no owner:

```
$ python -m qlab.autopilot.cli news-check --provider macro,gdelt
news integration: OK
  stack              macro,gdelt

  provider           macro  [OK]
  fetched/kept       2/2
  claims             1 (1 well-supported, 1 primary)
  publishers         Bureau of Economic Analysis
  window hash        13adfe5003f877ef
  sample:
    - GDP (Second Estimate) and Corporate Profits, 2nd Quarter 2026  (primary source)

  provider           gdelt  [NOT WORKING]
  error              URLError: <urlopen error timed out>
```
Exit code 0 — a live primary-source window, which the first run did not have.

- **`macro` is live.** BLS and Treasury were removed from the shipped
  `macro.feeds` (each replaced by a comment naming the finding and the date);
  BEA answers, and a real BEA GDP release is now grounded as a primary claim.
  No replacement URL was invented: Treasury's press-release page links no feed
  at all, and bls.gov returns 403 even for its own feed index page
  (`/bls/rss.htm`), so nothing could be verified live.
- **A dead feed no longer takes a member down.** `PartialWindow` means a
  multi-feed provider keeps what answered and reports what did not as
  `partial: <feed>: <error>`; only an all-dead provider is a dead member. Had
  this existed on the first run, `macro` would have returned BEA's records
  instead of going dark.
- **`gdelt` is still dark, and the timeout was not the whole story.** Raising
  `_TIMEOUT_S` from 10s to 45s did not fix it. Measured on the same connection
  minutes later: one DOC API request returned 200 in **43.1s**, another **hung
  past 75s** and was killed. The API is reachable but its latency today is both
  high and unstable, and the provider issues one request per rule (five rules
  shipped), so a full member fetch is minutes, not seconds. This is a
  measurement, not a fix.

Follow-ups this leaves open (superseding items 1 and 3 of the first run; item 1
is now done, item 2 stands):

1. **Decide whether `gdelt` belongs in a default stack at all while it is this
   slow.** A member that takes minutes on the heartbeat's path is a different
   problem from one that fails, and raising a timeout is the wrong instrument
   for it — a per-rule budget, fewer rules, or leaving `gdelt` an opt-in member
   are the real options. Do not raise `_TIMEOUT_S` again without measuring.
2. Re-verify the BLS and Treasury URLs from the publishers' own feed indexes
   (unchanged from the first run). If BLS keeps refusing automation, that is a
   publisher decision, not a bug, and the honest record is the comment now in
   `news_sources.yaml`.
