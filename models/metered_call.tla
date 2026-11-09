---- MODULE metered_call ----
\* 1 metered Clarity call through clarity._cached, quota.spend, and net.request.
\* The start state is any ledger and cache that earlier calls can leave.
\* clarity.credential reads the token before shared.once, because the cache path holds the
\* identity of the token. token is TRUE when the token is there.
EXTENDS Naturals
CONSTANT Limit
VARIABLES pc, api, cache, ledger, blocked, count, sent, count0, sent0, blocked0, receipt, token
vars == <<pc, api, cache, ledger, blocked, count, sent, count0, sent0, blocked0, receipt, token>>

Ends == {"hit", "repeated", "badAnswer", "busy", "notConnected", "refused", "stored", "failed", "notSent", "dayBlocked"}

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
  /\ token \in BOOLEAN

Same == UNCHANGED <<api, cache, ledger, blocked, count, sent, count0, sent0, blocked0, receipt, token>>
\* shared.once marks the failure of a spent call, so a repeat inside the window spends nothing.
Marked == /\ cache' = "failedInWindow"
          /\ UNCHANGED <<api, ledger, blocked, count, sent, count0, sent0, blocked0, receipt, token>>

\* clarity.credential: Site.secret raises NotConnected for a missing or empty token.
TokenMissing == pc = "start" /\ ~token /\ pc' = "notConnected" /\ Same
TokenRead == pc = "start" /\ token /\ pc' = "token" /\ Same
\* shared._only_sender: the lock of the cache path, which holds the identity of the token.
TakeLock == pc = "token" /\ pc' = "lock" /\ Same
CacheHit == pc = "lock" /\ cache = "today" /\ pc' = "hit" /\ Same
CacheOld == pc = "lock" /\ cache = "olderDay" /\ pc' = "ledger" /\ Same
CacheNone == pc = "lock" /\ cache = "none" /\ pc' = "ledger" /\ Same
\* shared._recent_failure: the failure of the same request is under the window.
\* shared._saved: an answer of today that an older server saved fails the shape check.
CacheBad == pc = "lock" /\ cache = "badToday" /\ pc' = "ledger" /\ Same
FailureRepeat == pc = "lock" /\ cache = "failedInWindow" /\ pc' = "repeated" /\ Same
FailureOld == pc = "lock" /\ cache = "failedBefore" /\ pc' = "ledger" /\ Same

\* quota.locked: a stuck process holds the ledger lock past the wait.
LockBusy == pc = "ledger" /\ pc' = "busy" /\ Same
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
  /\ UNCHANGED <<api, cache, ledger, blocked, sent, count0, sent0, blocked0, token>>
\* quota._read: a real older day restarts the count.
ReserveNewDay ==
  /\ pc = "ledger" /\ ledger = "olderDay"
  /\ pc' = "reserved" /\ count' = 1 /\ receipt' = TRUE
  /\ UNCHANGED <<api, cache, ledger, blocked, sent, count0, sent0, blocked0, token>>
\* quota._read: a day that is no real date, or is after today, is a broken ledger.
RefuseWrongDate == pc = "ledger" /\ ledger = "wrongDate" /\ pc' = "refused" /\ Same

\* net._pay needs the receipt, and only then the request can go out.
Send ==
  /\ pc = "reserved" /\ receipt
  /\ pc' = "flight" /\ sent' = sent + 1
  /\ UNCHANGED <<api, cache, ledger, blocked, count, count0, sent0, blocked0, receipt, token>>
\* shared.once: the call counts, no request went out, and the failure window starts.
ConnectFails == pc = "reserved" /\ pc' = "notSent" /\ Marked

Answer ==
  /\ pc = "flight" /\ pc' = "stored" /\ cache' = "today"
  /\ UNCHANGED <<api, ledger, blocked, count, sent, count0, sent0, blocked0, receipt, token>>
\* shared.once: HTTP 200 with a wrong shape, so the failure window starts and nothing is saved.
BadShape == pc = "flight" /\ pc' = "badAnswer" /\ Marked
\* clarity._cached: quota.block writes the flag, then shared.once marks the failure.
Export429 ==
  /\ pc = "flight" /\ api = "export" /\ pc' = "dayBlocked" /\ blocked' = TRUE
  /\ cache' = "failedInWindow"
  /\ UNCHANGED <<api, ledger, count, sent, count0, sent0, blocked0, receipt, token>>
\* The ledger is stuck after the 429: the day does not stop, and the next call gets its own 429.
Export429LockBusy == pc = "flight" /\ api = "export" /\ pc' = "failed" /\ Marked
Mcp429 == pc = "flight" /\ api = "mcp" /\ pc' = "failed" /\ Marked
OtherError == pc = "flight" /\ pc' = "failed" /\ Marked
\* shared.once: a CancelledError in flight marks the failure too.
TimesOut == pc = "flight" /\ pc' = "failed" /\ Marked

Done == pc \in Ends /\ UNCHANGED vars

Next ==
  TokenMissing \/ TokenRead \/ TakeLock \/ CacheHit \/ CacheOld \/ CacheNone \/ CacheBad \/ FailureRepeat \/ FailureOld
  \/ LockBusy \/ RefuseBroken \/ RefuseBlocked \/ RefuseLimit \/ Reserve \/ ReserveNewDay
  \/ RefuseWrongDate \/ Send \/ ConnectFails \/ Answer \/ BadShape \/ Export429 \/ Export429LockBusy \/ Mcp429
  \/ OtherError \/ TimesOut \/ Done

Spec == Init /\ [][Next]_vars

\* A cache hit spends no quota.
CacheHitSpendsNothing == pc = "hit" => (count = count0 /\ sent = sent0)
\* A repeat of a failed request inside the window spends nothing.
RepeatInWindowSpendsNothing == pc = "repeated" => (count = count0 /\ sent = sent0)
\* A connection failure counts 1 time, sends nothing, and starts the failure window.
ConnectionFailureKeepsTheWindow ==
  pc = "notSent" => (count = count0 + 1 /\ sent = sent0 /\ cache = "failedInWindow")
\* A ledger that a stuck process holds sends nothing and spends nothing.
BusyLedgerSendsNothing == pc = "busy" => (count = count0 /\ sent = sent0)
\* A bad HTTP 200 answer is never saved.
BadAnswerIsNotSaved == pc = "badAnswer" => cache # "today"
\* A saved answer that goes back has a good shape.
HitHasAGoodShape == pc = "hit" => cache = "today"
\* The count is never below the requests sent. A connection that fails before the send counts.
CountNeverBelowRequestsSent == count >= sent
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
\* A saved answer or a failure serves only with the token that the cache path names.
NoTokenReadsNoAnswer == pc \in {"hit", "repeated"} => token
\* Each failure of a spent call starts the failure window.
SpentFailureKeepsTheWindow == pc \in {"failed", "dayBlocked", "badAnswer"} => cache = "failedInWindow"
====
