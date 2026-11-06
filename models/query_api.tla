---- MODULE query_api ----
\* 1 call of core.query_api. The input choice is at each branch.
EXTENDS Naturals
VARIABLES pc, api, sent, wrote, scrubbed, saved, fromCache
vars == <<pc, api, sent, wrote, scrubbed, saved, fromCache>>
Discovery == {"catalog", "taskLookup", "recipeRows", "noTask", "fields"}
NoNetwork == Discovery \cup {"asked", "apiKnown", "methodKnown", "siteKnown", "inputError"}
Ends == {"catalog", "recipeRows", "noTask", "fields", "inputError", "rows", "apiMessage", "fixedError"}

Init ==
  /\ pc = "asked" /\ api \in {"google", "other"} /\ sent = 0 /\ wrote = 0 /\ scrubbed = FALSE
  /\ saved \in {"none", "fresh", "expired", "failedInWindow"} /\ fromCache = FALSE

Keep == UNCHANGED <<api, sent, wrote, scrubbed, saved, fromCache>>
TaskWithMethod == pc = "asked" /\ pc' = "inputError" /\ Keep
NoApiNoTask == pc = "asked" /\ pc' = "catalog" /\ Keep
TaskAsked == pc = "asked" /\ pc' = "taskLookup" /\ Keep
ApiNamed == pc = "asked" /\ pc' = "apiKnown" /\ Keep
ApiUnknown == pc = "asked" /\ pc' = "inputError" /\ Keep
TaskOnApi == pc = "apiKnown" /\ pc' = "taskLookup" /\ Keep
TaskMatches == pc = "taskLookup" /\ pc' = "recipeRows" /\ Keep
TaskNoMatch == pc = "taskLookup" /\ pc' = "noTask" /\ Keep
NoMethod == pc = "apiKnown" /\ pc' = "fields" /\ Keep
MethodNamed == pc = "apiKnown" /\ pc' = "methodKnown" /\ Keep
MethodUnknown == pc = "apiKnown" /\ pc' = "inputError" /\ Keep
FieldWrong == pc = "methodKnown" /\ pc' = "inputError" /\ Keep
FieldsGood == pc = "methodKnown" /\ pc' = "siteKnown" /\ Keep
SiteBad == pc = "methodKnown" /\ pc' = "inputError" /\ Keep
ValueWrong == pc = "siteKnown" /\ pc' = "inputError" /\ Keep
LocalRefusal == pc = "siteKnown" /\ pc' = "inputError" /\ Keep
\* shared.once: a saved answer of the same request, inside its stated time.
SavedAnswer ==
  /\ pc = "siteKnown" /\ saved = "fresh" /\ pc' = "rows" /\ fromCache' = TRUE /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved>>
\* shared.once: the same request failed inside the window.
SharedFailure ==
  /\ pc = "siteKnown" /\ saved = "failedInWindow" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache>>
\* net.check lets only a listed read endpoint through.
ReadQuery ==
  /\ pc = "siteKnown" /\ saved \in {"none", "expired"} /\ pc' = "flight" /\ sent' = sent + 1
  /\ UNCHANGED <<api, wrote, scrubbed, saved, fromCache>>
Rows ==
  /\ pc = "flight" /\ pc' = "rows" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache>>
\* net.request(explain=True) reads error.message only for a Google API on HTTP 400.
Google400 ==
  /\ pc = "flight" /\ api = "google" /\ pc' = "apiMessage" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache>>
Other400 ==
  /\ pc = "flight" /\ api = "other" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache>>
OtherFailure ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote, saved, fromCache>>
Done == pc \in Ends /\ UNCHANGED vars

Next ==
  TaskWithMethod \/ NoApiNoTask \/ TaskAsked \/ ApiNamed \/ ApiUnknown \/ TaskOnApi
  \/ TaskMatches \/ TaskNoMatch \/ NoMethod \/ MethodNamed \/ MethodUnknown
  \/ FieldWrong \/ FieldsGood \/ SiteBad \/ ValueWrong \/ LocalRefusal \/ SavedAnswer
  \/ SharedFailure \/ ReadQuery
  \/ Rows \/ Google400 \/ Other400 \/ OtherFailure \/ Done

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
OnlyGoogleErrorText == pc = "apiMessage" => api = "google"
\* Code claim: an answer from an API holds no secret.
ApiAnswerIsScrubbed == pc \in {"rows", "apiMessage", "fixedError"} => scrubbed
====
