---- MODULE submit_urls ----
\* 1 URL of the submit_urls tool, the 1 write: core.submit_urls, the connection gate,
\* indexing.problem, shared.once, quota.spend of indexing-publish-calls, and net.request.
\* The start state is any opt-in, URL, cache, and ledger that earlier calls can leave.
EXTENDS Naturals
CONSTANT Limit
VARIABLES pc, optIn, inProperty, connected, cache, ledger, count, sent, count0, sent0, receipt
vars == <<pc, optIn, inProperty, connected, cache, ledger, count, sent, count0, sent0, receipt>>

Ends == {"off", "foreign", "notConnected", "hit", "repeated", "busy", "refused", "dayEnded",
         "stored", "badAnswer", "failed", "notSent"}

Init ==
  /\ pc = "start"
  /\ optIn \in BOOLEAN
  /\ inProperty \in BOOLEAN
  /\ connected \in BOOLEAN
  /\ cache \in {"hour", "none", "failedInWindow"}
  /\ ledger \in {"good", "olderDay", "broken"}
  /\ sent \in 0..Limit
  \* A ledger of an older Pacific day counts nothing that went out today.
  /\ (ledger = "olderDay") => sent = 0
  /\ count = sent
  /\ count0 = count /\ sent0 = sent
  /\ receipt = "none"

Same == UNCHANGED <<optIn, inProperty, connected, cache, ledger, count, sent, count0, sent0, receipt>>
\* shared.once marks the failure of a spent call, so a repeat inside the window spends nothing.
Marked == /\ cache' = "failedInWindow"
          /\ UNCHANGED <<optIn, inProperty, connected, ledger, count, sent, count0, sent0, receipt>>

\* core.submit_urls: Site.opted_in is the exact value "on". The gate checks it again.
Off == pc = "start" /\ ~optIn /\ pc' = "off" /\ Same
OptedIn == pc = "start" /\ optIn /\ pc' = "gate" /\ Same
\* providers._gated: connection.require, with no secret value and no network.
NotConnected == pc = "gate" /\ ~connected /\ pc' = "notConnected" /\ Same
Connected == pc = "gate" /\ connected /\ pc' = "check" /\ Same
\* indexing.problem: a URL outside the property is refused before the token and the ledger.
Foreign == pc = "check" /\ ~inProperty /\ pc' = "foreign" /\ Same
Inside == pc = "check" /\ inProperty /\ pc' = "lock" /\ Same

\* shared.once under the lock of the URL: a saved submit of the last hour, or a failure.
CacheHit == pc = "lock" /\ cache = "hour" /\ pc' = "hit" /\ Same
FailureRepeat == pc = "lock" /\ cache = "failedInWindow" /\ pc' = "repeated" /\ Same
CacheNone == pc = "lock" /\ cache = "none" /\ pc' = "ledger" /\ Same

\* quota.locked: a stuck process holds the ledger lock past the wait.
LockBusy == pc = "ledger" /\ pc' = "busy" /\ Same
RefuseBroken == pc = "ledger" /\ ledger = "broken" /\ pc' = "refused" /\ Same
RefuseLimit == pc = "ledger" /\ ledger = "good" /\ count + 1 > Limit /\ pc' = "refused" /\ Same
Reserve ==
  /\ pc = "ledger" /\ ledger = "good" /\ count + 1 <= Limit
  /\ pc' = "reserved" /\ count' = count + 1 /\ receipt' = "today"
  /\ UNCHANGED <<optIn, inProperty, connected, cache, ledger, sent, count0, sent0>>
ReserveNewDay ==
  /\ pc = "ledger" /\ ledger = "olderDay"
  /\ pc' = "reserved" /\ count' = 1 /\ receipt' = "today"
  /\ UNCHANGED <<optIn, inProperty, connected, cache, ledger, sent, count0, sent0>>

\* The Pacific day can end between quota.spend and the send.
DayEnds ==
  /\ pc = "reserved" /\ receipt = "today" /\ receipt' = "olderDay" /\ pc' = "reserved"
  /\ UNCHANGED <<optIn, inProperty, connected, cache, ledger, count, sent, count0, sent0>>
\* indexing._one.send: a receipt of an older day opens no call of the new day.
StaleReceipt == pc = "reserved" /\ receipt = "olderDay" /\ pc' = "dayEnded" /\ Marked
\* net._pay needs the receipt of the ledger, and only then the request can go out.
Send ==
  /\ pc = "reserved" /\ receipt = "today"
  /\ pc' = "flight" /\ sent' = sent + 1
  /\ UNCHANGED <<optIn, inProperty, connected, cache, ledger, count, count0, sent0, receipt>>
ConnectFails == pc = "reserved" /\ receipt = "today" /\ pc' = "notSent" /\ Marked

\* A good answer holds a notifyTime that parses as a time.
Answer ==
  /\ pc = "flight" /\ pc' = "stored" /\ cache' = "hour"
  /\ UNCHANGED <<optIn, inProperty, connected, ledger, count, sent, count0, sent0, receipt>>
BadShape == pc = "flight" /\ pc' = "badAnswer" /\ Marked
\* HTTP 403, 429, another status, a read timeout, or the source timeout in flight.
Fails == pc = "flight" /\ pc' = "failed" /\ Marked

Done == pc \in Ends /\ UNCHANGED vars

Next ==
  Off \/ OptedIn \/ NotConnected \/ Connected \/ Foreign \/ Inside \/ CacheHit \/ FailureRepeat
  \/ CacheNone \/ LockBusy \/ RefuseBroken \/ RefuseLimit \/ Reserve \/ ReserveNewDay \/ DayEnds
  \/ StaleReceipt \/ Send \/ ConnectFails \/ Answer \/ BadShape \/ Fails \/ Done

Spec == Init /\ [][Next]_vars

Unspent == count = count0 /\ sent = sent0
\* A site with no opt-in sends nothing and spends nothing.
OffSendsNothing == ~optIn => Unspent
\* A site that is not connected sends nothing and spends nothing.
NotConnectedSendsNothing == ~connected => Unspent
\* A URL outside the property sends nothing and spends nothing.
ForeignUrlSendsNothing == ~inProperty => Unspent
\* A repeat inside the hour, or inside the failure window, spends nothing.
CacheHitSpendsNothing == pc = "hit" => Unspent
RepeatInWindowSpendsNothing == pc = "repeated" => Unspent
\* A ledger that a stuck process holds, or a broken ledger, sends nothing.
BusyLedgerSendsNothing == pc = "busy" => Unspent
BrokenLedgerSendsNothing == ledger = "broken" => Unspent
\* The count is never below the requests sent.
CountNeverBelowRequestsSent == count >= sent
\* The server never sends a publish past the limit of the Pacific day.
NeverPastTheLimit == sent <= Limit
\* A receipt of an older day sends nothing: each request that went out has a receipt of its day.
StaleReceiptSendsNothing == /\ (pc = "dayEnded" => sent = sent0)
                            /\ (sent > sent0 => receipt = "today")
\* The ledger counts the call before the network.
ReservedBeforeTheNetwork == sent > sent0 => receipt # "none"
\* A bad HTTP 200 answer is never saved.
BadAnswerIsNotSaved == pc = "badAnswer" => cache # "hour"
\* Each failure of a spent call starts the failure window.
SpentFailureKeepsTheWindow ==
  pc \in {"failed", "badAnswer", "notSent", "dayEnded"} => cache = "failedInWindow"
====
