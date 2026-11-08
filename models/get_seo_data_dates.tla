---- MODULE get_seo_data_dates ----
\* 1 dated source of get_seo_data (search_queries of Search Console, or ga4_organic):
\* params.parse, gsc._report or ga4._report, shared.once, and the coverage of _google.
\* A day is a number. Last is the last complete day of the API now, and Newest is the newest day
\* that the server allows (Search Console: the UTC date; GA4: the UTC date plus 1, sent as today).
\* The model does not hold the zone of a GA4 property: the server reads none.
EXTENDS Naturals
CONSTANTS Last, Newest, MaxDay
VARIABLES pc, prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
          totals, partial, sent
vars == <<pc, prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
          totals, partial, sent>>
Days == 1..MaxDay
Min(a, b) == IF a < b THEN a ELSE b

Init ==
  /\ pc = "asked" /\ prov = "unset" /\ start = 0 /\ end = 0 /\ sentEnd = 0 /\ moved = FALSE
  /\ cov = "none" /\ fetchedLast = 0 /\ shownLast = 0 /\ reply = "none" /\ status = "none"
  /\ totals = "none" /\ partial = FALSE /\ sent = 0

\* The agent names a source and 2 dates.
Ask ==
  /\ pc = "asked" /\ pc' = "named"
  /\ prov' \in {"gsc", "ga4"} /\ start' \in Days /\ end' \in Days
  /\ UNCHANGED <<sentEnd, moved, cov, fetchedLast, shownLast, reply, status, totals, partial, sent>>
Same == UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, status,
                    totals, partial, sent>>
\* params.period: start_date after end_date is BadInput for every source, before any source runs.
DatesBackwards == pc = "named" /\ start > end /\ pc' = "paramError" /\ Same
DatesInOrder == pc = "named" /\ start <= end /\ pc' = "checked" /\ Same
\* gsc._newest_end, ga4._newest_end: a start past the newest day raises too_late before the token.
StartTooLate == pc = "checked" /\ start > Newest /\ pc' = "tooLate" /\ Same
\* An end past the newest day goes out as the newest day, and the result gets date_moved.
\* ga4._newest_end sends an end on the newest day as today, so that end also gets date_moved.
EndSet ==
  /\ pc = "checked" /\ start <= Newest /\ pc' = "endSet"
  /\ sentEnd' = Min(end, Newest) /\ moved' = (end > Newest \/ (prov = "ga4" /\ end = Newest))
  /\ UNCHANGED <<prov, start, end, cov, fetchedLast, shownLast, reply, status, totals, partial, sent>>
\* gsc._last_complete: the coverage read (its own saved entry, 5 minutes) gives
\* firstIncompleteDate minus 1. It is the value of the API now.
CoverageFromApi ==
  /\ pc = "endSet" /\ prov = "gsc" /\ pc' = "covered" /\ cov' = "api" /\ shownLast' = Last
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, fetchedLast, reply, status, totals, partial, sent>>
\* The coverage read fails or has no value (Search Console), or the source is GA4 (3daysAgo):
\* an estimate, which the code takes again from the day of the oldest answer (answer_day).
CoverageEstimate ==
  /\ pc = "endSet" /\ pc' = "covered" /\ cov' = "estimate"
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, fetchedLast, shownLast, reply, status, totals, partial, sent>>
\* shared.once: the report goes out, and the answer holds the days as the API has them now.
SendReport ==
  /\ pc = "covered" /\ pc' = "flight" /\ sent' = sent + 1 /\ fetchedLast' = Last
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, shownLast, reply, status, totals, partial>>
\* shared.once: a saved answer of the same body, from before the API moved its last complete day.
\* Another source or agent read the coverage first, so the 2 entries end at different times.
SavedReport ==
  /\ pc = "covered" /\ pc' = "answered" /\ fetchedLast' = Last - 1
  /\ reply' \in {"rows", "noRows"}
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, shownLast, status, totals, partial, sent>>
\* An HTTP 200. noRows has a documented field and no row, such as responseAggregationType or kind.
Answer ==
  /\ pc = "flight" /\ pc' = "answered" /\ reply' \in {"rows", "noRows", "emptyObject", "otherText"}
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, status, totals, partial, sent>>
\* gsc.REPORT = shared.documented passes {}. ga4.REPORT = shared.holds("kind") does not.
Good(r, p) == r \in {"rows", "noRows"} \/ (r = "emptyObject" /\ p = "gsc")
BadShape ==
  /\ pc = "answered" /\ ~Good(reply, prov) /\ pc' = "result" /\ status' = "error"
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, shownLast, reply, totals, partial, sent>>
\* gsc._report: totals default to clicks 0 and impressions 0 when the totals query has no row.
\* ga4._report: GA4 leaves out totals with no sessions, and the code shows {}.
\* _google.coverage: partial_days runs from the day after the last complete day to the end.
Result ==
  /\ pc = "answered" /\ Good(reply, prov) /\ pc' = "result" /\ status' = "ok"
  /\ shownLast' = IF cov = "api" THEN shownLast ELSE fetchedLast - 1
  /\ totals' = IF reply = "rows" THEN "value" ELSE IF prov = "gsc" THEN "zero" ELSE "none"
  /\ partial' = (sentEnd > shownLast')
  /\ UNCHANGED <<prov, start, end, sentEnd, moved, cov, fetchedLast, reply, sent>>
Done == pc \in {"paramError", "tooLate", "result"} /\ UNCHANGED vars

Next ==
  Ask \/ DatesBackwards \/ DatesInOrder \/ StartTooLate \/ EndSet \/ CoverageFromApi
  \/ CoverageEstimate \/ SendReport \/ SavedReport \/ Answer \/ BadShape \/ Result \/ Done

Spec == Init /\ [][Next]_vars
Ok == pc = "result" /\ status = "ok"

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
====
