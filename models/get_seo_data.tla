---- MODULE get_seo_data ----
\* 1 call of core.get_seo_data with 2 sources. "One" is the source that the chart follows.
\* "Other" is any other source of the call, and it runs at the same time.
EXTENDS Naturals
VARIABLES pc, asked, st, calls, filtered, scrubbed, saved, linked
vars == <<pc, asked, st, calls, filtered, scrubbed, saved, linked>>
Sources == {"One", "Other"}
Final == {"ok", "thin", "error", "notConnected", "unsupported"}
Stops == {"siteError", "paramError", "sourceError"}

Init ==
  /\ pc = "asked" /\ asked \in BOOLEAN
  /\ st = [s \in Sources |-> "idle"]
  /\ calls = [s \in Sources |-> 0]
  /\ filtered = [s \in Sources |-> FALSE]
  /\ scrubbed = FALSE
  /\ saved \in [Sources -> BOOLEAN]
  \* connection.connected: each field and each secret reference of the provider is there.
  /\ linked \in [Sources -> BOOLEAN]

Keep == UNCHANGED <<asked, st, calls, filtered, scrubbed, saved, linked>>
SiteFound == pc = "asked" /\ pc' = "site" /\ Keep
SiteBad == pc = "asked" /\ pc' = "siteError" /\ Keep
ParamsGood == pc = "site" /\ pc' = "params" /\ Keep
ParamsBad == pc = "site" /\ pc' = "paramError" /\ Keep
SourcesKnown == pc = "params" /\ pc' = "sources" /\ Keep
SourcesUnknown == pc = "params" /\ pc' = "sourceError" /\ Keep

\* core._asked, 1 job for each source. Each job checks only its own source. The gate in
\* providers/__init__.py runs first: a source that is not connected never runs.
Start ==
  /\ pc = "sources"
  /\ \E accepts \in [Sources -> BOOLEAN]:
       /\ asked \/ (\A s \in Sources: accepts[s])
       /\ st' = [s \in Sources |->
                   IF ~linked[s] THEN "notConnected"
                   ELSE IF accepts[s] THEN "running" ELSE "unsupported"]
  /\ pc' = "running"
  /\ UNCHANGED <<asked, calls, filtered, scrubbed, saved, linked>>

\* clarity.clarity raises Unsupported for last_days over 3, before any request.
OutOfRange(s) ==
  /\ asked /\ pc = "running" /\ st[s] = "running"
  /\ st' = [st EXCEPT ![s] = "unsupported"]
  /\ UNCHANGED <<pc, asked, calls, filtered, scrubbed, saved, linked>>
NoFieldOrKey(s) ==
  /\ pc = "running" /\ st[s] = "running"
  /\ st' = [st EXCEPT ![s] = "notConnected"]
  /\ UNCHANGED <<pc, asked, calls, filtered, scrubbed, saved, linked>>
RefusedLocally(s) ==
  /\ pc = "running" /\ st[s] = "running"
  /\ st' = [st EXCEPT ![s] = "error"]
  /\ UNCHANGED <<pc, asked, calls, filtered, scrubbed, saved, linked>>
\* shared.once: the source of a saved answer filters it as a live one, with no request.
SavedRows(s) ==
  /\ pc = "running" /\ st[s] = "running" /\ saved[s]
  /\ st' = [st EXCEPT ![s] = "ok"]
  /\ filtered' = [filtered EXCEPT ![s] = asked]
  /\ UNCHANGED <<pc, asked, calls, scrubbed, saved, linked>>
Request(s) ==
  /\ pc = "running" /\ st[s] = "running" /\ ~saved[s]
  /\ st' = [st EXCEPT ![s] = "called"]
  /\ calls' = [calls EXCEPT ![s] = @ + 1]
  /\ UNCHANGED <<pc, asked, filtered, scrubbed, saved, linked>>
Rows(s) ==
  /\ pc = "running" /\ st[s] = "called"
  /\ st' = [st EXCEPT ![s] = "ok"]
  /\ filtered' = [filtered EXCEPT ![s] = asked]
  /\ UNCHANGED <<pc, asked, calls, scrubbed, saved, linked>>
ThinRows(s) ==
  /\ pc = "running" /\ st[s] = "called"
  /\ st' = [st EXCEPT ![s] = "thin"]
  /\ filtered' = [filtered EXCEPT ![s] = asked]
  /\ UNCHANGED <<pc, asked, calls, scrubbed, saved, linked>>
ApiError(s) ==
  /\ pc = "running" /\ st[s] = "called"
  /\ st' = [st EXCEPT ![s] = "error"]
  /\ UNCHANGED <<pc, asked, calls, filtered, scrubbed, saved, linked>>
TimesOut(s) ==
  /\ pc = "running" /\ st[s] = "called"
  /\ st' = [st EXCEPT ![s] = "error"]
  /\ UNCHANGED <<pc, asked, calls, filtered, scrubbed, saved, linked>>

AllDone ==
  /\ pc = "running" /\ \A s \in Sources: st[s] \in Final
  /\ pc' = "gathered" /\ Keep
Scrub ==
  /\ pc = "gathered" /\ pc' = "result" /\ scrubbed' = TRUE
  /\ UNCHANGED <<asked, st, calls, filtered, saved, linked>>

Done == pc \in (Stops \cup {"result"}) /\ UNCHANGED vars

Next ==
  \/ SiteFound \/ SiteBad \/ ParamsGood \/ ParamsBad \/ SourcesKnown \/ SourcesUnknown
  \/ Start \/ AllDone \/ Scrub \/ Done
  \/ \E s \in Sources:
       OutOfRange(s) \/ NoFieldOrKey(s) \/ RefusedLocally(s) \/ SavedRows(s) \/ Request(s)
       \/ Rows(s) \/ ThinRows(s) \/ ApiError(s) \/ TimesOut(s)

Spec == Init /\ [][Next]_vars

\* A call never reaches an API when a parameter is unsupported.
UnsupportedReachesNoApi == \A s \in Sources: st[s] = "unsupported" => calls[s] = 0
\* A filtered result is never unfiltered data.
FilteredResultIsFiltered == \A s \in Sources: (asked /\ st[s] \in {"ok", "thin"}) => filtered[s]
\* A failed source does not stop the others.
FailedSourceStopsNoOther == pc = "result" => \A s \in Sources: st[s] \in Final
\* Code claim: a bad site, parameter, or source name reaches no API.
BadInputReachesNoApi == pc \in Stops => \A s \in Sources: calls[s] = 0
\* A source with a saved answer reaches no API.
SavedAnswerReachesNoApi == \A s \in Sources: saved[s] => calls[s] = 0
\* A source that is not connected reaches no API and gives no rows, saved or live.
NotConnectedReachesNoApi ==
  \A s \in Sources: ~linked[s] => calls[s] = 0 /\ st[s] \notin {"ok", "thin"}
\* Code claim: a result holds no secret.
ResultIsScrubbed == pc = "result" => scrubbed
====
