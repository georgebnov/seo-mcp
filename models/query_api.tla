---- MODULE query_api ----
\* 1 call of core.query_api. The input choice is at each branch.
\* It holds the date check of a dated method (gsc._search_analytics, ga4._sent_range for
\* dateRanges and each cohort dateRange, clarity._check_date) and the shape check of an HTTP 200 (shared.once with good=).
EXTENDS Naturals
VARIABLES pc, api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply, read, covRead
vars == <<pc, api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply, read, covRead>>
Google == {"gsc", "ga4"}
Discovery == {"catalog", "taskLookup", "recipeRows", "noTask", "fields"}
Ends == {"catalog", "recipeRows", "noTask", "fields", "inputError", "rows", "apiMessage", "fixedError"}
\* The dates of the request. "startAfterEndRelative": GA4 only, at least 1 date is relative
\* (today, yesterday, NdaysAgo), and the start is after the end once both are plain dates.
DateKinds == {"none", "inRange", "futureEnd", "startPastNewest", "startAfterEndIso", "startAfterEndRelative"}
StartAfterEnd == {"startPastNewest", "startAfterEndIso", "startAfterEndRelative"}

Init ==
  \* The api, the saved state, and the dates are chosen at the step that reads them, so that
  \* the discovery states do not repeat for each later choice.
  /\ pc = "asked" /\ api = "unset" /\ sent = 0 /\ wrote = 0 /\ scrubbed = FALSE
  /\ saved = "unknown" /\ fromCache = FALSE
  /\ dates = "unset" /\ moved = FALSE /\ reply = "none" /\ read = "unset" /\ covRead = 0

Keep == UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply, read, covRead>>
TaskWithMethod == pc = "asked" /\ pc' = "inputError" /\ Keep
NoApiNoTask == pc = "asked" /\ pc' = "catalog" /\ Keep
TaskAsked == pc = "asked" /\ pc' = "taskLookup" /\ Keep
ApiNamed ==
  /\ pc = "asked" /\ pc' = "apiKnown" /\ api' \in {"gsc", "ga4", "other"}
  /\ UNCHANGED <<sent, wrote, scrubbed, saved, fromCache, dates, moved, reply, read, covRead>>
ApiUnknown == pc = "asked" /\ pc' = "inputError" /\ Keep
TaskOnApi == pc = "apiKnown" /\ pc' = "taskLookup" /\ Keep
TaskMatches == pc = "taskLookup" /\ pc' = "recipeRows" /\ Keep
TaskNoMatch == pc = "taskLookup" /\ pc' = "noTask" /\ Keep
\* core.query_api: a start_date with no task. tasks._length: a start_date after the end of the
\* task, past the recording retention of a click task, or on a task with no dates.
StartWithNoTask == pc = "asked" /\ pc' = "inputError" /\ Keep
TaskStartRefused == pc = "taskLookup" /\ pc' = "inputError" /\ Keep
NoMethod == pc = "apiKnown" /\ pc' = "fields" /\ Keep
MethodNamed == pc = "apiKnown" /\ pc' = "methodKnown" /\ Keep
MethodUnknown == pc = "apiKnown" /\ pc' = "inputError" /\ Keep
FieldWrong == pc = "methodKnown" /\ pc' = "inputError" /\ Keep
FieldsGood ==
  /\ pc = "methodKnown" /\ pc' = "siteKnown"
  /\ saved' \in {"none", "fresh", "expired", "failedInWindow"}
  /\ UNCHANGED <<api, sent, wrote, scrubbed, fromCache, dates, moved, reply, read, covRead>>
SiteBad == pc = "methodKnown" /\ pc' = "inputError" /\ Keep
ValueWrong == pc = "siteKnown" /\ pc' = "inputError" /\ Keep
\* params.period, gsc._newest_end, ga4._sent_dates, clarity._check_date: 2 YYYY-MM-DD dates in the
\* wrong order, or a start past the newest day, raise BadInput before the token and the request.
\* ga4._sent_dates turns a relative GA4 date into a plain date of params.today first.
DatesRefused ==
  /\ pc = "siteKnown" /\ dates' \in StartAfterEnd
  /\ (dates' = "startAfterEndRelative" => api = "ga4")
  /\ pc' = "inputError"
  /\ UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache, moved, reply, read, covRead>>
\* A valid range goes out as plain dates. A future end goes out as the newest day (today minus 1
\* for Search Console and GA4), and the result has date_moved.
DatesPass ==
  /\ pc = "siteKnown"
  /\ dates' \in {"none", "inRange", "futureEnd"}
  /\ pc' = "datesSet" /\ moved' = (dates' = "futureEnd")
  /\ UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache, reply, read, covRead>>
\* search_analytics has no ledger, so a ledger refusal comes with no coverage read.
LocalRefusal == pc = "datesSet" /\ covRead = 0 /\ pc' = "inputError" /\ Keep
\* gsc._search_analytics: the coverage read of get_seo_data (gsc._last_complete) goes out first
\* when its own saved entry is gone. It has no ledger, and a failure gives the estimate, so it
\* ends no call. Its day is in the key of the query, so it comes before the saved answer.
CoverageRead ==
  /\ pc = "datesSet" /\ api = "gsc" /\ covRead = 0 /\ covRead' = 1
  /\ UNCHANGED <<pc, api, sent, wrote, scrubbed, saved, fromCache, dates, moved, reply, read>>
\* shared.once: a saved answer of the same request, inside its stated time. _saved checks the shape.
SavedAnswer ==
  /\ pc = "datesSet" /\ saved = "fresh" /\ pc' = "rows" /\ fromCache' = TRUE /\ scrubbed' = TRUE
  /\ reply' = "data"
  /\ UNCHANGED <<api, sent, wrote, saved, dates, moved, read, covRead>>
SharedFailure ==
  /\ pc = "datesSet" /\ saved = "failedInWindow" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply, read, covRead>>
\* The read that goes out. Only Search Console has the 2 sitemap reads.
ReadQuery ==
  /\ pc = "datesSet" /\ saved \in {"none", "expired"} /\ pc' = "flight" /\ sent' = sent + 1
  /\ read' \in IF api = "gsc" /\ covRead = 0 THEN {"sitemapList", "sitemapGet", "otherRead"}
               ELSE {"otherRead"}
  /\ UNCHANGED <<api, wrote, scrubbed, saved, fromCache, dates, moved, reply, covRead>>
\* An HTTP 200 with a documented field: rows, or a documented empty such as {"kind": ...}.
\* gsc.SITEMAP needs path, so 1 sitemap is always data.
Rows ==
  /\ pc = "flight" /\ pc' = "rows" /\ scrubbed' = TRUE
  /\ reply' \in IF read = "sitemapGet" THEN {"data"} ELSE {"data", "noData"}
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, read, covRead>>
\* Search Console sitemaps (gsc.SITEMAP_LIST, shared.documented) passes {}: Google leaves out an
\* empty sitemap list. {} is its documented empty.
EmptySitemapIsNoData ==
  /\ pc = "flight" /\ read = "sitemapList" /\ pc' = "rows" /\ scrubbed' = TRUE /\ reply' = "noData"
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, read, covRead>>
\* shared.holds and shared.has_any refuse {}: Search Console search_analytics, url_inspection, and
\* sitemap, GA4 reports and metadata. net.BAD_SHAPE, and the window starts.
EmptyObjectFails ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE /\ reply' = "emptyObject"
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, read, covRead>>
OtherTextFails ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE /\ reply' = "otherText"
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, read, covRead>>
\* net.request(explain=True) reads error.message only for a Google API on HTTP 400.
Google400 ==
  /\ pc = "flight" /\ api \in Google /\ pc' = "apiMessage" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply, read, covRead>>
Other400 ==
  /\ pc = "flight" /\ api = "other" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply, read, covRead>>
OtherFailure ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache, dates, moved, reply, read, covRead>>
Done == pc \in Ends /\ UNCHANGED vars

Next ==
  TaskWithMethod \/ NoApiNoTask \/ TaskAsked \/ ApiNamed \/ ApiUnknown \/ TaskOnApi
  \/ TaskMatches \/ TaskNoMatch \/ StartWithNoTask \/ TaskStartRefused \/ NoMethod \/ MethodNamed \/ MethodUnknown
  \/ FieldWrong \/ FieldsGood \/ SiteBad \/ ValueWrong \/ DatesRefused \/ DatesPass
  \/ LocalRefusal \/ CoverageRead \/ SavedAnswer \/ SharedFailure \/ ReadQuery
  \/ Rows \/ EmptySitemapIsNoData \/ EmptyObjectFails \/ OtherTextFails
  \/ Google400 \/ Other400 \/ OtherFailure \/ Done

Spec == Init /\ [][Next]_vars

\* Discovery makes no network call.
DiscoveryMakesNoNetworkCall == pc \in Discovery => sent + covRead = 0
\* Code claim: a wrong field, method, or value is an error, and reaches no API.
WrongInputReachesNoApi == pc = "inputError" => sent + covRead = 0
\* An ask that matches no task gets unsupported, and the server guesses no call.
NoMatchGuessesNoCall == pc = "noTask" => sent + covRead = 0
\* Each call is read-only.
EachCallIsReadOnly == wrote = 0
\* A call sends at most 1 read query.
AtMostOneQuery == sent <= 1
\* The 2 tools give the same Search Console days the same answer, so search_analytics
\* reads the coverage of get_seo_data. A call sends at most 1 such read, only to Search Console.
AtMostOneCoverageRead == covRead <= 1 /\ (covRead = 1 => api = "gsc")
\* A saved answer sends no query. The coverage read has its own saved entry.
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
StartAfterEndSendsNothing == dates \in StartAfterEnd => sent + covRead = 0
\* George: a future end moves to the newest day, and the result says so.
FutureEndMovesAndSaysSo == (pc = "rows" /\ dates = "futureEnd") => moved
\* George: an answer with no documented field is never ok.
NoDocumentedFieldIsNeverOk == pc = "rows" => reply \in {"data", "noData"}
\* George: a sitemap that does not exist is HTTP 404, so 1 sitemap of {} is never ok.
EmptySitemapRecordIsNeverOk == (pc = "rows" /\ read = "sitemapGet") => reply = "data"
====
