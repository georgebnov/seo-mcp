---- MODULE parallel_callers ----
\* 2 callers of 1 request on a cold cache, through shared.once. Each source and method uses it.
\* 1 step holds the lock, the check of the saved answer and the failure, and the spend: the cache
\* of a slot changes only under its lock, and quota.spend reads and writes the ledger under the
\* ledger lock. The send is a separate step: between quota.spend and the request, net.request
\* awaits the connection, and another task or process can change the ledger.
\* Each caller has a key, A or B. The path of the answer, the failure, and the lock holds the
\* identity of the key (shared.entry, shared.named), so each key has its own slot. The ledger
\* is of the resource (Clarity project, Search Console site), so both keys spend from 1 count.
EXTENDS Naturals
CONSTANT Limit
\* failed is the file of the failure. hadFailure is what happened, with or without that file.
VARIABLES key, pc, lock, cache, blocked, count, sent, firstOk, sentAfterBlock, failed, hadFailure, spentInWindow
vars == <<key, pc, lock, cache, blocked, count, sent, firstOk, sentAfterBlock, failed, hadFailure, spentInWindow>>
Callers == {1, 2}
Keys == {"A", "B"}
Ends == {"hit", "answered", "refused", "failed", "notSent", "repeated", "dayBlocked", "timedOut"}

\* The slot of a caller: the cache path, the failure path, and the lock path of its key.
Slot(c) == key[c]
Sent == sent["A"] + sent["B"]

Init ==
  /\ key \in [Callers -> Keys]
  /\ pc = [c \in Callers |-> "waiting"]
  /\ lock = [k \in Keys |-> 0] /\ cache = [k \in Keys |-> "cold"] /\ blocked = FALSE
  /\ count = 0 /\ sent = [k \in Keys |-> 0] /\ firstOk = [k \in Keys |-> FALSE]
  /\ sentAfterBlock = FALSE
  /\ failed = [k \in Keys |-> FALSE] /\ hadFailure = [k \in Keys |-> FALSE]
  /\ spentInWindow = FALSE

Keep == UNCHANGED <<key, failed, hadFailure, spentInWindow>>

TakeLockHit(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "waiting" /\ lock[s] = 0 /\ cache[s] = "saved"
  /\ pc' = [pc EXCEPT ![c] = "hit"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
\* shared._recent_failure: the file of the failure is under the window, so nothing is spent.
TakeLockRepeat(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "waiting" /\ lock[s] = 0 /\ cache[s] = "cold" /\ failed[s]
  /\ pc' = [pc EXCEPT ![c] = "repeated"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
TakeLockRefused(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "waiting" /\ lock[s] = 0 /\ cache[s] = "cold" /\ ~failed[s]
  /\ (blocked \/ count + 1 > Limit)
  /\ pc' = [pc EXCEPT ![c] = "refused"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
\* shared.once: prepare runs quota.spend under the lock of the slot, so 1 call is reserved.
TakeLockSpend(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "waiting" /\ lock[s] = 0 /\ cache[s] = "cold" /\ ~failed[s]
  /\ ~blocked /\ count + 1 <= Limit
  /\ pc' = [pc EXCEPT ![c] = "spent"]
  /\ lock' = [lock EXCEPT ![s] = c] /\ count' = count + 1
  /\ spentInWindow' = (spentInWindow \/ hadFailure[s])
  /\ UNCHANGED <<key, cache, blocked, sent, firstOk, sentAfterBlock, failed, hadFailure>>
\* shared.once: send runs net.request, and the request goes out.
Send(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "spent"
  /\ pc' = [pc EXCEPT ![c] = "flight"]
  /\ sent' = [sent EXCEPT ![s] = @ + 1]
  /\ sentAfterBlock' = (sentAfterBlock \/ blocked)
  /\ UNCHANGED <<lock, cache, blocked, count, firstOk>> /\ Keep
\* The connection did not open: the call counts, no request went out, and the failure is kept.
NotSent(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "spent"
  /\ pc' = [pc EXCEPT ![c] = "notSent"]
  /\ lock' = [lock EXCEPT ![s] = 0]
  /\ failed' = [failed EXCEPT ![s] = TRUE] /\ hadFailure' = [hadFailure EXCEPT ![s] = TRUE]
  /\ UNCHANGED <<key, cache, blocked, count, sent, firstOk, sentAfterBlock, spentInWindow>>
WaitTimesOut(c) ==
  /\ pc[c] = "waiting"
  /\ pc' = [pc EXCEPT ![c] = "timedOut"]
  /\ UNCHANGED <<lock, cache, blocked, count, sent, firstOk, sentAfterBlock>> /\ Keep
Answer(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "answered"]
  /\ lock' = [lock EXCEPT ![s] = 0] /\ cache' = [cache EXCEPT ![s] = "saved"]
  /\ firstOk' = [firstOk EXCEPT ![s] = (sent[s] = 1)]
  /\ UNCHANGED <<blocked, count, sent, sentAfterBlock>> /\ Keep
Fails(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "failed"]
  /\ lock' = [lock EXCEPT ![s] = 0]
  /\ failed' = [failed EXCEPT ![s] = TRUE] /\ hadFailure' = [hadFailure EXCEPT ![s] = TRUE]
  /\ UNCHANGED <<key, cache, blocked, count, sent, firstOk, sentAfterBlock, spentInWindow>>
\* The 429 blocks the day of the ledger, so it blocks both keys.
Limit429(c) ==
  LET s == Slot(c) IN
  /\ pc[c] = "flight"
  /\ pc' = [pc EXCEPT ![c] = "dayBlocked"]
  /\ lock' = [lock EXCEPT ![s] = 0] /\ blocked' = TRUE
  /\ failed' = [failed EXCEPT ![s] = TRUE] /\ hadFailure' = [hadFailure EXCEPT ![s] = TRUE]
  /\ UNCHANGED <<key, cache, count, sent, firstOk, sentAfterBlock, spentInWindow>>

Done == (\A c \in Callers: pc[c] \in Ends) /\ UNCHANGED vars

Next ==
  \/ \E c \in Callers:
       TakeLockHit(c) \/ TakeLockRepeat(c) \/ TakeLockRefused(c) \/ TakeLockSpend(c)
       \/ Send(c) \/ NotSent(c) \/ WaitTimesOut(c)
       \/ Answer(c) \/ Fails(c) \/ Limit429(c)
  \/ Done

Spec == Init /\ [][Next]_vars

\* 2 parallel callers of 1 key on a cold cache spend 1 call.
TwoCallersSpendOneCall == \A k \in Keys: sent[k] <= 1
\* Added: when the first call of a key gives an answer, the other caller of that key spends nothing.
FirstAnswerIsShared == \A k \in Keys: firstOk[k] => sent[k] = 1
\* The count is never below the requests sent. A connection that fails before the send counts.
\* 1 count holds the sends of both keys: a new key does not reset the ledger.
CountNeverBelowRequestsSent == count >= Sent
\* The server never sends a metered call past the limit, with any number of keys.
NeverPastTheLimit == Sent <= Limit
\* After the daily limit answer, no caller sends again on that day.
NoSendAfterTheLimitAnswer == ~sentAfterBlock
\* A repeat of a failed request inside the window spends nothing, also after a connection failure.
RepeatInWindowSpendsNothing == ~spentInWindow
\* A saved answer or a failure serves only a caller of the key that made it.
\* So after a key change, the old answer and the old failure window do not serve.
NoAnswerOfAnotherKey ==
  \A c \in Callers:
    pc[c] \in {"hit", "repeated"} => \E d \in Callers: d # c /\ key[d] = key[c]
====
