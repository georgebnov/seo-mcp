export const title = "2 parallel callers of 1 Clarity request on a cold cache";
const pair = (v) => [v.pc[1], v.pc[2]];
const count = (v, names) => pair(v).filter((p) => names.includes(p)).length;
const ended = ["hit", "answered", "refused", "failed", "notSent", "repeated", "dayBlocked", "timedOut"];
export const state = (v) => {
  const calls = v.sent === 2 ? "2 calls spent" : v.sent === 1 ? "1 call spent" : "No call spent";
  const flying = count(v, ["flight"]) === 1;
  const waiting = count(v, ["waiting"]);
  const done = count(v, ended);
  if (done === 2) return `Both callers have a result. ${calls}`;
  if (flying && v.sent === 2) return "The 2nd caller fetches too. 2 calls spent";
  if (flying) return `1 caller fetches, the other waits or stopped. ${calls}`;
  if (waiting === 2) return "Both callers wait for the fetch lock";
  if (v.blocked) return "The day is blocked, 1 caller still waits";
  if (v.cache === "saved") return "The answer is saved, 1 caller still waits";
  if (v.failed) return "The call failed and the failure is kept, 1 caller still waits";
  if (count(v, ["notSent"]) === 1) return "The connection did not open, 1 caller still waits";
  return "1 caller stopped waiting, 1 caller still waits";
};
const labels = {
  TakeLockHit: "the waiting caller gets the lock and reads the saved answer",
  TakeLockRefused: "the waiting caller gets the lock and the ledger refuses",
  TakeLockSend: "a caller gets the lock, reserves 1 call, and sends",
  TakeLockRepeat: "the waiting caller gets the lock and shares the failure, with no spend",
  TakeLockNotSent: "a caller reserves 1 call, and the connection does not open",
  WaitTimesOut: "a waiting caller reaches the source timeout",
  Answer: "Clarity answers with data",
  Fails: "the call fails or times out",
  Limit429: "Clarity says the daily limit is reached",
};
export const step = (action) => (action === "Done" ? null : labels[action]);
export const group = (v) => {
  if (count(v, ended) === 2) return v.sent === 2 ? "2 calls spent" : "End";
  if (v.sent === 2) return "2 calls spent";
  if (count(v, ["flight"]) === 1) return "1 caller fetches";
  return count(v, ["waiting"]) === 2 ? "Start" : "1 caller waits";
};
export const colors = { Start: "blue", "1 caller fetches": "orange", "1 caller waits": "yellow", End: "purple", "2 calls spent": "pink" };
