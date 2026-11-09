const names = {
  start: "An agent makes a metered Clarity call, and the server reads the token",
  lock: "The call holds the fetch lock and reads the saved answer",
  hit: "The saved answer goes back, no call spent",
  badAnswer: "The answer has a wrong shape: not saved, and the reason says so",
  repeated: "The same failure goes back, no call spent",
  token: "The token is there, and the call waits for the fetch lock",
  notConnected: "Not connected, no call spent",
  ledger: "The server reads the ledger under its lock",
  busy: "The ledger is busy: no call sent, no call spent",
  refused: "Refused before the network, no call spent",
  reserved: "1 call is reserved in the ledger",
  flight: "The request is on the network",
  stored: "The answer is saved for the UTC day and goes back",
  failed: "The call failed after the request went out, and it stays counted",
  notSent: "No request went out, the reserved call stays counted, and the failure window starts",
  dayBlocked: "The day is blocked until the next UTC day",
};
export const title = "1 metered Clarity call";
export const state = (v) => names[v.pc];
const labels = {
  TakeLock: "the call takes the fetch lock",
  CacheHit: "a saved answer of today exists",
  CacheOld: "the saved answer is of an older UTC day",
  CacheNone: "no saved answer exists",
  CacheBad: "the saved answer of today has a wrong shape",
  FailureRepeat: "the same request failed inside the window",
  FailureOld: "the same request failed before the window",
  TokenMissing: "the token is missing or empty",
  TokenRead: "the token is there",
  LockBusy: "a stuck process holds the ledger lock for 2 seconds",
  RefuseBroken: "the ledger text or count is broken",
  RefuseBlocked: "the day is already blocked",
  RefuseLimit: "the count is at the limit",
  Reserve: "the count is below the limit",
  ReserveNewDay: "the ledger is of an older UTC day, so the count restarts at 0",
  RefuseWrongDate: "the ledger date is no real date, or is after today, so the ledger is broken",
  Send: "the server sends the request",
  ConnectFails: "the connection fails before the request goes out",
  Answer: "Clarity answers with data",
  BadShape: "Clarity answers HTTP 200 with a wrong shape",
  Export429: "Clarity says the daily limit is reached (HTTP 429 of the Data Export API)",
  Export429LockBusy: "Clarity says the daily limit is reached, and the ledger is busy",
  Mcp429: "an /mcp endpoint answers HTTP 429",
  OtherError: "Clarity answers another error",
  TimesOut: "the source times out",
};
export const step = (action) => (action === "Done" ? null : labels[action]);
const phases = {
  start: "Start", lock: "Saved answer", hit: "Saved answer", repeated: "Saved answer",
  token: "Start", ledger: "Ledger", notConnected: "Stop", refused: "Stop", busy: "Stop",
  reserved: "Spend", flight: "Spend",
  stored: "Answer", failed: "Stop", notSent: "Stop", badAnswer: "Stop", dayBlocked: "Stop",
};
export const group = (v) => phases[v.pc];
export const colors = { Start: "blue", "Saved answer": "teal", Ledger: "yellow", Spend: "orange", Stop: "pink", Answer: "purple" };
