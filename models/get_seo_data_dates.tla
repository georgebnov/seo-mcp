---- MODULE get_seo_data_dates ----
\* 1 dated source of get_seo_data (search_queries of Search Console, or ga4_organic):
\* params.parse, gsc._report or ga4._report, shared.once, and the coverage of _google.
\* A day is a number. Last is the last complete day of the API now, and Newest is the newest day
\* that the server allows: params.newest, today minus 1, for both sources. Today is the local date
\* of the machine, read 1 time for each call (params.one_day). No hour and no zone take part.
\* The range is 1 side of a date comparison. First is the first day of data of the property:
\* GA4 or Search Console collected nothing before it. The other side has data on each day.
EXTENDS Naturals
CONSTANTS Last, Newest, MaxDay
VARIABLES pc, prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
          totals, partial, sent, first, startRow, read, delta, extra, appliedEnd
vars == <<pc, prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
          totals, partial, sent, first, startRow, read, delta, extra, appliedEnd>>
Gate == <<first, startRow, read, delta, extra, appliedEnd>>
Days == 1..MaxDay
Min(a, b) == IF a < b THEN a ELSE b

Init ==
  /\ pc = "asked" /\ prov = "unset" /\ start = 0 /\ end = 0 /\ sentEnd = 0 /\ moved = FALSE
  /\ cov = "none" /\ fetchedLast = 0 /\ shownLast = 0 /\ reply = "none" /\ status = "none"
  /\ totals = "none" /\ partial = FALSE /\ sent = 0
  /\ first = 0 /\ startRow = FALSE /\ read = "none" /\ delta = "none" /\ extra = 0
  /\ appliedEnd = 0

\* The agent names a source and 2 dates. The property has its first day of data.
Ask ==
  /\ pc = "asked" /\ pc' = "named"
  /\ prov' \in {"gsc", "ga4"} /\ start' \in Days /\ end' \in Days /\ first' \in Days
  /\ UNCHANGED <<sentEnd, moved, cov, fetchedLast, shownLast, reply, status, totals, partial, sent>>
  /\ UNCHANGED <<startRow, read, delta, extra, appliedEnd>>
Same == UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
                    totals, partial, sent>> /\ UNCHANGED Gate
\* params.period: start_date after end_date is BadInput for every source, before any source runs.
DatesBackwards == pc = "named" /\ start > end /\ pc' = "paramError" /\ Same
DatesInOrder == pc = "named" /\ start <= end /\ pc' = "checked" /\ Same
\* gsc._newest_end, ga4._sent_dates: a start past the newest day raises too_late before the token.
StartTooLate == pc = "checked" /\ start > Newest /\ pc' = "tooLate" /\ Same
\* An end past the newest day goes out as the newest day, and the result gets date_moved.
\* GA4 gets plain dates too (ga4._sent_dates), so both sources move the same way.
EndSet ==
  /\ pc = "checked" /\ start <= Newest /\ pc' = "endSet"
  /\ sentEnd' = Min(end, Newest) /\ moved' = (end > Newest)
  /\ UNCHANGED <<prov, start, end, cov, fetchedLast, shownLast, reply, status, totals, partial, sent>>
  /\ UNCHANGED Gate
\* gsc._last_complete: the coverage read (its own saved entry, 5 minutes) gives
\* firstIncompleteDate minus 1. It is the value of the API now.
CoverageFromApi ==
  /\ pc = "endSet" /\ prov = "gsc" /\ pc' = "covered" /\ cov' = "api" /\ shownLast' = Last
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, fetchedLast, reply, status, totals, partial, sent>>
  /\ UNCHANGED Gate
\* The coverage read fails or has no value (Search Console: Newest - 3), or the source is GA4
\* (today - 3): an estimate from today (gsc._estimate, ga4._last_complete).
CoverageEstimate ==
  /\ pc = "endSet" /\ pc' = "covered" /\ cov' = "estimate"
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, fetchedLast, shownLast, reply, status, totals, partial, sent>>
  /\ UNCHANGED Gate
\* shared.once: the report goes out, and the answer holds the days as the API has them now.
SendReport ==
  /\ pc = "covered" /\ pc' = "flight" /\ sent' = sent + 1 /\ fetchedLast' = Last
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, shownLast, reply, status, totals, partial>>
  /\ UNCHANGED Gate
\* shared.once: a saved answer of the same body. Another source or agent read the coverage first,
\* so the 2 entries end at different times. With an API day, that day is in the key of the report
\* (gsc._report basis), so a saved answer has the day of the coverage. With an estimate, today is
\* in the key (gsc._basis, ga4._post), so a saved answer is of the same day, but it can be from
\* before the API moved its last complete day.
SavedReport ==
  /\ pc = "covered" /\ pc' = "answered"
  /\ fetchedLast' = IF cov = "api" THEN shownLast ELSE Last - 1
  /\ reply' \in {"rows", "noRows"}
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, shownLast, status, totals, partial, sent>>
  /\ UNCHANGED Gate
\* An HTTP 200. noRows has a documented field and no row, such as responseAggregationType or kind.
Answer ==
  /\ pc = "flight" /\ pc' = "answered" /\ reply' \in {"rows", "noRows", "emptyObject", "otherText"}
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, status, totals, partial, sent>>
  /\ UNCHANGED Gate
\* gsc.REPORT = shared.has_any and ga4.REPORT = shared.holds("kind") refuse {}.
Good(r) == r \in {"rows", "noRows"}
BadShape ==
  /\ pc = "answered" /\ ~Good(reply) /\ pc' = "result" /\ status' = "error"
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, totals, partial, sent>>
  /\ UNCHANGED Gate
\* gsc._report, ga4._report: with no totals row, totals are null ("none") when the start is
\* after the shown last complete day (each day is partial), else "zero": 0 for each count.
\* A GA4 rate is null in "zero", because 0 sessions give 0/0 (ga4.EMPTY_TOTALS).
\* _google.coverage: partial_days runs from the day after the last complete day to the end.
Result ==
  /\ pc = "answered" /\ Good(reply) /\ pc' = "result" /\ status' = "ok"
  /\ shownLast' = IF cov = "api" THEN shownLast ELSE fetchedLast - 1
  /\ totals' = IF reply = "rows" THEN "value"
               ELSE IF start <= shownLast' THEN "zero" ELSE "none"
  /\ partial' = (sentEnd > shownLast')
  \* GA4 or Search Console has no row on a day before First, and can have none on a real 0 day.
  /\ startRow' \in IF reply = "rows" /\ start >= first THEN BOOLEAN ELSE {FALSE}
  \* core._asked: applied shows the sent end of date_moved, else the asked end.
  /\ appliedEnd' = IF moved THEN sentEnd ELSE end
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, reply, sent>>
  /\ UNCHANGED <<first, read, delta, extra>>
\* core._delta: unknown totals give no delta.
Shown == IF totals = "none" THEN "null" ELSE "shown"
Keep == UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
                    totals, partial, sent, first, startRow, appliedEnd>>
\* ga4._gap_at_start: a GA4 day report with a row on the first day of the range needs no read.
\* A GA4 landing page report and each Search Console report have no day rows, so they read.
NeedsRead == prov = "gsc" \/ ~startRow
DeltaNoRead ==
  /\ pc = "result" /\ status = "ok" /\ ~NeedsRead /\ pc' = "gated" /\ delta' = Shown
  /\ UNCHANGED <<read, extra>> /\ Keep
\* ga4._first_event, gsc._first_data: 1 saved read of a fixed body gives the first day of data,
\* no day (no event at all), or a failure. core._gate_delta: only a found day on or before the
\* start keeps the delta.
DeltaAfterRead ==
  /\ pc = "result" /\ status = "ok" /\ NeedsRead /\ pc' = "gated" /\ extra' = extra + 1
  /\ read' \in {"found", "noEvent", "failed"}
  /\ delta' = IF read' = "found" /\ first <= start THEN Shown ELSE "null"
  /\ Keep
Done == (pc \in {"paramError", "tooLate", "gated"} \/ (pc = "result" /\ status = "error"))
        /\ UNCHANGED vars

Next ==
  Ask \/ DatesBackwards \/ DatesInOrder \/ StartTooLate \/ EndSet \/ CoverageFromApi
  \/ CoverageEstimate \/ SendReport \/ SavedReport \/ Answer \/ BadShape \/ Result
  \/ DeltaNoRead \/ DeltaAfterRead \/ Done

Spec == Init /\ [][Next]_vars
Ok == pc \in {"result", "gated"} /\ status = "ok"

\* George: a start after the end sends no request (also after the end moves to the newest day).
StartAfterEndSendsNothing == (pc # "asked" /\ (start > end \/ start > Newest)) => sent = 0
\* George: a future end moves to the newest day, and the result says so.
FutureEndMovesAndSaysSo == (Ok /\ end > Newest) => (sentEnd = Newest /\ moved)
\* George: a partial day is never shown as complete.
PartialDayNeverShownComplete == Ok => shownLast <= fetchedLast
\* Each day after the shown last complete day is in partial_days.
PartialDaysAreMarked == (Ok /\ sentEnd > shownLast) => partial
\* George: an answer with no documented field is never ok.
NoDocumentedFieldIsNeverOk == (pc = "result" /\ reply \in {"emptyObject", "otherText"}) => status # "ok"
\* George: an unfinished day is never a 0.
UnfinishedDayIsNeverZero == (Ok /\ start > shownLast) => totals # "zero"
\* George: a range with a complete day and no rows is a real 0, so a drop shows in delta.
CompleteDayWithNoRowsIsZero == (Ok /\ reply = "noRows" /\ start <= shownLast) => totals = "zero"
\* George: the comparison needs to be same dates. A delta never compares a range with no data.
DeltaNeverComparesNoData == delta = "shown" => first <= start
\* A real 0 after the first day of data keeps the delta.
RealZeroKeepsTheDelta ==
  (pc = "gated" /\ read = "found" /\ first <= start /\ totals # "none") => delta = "shown"
\* A GA4 day report with a row on the first day sends no extra read, and no range sends 2.
ExtraReadOnlyForAGap == (pc = "gated" /\ prov = "ga4" /\ startRow) => extra = 0
AtMost1ExtraRead == extra <= 1
\* The tool text: a source that ran shows the values that it used in applied.
AppliedShowsTheSentEnd == Ok => appliedEnd = sentEnd
====
