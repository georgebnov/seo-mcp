const names = {
  start: "An agent makes a metered Clarity call",
  lock: "The call holds the fetch lock and reads the saved answer",
  hit: "The saved answer goes back, no call spent",
  token: "No saved answer for today, the server reads the token",
  notConnected: "Not connected, no call spent",
  ledger: "The server reads the ledger under its lock",
  refused: "Refused before the network, no call spent",
  reserved: "1 call is reserved in the ledger",
  flight: "The request is on the network",
  stored: "The answer is saved for the UTC day and goes back",
  failed: "The call failed, and the reserved call stays counted",
  dayBlocked: "The day is blocked until the next UTC day",
};
export const title = "1 metered Clarity call";
export const state = (v) => {
  if (v.pc === "failed") {
    return v.sent > v.sent0
      ? "The call failed after the request went out, and it stays counted"
      : "No request went out, and the reserved call stays counted";
  }
  return names[v.pc];
};
const labels = {
  TakeLock: "the call takes the fetch lock",
  CacheHit: "a saved answer of today exists",
  CacheOld: "the saved answer is of an older UTC day",
  CacheNone: "no saved answer exists",
  TokenMissing: "the token is missing or empty",
  TokenRead: "the token is there",
  RefuseBroken: "the ledger text or count is broken",
  RefuseBlocked: "the day is already blocked",
  RefuseLimit: "the count is at the limit",
  Reserve: "the count is below the limit",
  ReserveNewDay: "the ledger is of an older UTC day, so the count restarts at 0",
  RefuseWrongDate: "the ledger date is no real date, or is after today, so the ledger is broken",
  Send: "the server sends the request",
  ConnectFails: "the connection fails before the request goes out",
  Answer: "Clarity answers with data",
  Export429: "Clarity says the daily limit is reached (HTTP 429 of the Data Export API)",
  Mcp429: "an /mcp endpoint answers HTTP 429",
  OtherError: "Clarity answers another error",
  TimesOut: "the source times out",
};
export const step = (action) => (action === "Done" ? null : labels[action]);
const phases = {
  start: "Start", lock: "Saved answer", hit: "Saved answer",
  token: "Ledger", ledger: "Ledger", notConnected: "Stop", refused: "Stop",
  reserved: "Spend", flight: "Spend",
  stored: "Answer", failed: "Stop", dayBlocked: "Stop",
};
export const group = (v) => phases[v.pc];
export const colors = { Start: "blue", "Saved answer": "teal", Ledger: "yellow", Spend: "orange", Stop: "pink", Answer: "purple" };
