---- MODULE parallel_callers ----
\* 2 callers of 1 Clarity request on a cold cache, through clarity._only_fetcher.
\* The steps between the lock and the send have no await, so 1 step holds them.
EXTENDS Naturals
CONSTANT Limit
VARIABLES pc, lock, cache, blocked, count, sent, firstOk, sentAfterBlock, failed, spentInWindow
vars == <<pc, lock, cache, blocked, count, sent, firstOk, sentAfterBlock, failed, spentInWindow>>
Callers == {1, 2}
Ends == {"hit", "answered", "refused", "failed", "notSent", "repeated", "dayBlocked", "timedOut"}

Init ==
  /\ pc = [c \in Callers |-> "waiting"]
  /\ lock = 0 /\ cache = "cold" /\ blocked = FALSE
  /\ count = 0 /\ sent = 0 /\ firstOk = FALSE /\ sentAfterBlock = FALSE
  /\ failed = FALSE /\ spentInWindow = FALSE

Keep == UNCHANGED <<failed, spentInWindow>>

TakeLockHit(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "saved"
  /\ pc' = [pc EXCEPT ![c] = "hit"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
\* clarity._recent_failure: the file of the failure is under the window, so nothing is spent.
TakeLockRepeat(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "cold" /\ failed
  /\ pc' = [pc EXCEPT ![c] = "repeated"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
TakeLockRefused(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "cold" /\ ~failed
  /\ (blocked \/ count + 1 > Limit)
  /\ pc' = [pc EXCEPT ![c] = "refused"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
TakeLockSend(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "cold" /\ ~failed
  /\ ~blocked /\ count + 1 <= Limit
  /\ pc' = [pc EXCEPT ![c] = "flight"]
  /\ lock' = c /\ count' = count + 1 /\ sent' = sent + 1
  /\ sentAfterBlock' = (sentAfterBlock \/ blocked)
  /\ spentInWindow' = (spentInWindow \/ failed)
  /\ UNCHANGED <<cache, blocked, firstOk, failed>>
\* The connection did not open: the call counts, no request went out, and no failure is kept.
TakeLockNotSent(c) ==
  /\ pc[c] = "waiting" /\ lock = 0 /\ cache = "cold" /\ ~failed
  /\ ~blocked /\ count + 1 <= Limit
  /\ pc' = [pc EXCEPT ![c] = "notSent"]
  /\ count' = count + 1
  /\ spentInWindow' = (spentInWindow \/ failed)
  /\ UNCHANGED <<lock, cache, blocked, sent, firstOk, sentAfterBlock, failed>>
WaitTimesOut(c) ==
  /\ pc[c] = "waiting"
  /\ pc' = [pc EXCEPT ![c] = "timedOut"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
Answer(c) ==
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "answered"]
  /\ lock' = 0 /\ cache' = "saved" /\ firstOk' = (sent = 1)
  /\ UNCHANGED <<blocked, count, sent, sentAfterBlock>> /\ Keep
Fails(c) ==
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "failed"]
  /\ lock' = 0 /\ failed' = TRUE
  /\ UNCHANGED <<cache, blocked, count, sent, firstOk, sentAfterBlock, spentInWindow>>
Limit429(c) ==
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "dayBlocked"]
  /\ lock' = 0 /\ blocked' = TRUE /\ failed' = TRUE
  /\ UNCHANGED <<cache, count, sent, firstOk, sentAfterBlock, spentInWindow>>

Done == (\A c \in Callers: pc[c] \in Ends) /\ UNCHANGED vars

Next ==
  \/ \E c \in Callers:
       TakeLockHit(c) \/ TakeLockRepeat(c) \/ TakeLockRefused(c) \/ TakeLockSend(c)
       \/ TakeLockNotSent(c) \/ WaitTimesOut(c)
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
\* A repeat of a failed request inside the window spends nothing.
RepeatInWindowSpendsNothing == ~spentInWindow
====
