---
name: quota-verify
description: Formal check of the quota ledger and the shared call path (shared.once, quota.spend, quota.block, net._pay, clarity._cached, gsc URL Inspection). Use after a change to src/seo_mcp/shared.py, quota.py, net.py, the _cached or credential function of clarity.py, or the url_inspection or index_status code of gsc.py, or to models/metered_call.tla or models/parallel_callers.tla.
---

# Quota verify

Clarity gives 10 Data Export calls a day for each project, and URL Inspection gives 2,000 for each Search Console property. Many server processes share 1 cache folder and spend from 1 ledger file. A bug in this path spends the daily quota of a user, or serves the answer of another key. Unit tests check some orders of events. This check covers every order of the steps in 2 models, and it proves that each rule can fail.

It has 3 parts:

- **Models** (TLA+, checked by TLC through the `statechart` skill): `models/metered_call.tla` (1 call, from any ledger and cache that earlier calls can leave) and `models/parallel_callers.tla` (2 callers, 1 key or 2 keys, 1 ledger).
- **Mutants** (`scripts/verify_mutants.py`, area `quota`): each rule, broken on purpose in a copy of the model and in a copy of the real code. The model or the named tests must catch each one. The repo and the cache folder stay unchanged.
- **Anchor test** (`tests/test_models.py`, in `just check` and CI): each mutant text is in its file 1 time. It fails when the verified code changes under a mutant.

## Rules

| Rule | Model | Sentence | Harm when it breaks |
|---|---|---|---|
| NeverPastTheLimit | both | The server never sends a metered call past the daily limit, with any number of keys. | The API refuses the user for the rest of the day. |
| CountNeverBelowRequestsSent | both | The ledger count is never below the requests sent. | The ledger allows more calls than the API does. |
| ReservedBeforeTheNetwork | metered_call | The ledger counts a call before its request goes out. | A crash between send and count loses a spent call. |
| BlockedDaySendsNothing | metered_call | After a Data Export 429 is in the ledger, no new call spends on that UTC day. | Each new call gets its own 429. |
| Limit429SetsTheFlag | metered_call | A Data Export 429 writes the blocked flag. | As above. |
| BrokenLedgerSendsNothing | metered_call | A ledger that does not parse, or has a wrong day, sends nothing. | A broken file reads as 0 and spends without control. |
| BusyLedgerSendsNothing | metered_call | A ledger that a stuck process holds for 2 seconds sends and spends nothing. | A stuck process lets calls past the count. |
| CacheHitSpendsNothing, HitHasAGoodShape | metered_call | A saved answer of today goes back with no spend, and it has a good shape. | A repeat spends a call. |
| BadAnswerIsNotSaved | metered_call | An HTTP 200 with a wrong shape is never saved. | A bad answer serves all day. |
| RepeatInWindowSpendsNothing | both | A repeat of a failed request inside the 60-second window spends nothing. | A burst of agents spends 1 call each on 1 outage. |
| ConnectionFailureKeepsTheWindow, SpentFailureKeepsTheWindow | metered_call | Each failure of a spent call, also a connection that did not open, starts the window. | As above. |
| NoTokenReadsNoAnswer | metered_call | A saved answer or a failure serves only with the token that the cache path names. | A missing or new token reads an old answer. |
| TwoCallersSpendOneCall, FirstAnswerIsShared | parallel_callers | 2 parallel callers of 1 key and 1 request spend 1 call. | 18 agents of 1 audit spend 18 calls. |
| NoAnswerOfAnotherKey | parallel_callers | A saved answer or a failure serves only the key that made it. | A new key gets the answer of the old key. |
| NoSpendAfterTheLimitAnswer | parallel_callers | After the daily limit answer, the ledger refuses each new spend on that UTC day. A call that spent before the answer still goes out and gets the API error. | Each new call spends and gets its own 429. |

## Coverage

Each model action names its code in a comment above it. The main map:

| Model step | Code |
|---|---|
| `TokenMissing`, `TokenRead` | `clarity.credential` (called before `_cached`), `Site.secret` |
| `TakeLock`, `TakeLockHit`, `TakeLockRepeat` | `shared.once`, `shared._only_sender`, `shared._saved`, `shared._recent_failure` |
| `Reserve`, `RefuseLimit`, `RefuseBlocked`, `RefuseBroken`, `RefuseWrongDate`, `LockBusy`, `TakeLockSpend`, `TakeLockRefused` | the `prepare` step of `clarity._cached`, `gsc._url_inspection`, `gsc.index_status`; `quota.spend`, `quota.locked`, `quota._read` |
| `Send` | the `send` step of `shared.once`, `net.request`, `net._pay` |
| `ConnectFails`, `NotSent`, `Fails`, `OtherError`, `TimesOut`, `BadShape` | `shared.once` (`_mark_failure`, the `CancelledError` branch), `net.NOT_SENT` |
| `Export429`, `Export429LockBusy`, `Limit429` | the `send` step of `clarity._cached`, `quota.block` |
| `Answer` | `shared.once` (`quota.write_json` of the answer) |
| `Slot(c)` | `shared.entry`, `shared.named`, `shared.identity`, `_google.principal` |

Atomic steps: 1 model step holds the path lock, the check of the saved answer and the failure, and the spend. That is sound because the cache of a path changes only under its lock, and `quota.spend` reads and writes the ledger under the ledger lock with no await inside. The send is its own step: `net.request` awaits the connection after the spend, and another task or process can block the day in between.

Sync commit: `61bbe1b`. The models and mutants describe `src/` as of this commit. Move it forward only after you confirm that each model step still matches its code at the new commit.

## Steps

1. **Does the change touch the covered code?** Run `git diff 61bbe1b -- src/seo_mcp/shared.py src/seo_mcp/quota.py src/seo_mcp/net.py src/seo_mcp/providers/clarity.py src/seo_mcp/providers/gsc.py`. Name each touched function from "Coverage", or none. If none, stop.
2. **Run the models.** Run `just models <scratch folder>`. Done when it prints "Each break is in the known list, and each other rule holds."
3. **Read each result** with the `statechart` skill. For a break, read the shortest path in `<scratch folder>/<model>/report.md`. Show `<scratch folder>/parallel_callers/chart.mmd` as a `mermaid` block when George asks how parallel calls move. `metered_call` has more than 500 states, so it has no chart: read its report.
4. **Run the mutants.** Run `uv run python scripts/verify_mutants.py quota` (about 1 minute). Done when it prints "Each mutant is caught." Read each other line by its first word:
   - `BROKEN`: the code as it is now fails the tests of that rule, before any mutant. The failed test follows the line. Go to step 5 with that test.
   - `MISSED`: the model or the tests can no longer see that break. Strengthen them, never the mutant.
   - `error: ... found 0 times`: the code or the model moved. Copy the new line into `QUOTA` with the same break.
   - Any other `error`: TLC, pytest, or the copy failed. That is no catch: fix the cause and run again.
5. **When a rule breaks**, decide which side is wrong. Replay the path on the real code, 1 step for each action, as a test with the `live` and `fake_net` fixtures. To hold a request between its spend and its send, give `net.transport` an `httpx2.AsyncBaseTransport` whose `handle_async_request` awaits an `asyncio.Event` (as `Gate` in `tests/test_failure_window.py`).
   - The test shows the break: it is a bug in the code. Report it with the path in the code's words. Add the line to `models/known-breaks.txt` only when George or the brief keeps the code. Fix the code only when George or the brief asks.
   - The test does not show the break: the model is wrong. Fix the model to match the code, and say what was wrong.
   - Never weaken a rule to make it hold. A rule changes only when George changes the promise.
6. **An intended change to the flow.** Change the code, the model, and the mutant text in 1 PR. Run steps 2 to 4, then move the sync commit.

## Scope

- `Limit` is 1 in `metered_call` and 2 in `parallel_callers`. The models have 1 UTC day, 2 callers, and 2 keys. 2 different requests of 1 key act as 2 keys: 2 paths, 1 ledger.
- Each rule has a model mutant and a code mutant, with 1 exception. NoTokenReadsNoAnswer has a model mutant only: the order of `clarity.credential` and `shared.once` has no 1-line break.
- `metered_call` spends 1 call. `gsc.index_status` spends 1 call for each URL in 1 spend: the model does not cover the batch size.
- `parallel_callers` has no busy ledger lock and no failed `quota.block`. `metered_call` has both.
- Not covered: the midnight edge between `spend` and `block` (only tests cover `receipt.day`), the prune of old entries, the salt file, GA4 token readings (`ga4._record` only records what GA4 says, it gates no call), and Bing and PageSpeed, which have no ledger.

## Accepted behavior

- 2 requests of 1 Clarity project, A and B, both spend before the Data Export 429. A gets the 429, and `quota.block` writes the flag. B still goes out and gets the API error. B was counted, so the ledger stays correct. The code does not check the flag again before the send, by design. NoSpendAfterTheLimitAnswer holds: a call that comes after the flag spends nothing. `tests/test_failure_window.py::test_after_the_429_no_new_call_spends` replays this path.
