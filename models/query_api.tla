---- MODULE query_api ----
\* 1 call of core.query_api. The input choice is at each branch.
\* It holds the date check of a dated method (gsc._search_analytics, ga4._sent_range,
\* clarity._check_date) and the shape check of an HTTP 200 (shared.once with good=).
EXTENDS Naturals
VARIABLES pc, api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply
vars == <<pc, api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply>>
Google == {"gsc", "ga4"}
Discovery == {"catalog", "taskLookup", "recipeRows", "noTask", "fields"}
Ends == {"catalog", "recipeRows", "noTask", "fields", "inputError", "rows", "apiMessage", "fixedError"}
\* The dates of the request. "startAfterEndRelative": GA4 only, at least 1 date is relative
\* (today, yesterday, NdaysAgo), and the start is after the end in every zone.
DateKinds == {"none", "inRange", "futureEnd", "startPastNewest", "startAfterEndIso", "startAfterEndRelative"}
StartAfterEnd == {"startPastNewest", "startAfterEndIso", "startAfterEndRelative"}

Init ==
  \* The api, the saved state, and the dates are chosen at the step that reads them, so that
  \* the discovery states do not repeat for each later choice.
  /\ pc = "asked" /\ api = "unset" /\ sent = 0 /\ wrote = 0 /\ scrubbed = FALSE
  /\ saved = "unknown" /\ fromCache = FALSE
  /\ dates = "unset" /\ moved = FALSE /\ reply = "none"

Keep == UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply>>
TaskWithMethod == pc = "asked" /\ pc' = "inputError" /\ Keep
NoApiNoTask == pc = "asked" /\ pc' = "catalog" /\ Keep
TaskAsked == pc = "asked" /\ pc' = "taskLookup" /\ Keep
ApiNamed ==
  /\ pc = "asked" /\ pc' = "apiKnown" /\ api' \in {"gsc", "ga4", "other"}
  /\ UNCHANGED <<sent, wrote, scrubbed, saved, fromCache, dates, moved, reply>>
ApiUnknown == pc = "asked" /\ pc' = "inputError" /\ Keep
TaskOnApi == pc = "apiKnown" /\ pc' = "taskLookup" /\ Keep
TaskMatches == pc = "taskLookup" /\ pc' = "recipeRows" /\ Keep
TaskNoMatch == pc = "taskLookup" /\ pc' = "noTask" /\ Keep
NoMethod == pc = "apiKnown" /\ pc' = "fields" /\ Keep
MethodNamed == pc = "apiKnown" /\ pc' = "methodKnown" /\ Keep
MethodUnknown == pc = "apiKnown" /\ pc' = "inputError" /\ Keep
FieldWrong == pc = "methodKnown" /\ pc' = "inputError" /\ Keep
FieldsGood ==
  /\ pc = "methodKnown" /\ pc' = "siteKnown"
  /\ saved' \in {"none", "fresh", "expired", "failedInWindow"}
  /\ UNCHANGED <<api, sent, wrote, scrubbed, fromCache, dates, moved, reply>>
SiteBad == pc = "methodKnown" /\ pc' = "inputError" /\ Keep
ValueWrong == pc = "siteKnown" /\ pc' = "inputError" /\ Keep
\* params.period, gsc._newest_end, ga4._newest_end, clarity._check_date: 2 YYYY-MM-DD dates in the
\* wrong order, or a start past the newest day, raise BadInput before the token and the request.
DatesRefused ==
  /\ pc = "siteKnown" /\ dates' \in {"startPastNewest", "startAfterEndIso"}
  /\ pc' = "inputError"
  /\ UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache, moved, reply>>
\* ga4._newest_end compares only 2 YYYY-MM-DD dates: a relative date passes with no check.
\* A future end goes out as the newest day, and the result has date_moved.
DatesPass ==
  /\ pc = "siteKnown"
  /\ dates' \in {"none", "inRange", "futureEnd", "startAfterEndRelative"}
  /\ (dates' = "startAfterEndRelative" => api = "ga4")
  /\ pc' = "datesSet" /\ moved' = (dates' = "futureEnd")
  /\ UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache, reply>>
LocalRefusal == pc = "datesSet" /\ pc' = "inputError" /\ Keep
\* shared.once: a saved answer of the same request, inside its stated time. _saved checks the shape.
SavedAnswer ==
  /\ pc = "datesSet" /\ saved = "fresh" /\ pc' = "rows" /\ fromCache' = TRUE /\ scrubbed' = TRUE
  /\ reply' = "data"
  /\ UNCHANGED <<api, sent, wrote, saved, dates, moved>>
SharedFailure ==
  /\ pc = "datesSet" /\ saved = "failedInWindow" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply>>
ReadQuery ==
  /\ pc = "datesSet" /\ saved \in {"none", "expired"} /\ pc' = "flight" /\ sent' = sent + 1
  /\ UNCHANGED <<api, wrote, scrubbed, saved, fromCache, dates, moved, reply>>
\* An HTTP 200 with a documented field: rows, or a documented empty such as {"kind": ...}.
Rows ==
  /\ pc = "flight" /\ pc' = "rows" /\ scrubbed' = TRUE /\ reply' \in {"data", "noData"}
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved>>
\* shared.documented passes {}: Search Console search_analytics, sitemaps, sitemap, GA4 metadata.
EmptyObjectPasses ==
  /\ pc = "flight" /\ api \in Google /\ pc' = "rows" /\ scrubbed' = TRUE /\ reply' = "emptyObject"
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved>>
\* shared.holds, the GA4 report check (kind), and the url_inspection check (inspectionResult) refuse {}:
\* net.BAD_SHAPE, and the window starts.
EmptyObjectFails ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE /\ reply' = "emptyObject"
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved>>
OtherTextFails ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE /\ reply' = "otherText"
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved>>
\* net.request(explain=True) reads error.message only for a Google API on HTTP 400.
Google400 ==
  /\ pc = "flight" /\ api \in Google /\ pc' = "apiMessage" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply>>
Other400 ==
  /\ pc = "flight" /\ api = "other" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply>>
OtherFailure ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply>>
Done == pc \in Ends /\ UNCHANGED vars

Next ==
  TaskWithMethod \/ NoApiNoTask \/ TaskAsked \/ ApiNamed \/ ApiUnknown \/ TaskOnApi
  \/ TaskMatches \/ TaskNoMatch \/ NoMethod \/ MethodNamed \/ MethodUnknown
  \/ FieldWrong \/ FieldsGood \/ SiteBad \/ ValueWrong \/ DatesRefused \/ DatesPass
  \/ LocalRefusal \/ SavedAnswer \/ SharedFailure \/ ReadQuery
  \/ Rows \/ EmptyObjectPasses \/ EmptyObjectFails \/ OtherTextFails
  \/ Google400 \/ Other400 \/ OtherFailure \/ Done

Spec == Init /\ [][Next]_vars

\* Discovery makes no network call.
DiscoveryMakesNoNetworkCall == pc \in Discovery => sent = 0
\* Code claim: a wrong field, method, or value is an error, and reaches no API.
WrongInputReachesNoApi == pc = "inputError" => sent = 0
\* An ask that matches no task gets unsupported, and the server guesses no call.
NoMatchGuessesNoCall == pc = "noTask" => sent = 0
\* Each call is read-only.
EachCallIsReadOnly == wrote = 0
\* A call sends at most 1 read query.
AtMostOneQuery == sent <= 1
\* A saved answer sends nothing.
SavedAnswerSendsNothing == fromCache => sent = 0
\* The server never serves a saved answer past its stated time.
NoAnswerPastItsTime == fromCache => saved = "fresh"
\* A repeat of a failed request inside the window spends nothing.
RepeatInWindowSpendsNothing == saved = "failedInWindow" => sent = 0
\* Code claim: only a Google API puts its own text in an error.
OnlyGoogleErrorText == pc = "apiMessage" => api \in Google
\* Code claim: an answer from an API holds no secret.
ApiAnswerIsScrubbed == pc \in {"rows", "apiMessage", "fixedError"} => scrubbed
\* George: a start after the end sends no request.
StartAfterEndSendsNothing == dates \in StartAfterEnd => sent = 0
\* George: a future end moves to the newest day, and the result says so.
FutureEndMovesAndSaysSo == (pc = "rows" /\ dates = "futureEnd") => moved
\* George: an answer with no documented field is never ok.
NoDocumentedFieldIsNeverOk == pc = "rows" => reply \in {"data", "noData"}
====
