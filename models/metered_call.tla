---- MODULE metered_call ----
\* 1 metered Clarity call through clarity._cached, quota.spend, and net.request.
\* The start state is any ledger and cache that earlier calls can leave.
EXTENDS Naturals
CONSTANT Limit
VARIABLES pc, api, cache, ledger, blocked, count, sent, count0, sent0, blocked0, receipt
vars == <<pc, api, cache, ledger, blocked, count, sent, count0, sent0, blocked0, receipt>>

Ends == {"hit", "repeated", "badAnswer", "notConnected", "refused", "stored", "failed", "dayBlocked"}

Init ==
  /\ pc = "start"
  /\ api \in {"export", "mcp"}
  /\ cache \in {"today", "olderDay", "none", "failedInWindow", "failedBefore", "badToday"}
  /\ ledger \in {"good", "olderDay", "brokenText", "wrongDate"}
  /\ sent \in 0..Limit
  /\ blocked \in BOOLEAN
  \* A ledger of an older day counts nothing that went out today.
  /\ (ledger = "olderDay") => (sent = 0 /\ blocked = FALSE)
  \* A broken text has no readable flag.
  /\ (ledger = "brokenText") => blocked = FALSE
  /\ count = sent
  /\ count0 = count /\ sent0 = sent /\ blocked0 = blocked
  /\ receipt = FALSE

Same == UNCHANGED <<api, cache, ledger, blocked, count, sent, count0, sent0, blocked0, receipt>>

TakeLock == pc = "start" /\ pc' = "lock" /\ Same
CacheHit == pc = "lock" /\ cache = "today" /\ pc' = "hit" /\ Same
CacheOld == pc = "lock" /\ cache = "olderDay" /\ pc' = "token" /\ Same
CacheNone == pc = "lock" /\ cache = "none" /\ pc' = "token" /\ Same
\* clarity._recent_failure: the failure of the same request is under the window.
\* clarity._read_cache: an answer of today that an older server saved fails the shape check.
CacheBad == pc = "lock" /\ cache = "badToday" /\ pc' = "token" /\ Same
FailureRepeat == pc = "lock" /\ cache = "failedInWindow" /\ pc' = "repeated" /\ Same
FailureOld == pc = "lock" /\ cache = "failedBefore" /\ pc' = "token" /\ Same
TokenMissing == pc = "token" /\ pc' = "notConnected" /\ Same
TokenRead == pc = "token" /\ pc' = "ledger" /\ Same

\* quota._read raises Refused on text that does not parse, or on a bad count.
RefuseBroken == pc = "ledger" /\ ledger = "brokenText" /\ pc' = "refused" /\ Same
\* quota.spend: a good ledger of today.
RefuseBlocked == pc = "ledger" /\ ledger = "good" /\ blocked /\ pc' = "refused" /\ Same
RefuseLimit ==
  /\ pc = "ledger" /\ ledger = "good" /\ ~blocked /\ count + 1 > Limit
  /\ pc' = "refused" /\ Same
Reserve ==
  /\ pc = "ledger" /\ ledger = "good" /\ ~blocked /\ count + 1 <= Limit
  /\ pc' = "reserved" /\ count' = count + 1 /\ receipt' = TRUE
  /\ UNCHANGED <<api, cache, ledger, blocked, sent, count0, sent0, blocked0>>
\* quota._read: a real older day restarts the count.
ReserveNewDay ==
  /\ pc = "ledger" /\ ledger = "olderDay"
  /\ pc' = "reserved" /\ count' = 1 /\ receipt' = TRUE
  /\ UNCHANGED <<api, cache, ledger, blocked, sent, count0, sent0, blocked0>>
\* quota._read: a day that is no real date, or is after today, is a broken ledger.
RefuseWrongDate == pc = "ledger" /\ ledger = "wrongDate" /\ pc' = "refused" /\ Same

\* net._pay needs the receipt, and only then the request can go out.
Send ==
  /\ pc = "reserved" /\ receipt
  /\ pc' = "flight" /\ sent' = sent + 1
  /\ UNCHANGED <<api, cache, ledger, blocked, count, count0, sent0, blocked0, receipt>>
ConnectFails == pc = "reserved" /\ pc' = "failed" /\ Same

Answer ==
  /\ pc = "flight" /\ pc' = "stored" /\ cache' = "today"
  /\ UNCHANGED <<api, ledger, blocked, count, sent, count0, sent0, blocked0, receipt>>
\* clarity._cached: HTTP 200 with a wrong shape, so the failure window starts and nothing is saved.
BadShape ==
  /\ pc = "flight" /\ pc' = "badAnswer" /\ cache' = "failedInWindow"
  /\ UNCHANGED <<api, ledger, blocked, count, sent, count0, sent0, blocked0, receipt>>
Export429 ==
  /\ pc = "flight" /\ api = "export" /\ pc' = "dayBlocked" /\ blocked' = TRUE
  /\ UNCHANGED <<api, cache, ledger, count, sent, count0, sent0, blocked0, receipt>>
Mcp429 == pc = "flight" /\ api = "mcp" /\ pc' = "failed" /\ Same
OtherError == pc = "flight" /\ pc' = "failed" /\ Same
TimesOut == pc = "flight" /\ pc' = "failed" /\ Same

Done == pc \in Ends /\ UNCHANGED vars

Next ==
  TakeLock \/ CacheHit \/ CacheOld \/ CacheNone \/ CacheBad \/ FailureRepeat \/ FailureOld \/ TokenMissing \/ TokenRead
  \/ RefuseBroken \/ RefuseBlocked \/ RefuseLimit \/ Reserve \/ ReserveNewDay
  \/ RefuseWrongDate \/ Send \/ ConnectFails \/ Answer \/ BadShape \/ Export429 \/ Mcp429
  \/ OtherError \/ TimesOut \/ Done

Spec == Init /\ [][Next]_vars

\* A cache hit spends no quota.
CacheHitSpendsNothing == pc = "hit" => (count = count0 /\ sent = sent0)
\* A repeat of a failed request inside the window spends nothing.
RepeatInWindowSpendsNothing == pc = "repeated" => (count = count0 /\ sent = sent0)
\* A bad HTTP 200 answer is never saved.
BadAnswerIsNotSaved == pc = "badAnswer" => cache # "today"
\* A saved answer that goes back has a good shape.
HitHasAGoodShape == pc = "hit" => cache = "today"
\* The ledger count equals the metered requests sent.
CountEqualsRequestsSent == pc \in Ends => count = sent
\* The server never sends a metered call past the limit.
NeverPastTheLimit == sent <= Limit
\* A broken ledger sends no metered call.
BrokenLedgerSendsNothing == ledger \in {"brokenText", "wrongDate"} => sent = sent0
\* The ledger reserves the call before the network.
ReservedBeforeTheNetwork == sent > sent0 => receipt
\* The day stops for the rest of the day after Clarity says the limit is reached.
BlockedDaySendsNothing == blocked0 => sent = sent0
\* HTTP 429 of the Data Export API sets the blocked flag.
Limit429SetsTheFlag == pc = "dayBlocked" => blocked
====
