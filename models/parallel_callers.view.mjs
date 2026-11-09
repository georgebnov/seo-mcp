export const title = "2 parallel callers of 1 request on a cold cache, with 1 key or 2 keys";
const pair = (v) => [v.pc[1], v.pc[2]];
const count = (v, names) => pair(v).filter((p) => names.includes(p)).length;
const total = (v) => v.sent.A + v.sent.B;
const keys = (v) => (v.key[1] === v.key[2] ? "1 key" : "2 keys");
const any = (record) => record.A || record.B;
const ended = ["hit", "answered", "refused", "failed", "notSent", "repeated", "dayBlocked", "timedOut"];
export const state = (v) => {
  const sent = total(v);
  const calls = sent === 2 ? "2 requests sent" : sent === 1 ? "1 request sent" : "No request sent";
  const flying = count(v, ["flight"]) >= 1;
  const waiting = count(v, ["waiting"]);
  const done = count(v, ended);
  if (done === 2) return `Both callers have a result, ${keys(v)}. ${calls}`;
  if (count(v, ["spent"]) >= 1) {
    const day = v.blocked ? ", and the day is blocked" : "";
    return `A caller reserved 1 call and opens the connection${day}. ${calls}`;
  }
  if (flying && sent === 2) return `The 2nd caller fetches too, ${keys(v)}. 2 requests sent`;
  if (flying) return `1 caller fetches, the other waits or stopped. ${calls}`;
  if (waiting === 2) return `Both callers wait for the fetch lock, ${keys(v)}`;
  if (v.blocked) return "The day is blocked, 1 caller still waits";
  if (v.cache.A === "saved" || v.cache.B === "saved") return "The answer is saved, 1 caller still waits";
  if (any(v.failed)) return "The call failed and the failure is kept, 1 caller still waits";
  return "1 caller stopped waiting, 1 caller still waits";
};
const labels = {
  TakeLockHit: "the waiting caller gets the lock and reads the saved answer",
  TakeLockRefused: "the waiting caller gets the lock and the ledger refuses",
  TakeLockSpend: "a caller gets the lock and reserves 1 call",
  Send: "the request of that caller goes out",
  TakeLockRepeat: "the waiting caller gets the lock and shares the failure, with no spend",
  NotSent: "the connection does not open: the call stays counted, and the failure is kept",
  WaitTimesOut: "a waiting caller reaches the source timeout",
  Answer: "Clarity answers with data",
  Fails: "the call fails or times out",
  Limit429: "Clarity says the daily limit is reached",
};
export const step = (action) => (action === "Done" ? null : labels[action]);
export const group = (v) => {
  if (count(v, ended) === 2) return total(v) === 2 ? "2 requests sent" : "End";
  if (total(v) === 2) return "2 requests sent";
  if (count(v, ["flight", "spent"]) >= 1) return "1 caller fetches";
  return count(v, ["waiting"]) === 2 ? "Start" : "1 caller waits";
};
export const colors = { Start: "blue", "1 caller fetches": "orange", "1 caller waits": "yellow", End: "purple", "2 requests sent": "pink" };
