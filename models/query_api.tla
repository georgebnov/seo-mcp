---- MODULE query_api ----
\* 1 call of core.query_api. The input choice is at each branch.
EXTENDS Naturals
VARIABLES pc, api, sent, wrote, scrubbed
vars == <<pc, api, sent, wrote, scrubbed>>
Discovery == {"catalog", "taskLookup", "recipeRows", "noTask", "fields"}
NoNetwork == Discovery \cup {"asked", "apiKnown", "methodKnown", "siteKnown", "inputError"}
Ends == {"catalog", "recipeRows", "noTask", "fields", "inputError", "rows", "apiMessage", "fixedError"}

Init == pc = "asked" /\ api \in {"google", "other"} /\ sent = 0 /\ wrote = 0 /\ scrubbed = FALSE

Keep == UNCHANGED <<api, sent, wrote, scrubbed>>
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
\* net._check lets only a listed read endpoint through.
ReadQuery ==
  /\ pc = "siteKnown" /\ pc' = "flight" /\ sent' = sent + 1
  /\ UNCHANGED <<api, wrote, scrubbed>>
Rows == pc = "flight" /\ pc' = "rows" /\ scrubbed' = TRUE /\ UNCHANGED <<api, sent, wrote>>
\* net.request(explain=True) reads error.message only for a Google API on HTTP 400.
Google400 ==
  /\ pc = "flight" /\ api = "google" /\ pc' = "apiMessage" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote>>
Other400 ==
  /\ pc = "flight" /\ api = "other" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote>>
OtherFailure ==
  /\ pc = "flight" /\ pc' = "fixedError" /\ scrubbed' = TRUE
  /\ UNCHANGED <<api, sent, wrote>>
Done == pc \in Ends /\ UNCHANGED vars

Next ==
  TaskWithMethod \/ NoApiNoTask \/ TaskAsked \/ ApiNamed \/ ApiUnknown \/ TaskOnApi
  \/ TaskMatches \/ TaskNoMatch \/ NoMethod \/ MethodNamed \/ MethodUnknown
  \/ FieldWrong \/ FieldsGood \/ SiteBad \/ ValueWrong \/ LocalRefusal \/ ReadQuery
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
\* Code claim: only a Google API puts its own text in an error.
OnlyGoogleErrorText == pc = "apiMessage" => api = "google"
\* Code claim: an answer from an API holds no secret.
ApiAnswerIsScrubbed == pc \in {"rows", "apiMessage", "fixedError"} => scrubbed
====
