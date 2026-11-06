---- MODULE parallel_callers ----
\* 2 callers of 1 Clarity request on a cold cache, through clarity._only_fetcher.
\* The steps between the lock and the send have no await, so 1 step holds them.
EXTENDS Naturals
CONSTANT Limit
VARIABLES pc, lock, cache, blocked, count, sent, firstOk, sentAfterBlock
vars == <<pc, lock, cache, blocked, count, sent, firstOk, sentAfterBlock>>
Callers == {1, 2}
Ends == {"hit", "answered", "refused", "failed", "dayBlocked", "timedOut"}

Init ==
  /\ pc = [c \in Callers |-> "waiting"]
  /\ lock = 0 /\ cache = "cold" /\ blocked = FALSE
  /\ count = 0 /\ sent = 0 /\ firstOk = FALSE /\ sentAfterBlock = FALSE

TakeLockHit(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "saved"
  /\ pc' = [pc EXCEPT ![c] = "hit"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>>
TakeLockRefused(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "cold"
  /\ (blocked \/ count + 1 > Limit)
  /\ pc' = [pc EXCEPT ![c] = "refused"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>>
TakeLockSend(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "cold"
  /\ ~blocked /\ count + 1 <= Limit
  /\ pc' = [pc EXCEPT ![c] = "flight"]
  /\ lock' = c /\ count' = count + 1 /\ sent' = sent + 1
  /\ sentAfterBlock' = (sentAfterBlock \/ blocked)
  /\ UNCHANGED <<cache, blocked, firstOk>>
WaitTimesOut(c) ==
  /\ pc[c] = "waiting"
  /\ pc' = [pc EXCEPT ![c] = "timedOut"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>>
Answer(c) ==
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "answered"]
  /\ lock' = 0 /\ cache' = "saved" /\ firstOk' = (sent = 1)
  /\ UNCHANGED <<blocked, count, sent, sentAfterBlock>>
Fails(c) ==
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "failed"]
  /\ lock' = 0
  /\ UNCHANGED <<cache, blocked, count, sent, firstOk, sentAfterBlock>>
Limit429(c) ==
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "dayBlocked"]
  /\ lock' = 0 /\ blocked' = TRUE
  /\ UNCHANGED <<cache, count, sent, firstOk, sentAfterBlock>>

Done == (\A c \in Callers: pc[c] \in Ends) /\ UNCHANGED vars

Next ==
  \/ \E c \in Callers:
       TakeLockHit(c) \/ TakeLockRefused(c) \/ TakeLockSend(c) \/ WaitTimesOut(c)
       \/ Answer(c) \/ Fails(c) \/ Limit429(c)
  \/ Done

Spec == Init /\ [][Next]_vars

\* 2 parallel callers on a cold cache spend 1 call.
TwoCallersSpendOneCall == sent <= 1
\* Added: when the first call gives an answer, the other caller spends nothing.
FirstAnswerIsShared == firstOk => sent = 1
\* The ledger count equals the metered requests sent.
CountEqualsRequestsSent == count = sent
\* The server never sends a metered call past the limit.
NeverPastTheLimit == sent <= Limit
\* After the daily limit answer, no caller sends again on that day.
NoSendAfterTheLimitAnswer == ~sentAfterBlock
====
