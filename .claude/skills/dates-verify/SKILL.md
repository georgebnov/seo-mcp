---
name: dates-verify
description: Formal check of the date and delta gate of get_seo_data (start and end checks, the move to the newest day, the last complete day, partial days, real 0 against unknown totals, the first day of data, and the delta). Use after a change to params.period, the _newest_end, _report, _last_complete, _basis, _shown, _first_data, _first_event, _gap_at_start, or REPORT code of gsc.py or ga4.py, _google.coverage, core._asked, core._delta, core._gate_delta, or models/get_seo_data_dates.tla.
---

# Dates verify

`get_seo_data` gives totals and a delta for a date range. A wrong gate shows a partial day as complete, an unfinished day as a 0, or a drop that compares a range with no collected data. An agent then tells the user that traffic fell when it did not. Unit tests check some ranges. This check covers every order of steps and every range of small days in 1 model, and it proves that each rule can fail.

It has 3 parts:

- **Model** (`models/get_seo_data_dates.tla`, TLA+, checked by TLC through the `statechart` skill): 1 dated source (Search Console `search_queries` or GA4 `ga4_organic`) from the asked dates to the delta.
- **Mutants** (`scripts/verify_mutants.py`, area `dates`): each rule, broken on purpose in a copy of the model and in a copy of the real code. TLC or the named tests must catch each one. The repo and the cache folder stay unchanged.
- **Anchor test** (`tests/test_models.py`, in `just check` and CI): each mutant text is in its file 1 time.

## Rules

| Rule | Sentence | Harm when it breaks |
|---|---|---|
| StartAfterEndSendsNothing | A start after the end, or past the newest day of the source, sends no request. | A wasted call, or a reversed range read as no data. |
| FutureEndMovesAndSaysSo | An end past the newest day goes out as the newest day, and the result says so. | The API refuses, or the agent does not know which days it got. |
| AppliedShowsTheSentEnd | `applied.end_date` is the end that went out. | The agent compares ranges that it did not ask for. |
| PartialDayNeverShownComplete | The shown last complete day is never after the last complete day of the answer. | A partial day reads as final, and its low count reads as a drop. |
| PartialDaysAreMarked | Each day after the last complete day is in `partial_days`. | As above. |
| UnfinishedDayIsNeverZero | A range of only partial days with no rows has unknown totals, not 0. | An unfinished day reads as 0 traffic. |
| CompleteDayWithNoRowsIsZero | A range with a complete day and no rows has 0 totals. | A real drop to 0 shows no delta. |
| NoDocumentedFieldIsNeverOk | `{}` or other text with no documented field is never `ok`. | A failed read reads as no data. |
| DeltaNeverComparesNoData | A delta shows only when the first day of data is on or before the start. | A new property reads as a drop or a rise. |
| RealZeroKeepsTheDelta | A real 0 after the first day of data keeps its delta. | A real drop is hidden. |
| ExtraReadOnlyForAGap, AtMost1ExtraRead | A GA4 day report with a row on its first day sends no extra read, and no range sends 2. | Each comparison spends GA4 tokens twice. |

## Coverage

Each model action names its code in a comment above it. The main map:

| Model step | Code |
|---|---|
| `DatesBackwards`, `DatesInOrder` | `params.period` (and the order check in `ga4._newest_end`) |
| `StartTooLate`, `EndSet` | `gsc._newest_end`, `gsc._named`, `ga4._newest_end`, `ga4._window`, `params.too_late` |
| `CoverageFromApi`, `CoverageEstimate` | `gsc._last_complete`, `gsc._reported`, `gsc._estimate`, `_google.answer_day`, `ga4._coverage` |
| `SendReport`, `SavedReport`, `Answer` | `shared.once` through `gsc._analytics` (key with `gsc._basis`) and `ga4._post` |
| `BadShape` | `gsc.REPORT`, `ga4.REPORT` |
| `Result` | `gsc._report` (`unknown`, `total`, `_shown`), `ga4._report` (`EMPTY_TOTALS`, `unknown`), `_google.coverage`, `ga4._partial`, `core._asked` (`applied`) |
| `DeltaNoRead`, `DeltaAfterRead` | `ga4._gap_at_start`, `ga4._first_event`, `gsc._first_data`, `core._delta`, `core._uncollected`, `core._gate_delta` |

Atomic steps: the model has 1 caller, so a step is 1 call or 1 decision in the code. The only other actor is a saved answer of another source or agent, which `SavedReport` stands for.

Sync commit: `47ec7aa`. The model and mutants describe `src/` as of this commit. Move it forward only after you confirm that each model step still matches its code at the new commit.

## Steps

1. **Does the change touch the covered code?** Run `git diff 47ec7aa -- src/seo_mcp/params.py src/seo_mcp/core.py src/seo_mcp/providers/gsc.py src/seo_mcp/providers/ga4.py src/seo_mcp/providers/_google.py`. Name each touched function from "Coverage", or none. If none, stop.
2. **Run the models.** Run `just models <scratch folder>`. Done when it prints "Each break is in the known list, and each other rule holds."
3. **Read each result** with the `statechart` skill. For a break, read the shortest path in `<scratch folder>/get_seo_data_dates/report.md`. The check of record has 3,465 states, so it has no chart. For the chart, run `statechart` on `models/get_seo_data_dates.tla` with `models/get_seo_data_dates.chart.cfg` (2 days, and day 1 is both the last complete day and the newest day, so it fits the 500-state budget) and `models/get_seo_data_dates.view.mjs`, and show its `chart.mmd` as a `mermaid` block.
4. **Run the mutants.** Run `uv run python scripts/verify_mutants.py dates` (about 1 minute). Done when it prints "Each mutant is caught." Read each other line by its first word:
   - `BROKEN`: the code as it is now fails the tests of that rule, before any mutant. The failed test follows the line. Go to step 5 with that test.
   - `MISSED`: the model or the tests can no longer see that break. Strengthen them, never the mutant.
   - `error: ... found 0 times`: the code or the model moved. Copy the new line into `DATES` with the same break.
   - Any other `error`: TLC, pytest, or the copy failed. That is no catch: fix the cause and run again.
5. **When a rule breaks**, decide which side is wrong. Replay the path on the real code as a test with the `live` and `fake_net` fixtures: set the UTC date with `monkeypatch` as `tests/test_coverage.py` does, give the Search Console reply its `metadata.firstIncompleteDate`, and call `core.get_seo_data` with the dates of the path.
   - The test shows the break: it is a bug in the code. Report it with the path in the code's words. Add the line to `models/known-breaks.txt` only when George or the brief keeps the code. Fix the code only when George or the brief asks.
   - The test does not show the break: the model is wrong. Fix the model to match the code, and say what was wrong.
   - Never weaken a rule to make it hold. A rule changes only when George changes the promise.
6. **An intended change to the gate.** Change the code, the model, and the mutant text in 1 PR. Run steps 2 to 4, then move the sync commit.

## Scope

- Days are numbers 1 to 4. The last complete day of the API is 2, and the newest day is 3. 1 number stands for the newest day of both sources (Search Console: the UTC date; GA4: the UTC date plus 1).
- The model follows 1 range. The comparison range runs the same steps, and `core._found_day` joins the 2 first-day reads: the model does not cover that join. Tests in `tests/test_collected.py` do.
- The model reads no zone. GA4 reads a relative date in the property zone, and `ga4._partial` marks each day that can be partial in some zone: the model covers that only as "the estimate".
- Not covered: `last_days` and default ranges (they cannot be after the end), query and page filters, the Search Console history limit (`gsc._gaps`), `ga4_landing_pages` row limits, Clarity and Bing dates, and the `query_api` dates (`models/query_api.tla` covers those).

## Findings

- Each rule holds at `47ec7aa`. No code breaks a rule.
- A test gap: the code mutant of RealZeroKeepsTheDelta (`first <= start` to `first < start` in `core._uncollected`) was MISSED by all of `tests/test_collected.py`. No test had a range that starts on the first day of data. `test_a_range_that_starts_on_the_first_event_keeps_the_delta` now covers it.
- AtMost1ExtraRead has a model mutant only. In the code, `shared.once` makes a 2nd read of the same fixed body send nothing, so no 1-line break of the code sends 2.
