const names = {
  start: "An agent calls submit_urls with 1 URL",
  off: "The site did not opt in: refused, nothing sent, no call spent",
  gate: "The site opted in, and the connection gate checks the names",
  notConnected: "Not connected: nothing sent, no call spent",
  check: "The server checks that the URL is in the property",
  foreign: "The URL is outside the property: refused, nothing sent, no call spent",
  lock: "The call holds the lock of the URL and reads the saved submit",
  hit: "A submit of the last hour goes back, no call spent",
  repeated: "The same failure goes back, no call spent",
  ledger: "The server reads the ledger of the Cloud project under its lock",
  busy: "The ledger is busy: nothing sent, no call spent",
  refused: "Refused before the network, no call spent",
  reserved: "1 call is reserved for the Pacific day",
  dayEnded: "The day ended after the count: nothing sent, the call stays counted",
  flight: "The publish request is on the network",
  stored: "Google took the notification, and the server saves it for 1 hour",
  badAnswer: "Google answered with no valid notifyTime: not saved",
  failed: "The call failed after the request went out, and it stays counted",
  notSent: "No request went out, the reserved call stays counted",
};
export const title = "1 URL of submit_urls";
export const state = (v) => names[v.pc];
const labels = {
  Off: "the site has no \"submit_urls\": \"on\"",
  OptedIn: "the site has \"submit_urls\": \"on\"",
  NotConnected: "a field or a secret name is missing",
  Connected: "each field and secret name is there",
  Foreign: "the URL is not in the Search Console property",
  Inside: "the URL is in the property",
  CacheHit: "the same URL went out less than 1 hour ago",
  FailureRepeat: "the same URL failed inside the window",
  CacheNone: "no saved submit exists",
  LockBusy: "a stuck process holds the ledger lock for 2 seconds",
  RefuseBroken: "the ledger text or count is broken",
  RefuseLimit: "the count is at 200",
  Reserve: "the count is below the limit",
  ReserveNewDay: "the ledger is of an older Pacific day, so the count restarts at 0",
  DayEnds: "midnight Pacific time passes",
  StaleReceipt: "the receipt is of the old day",
  Send: "the server sends URL_UPDATED",
  ConnectFails: "the connection fails before the request goes out",
  Answer: "Google answers with notifyTime",
  BadShape: "Google answers HTTP 200 with no valid notifyTime",
  Fails: "Google answers an error, or the call times out",
};
export const step = (action) => (action === "Done" ? null : labels[action]);
const phases = {
  start: "Start", gate: "Start", check: "Start", lock: "Saved answer", hit: "Saved answer",
  repeated: "Saved answer", ledger: "Ledger", off: "Stop", notConnected: "Stop", foreign: "Stop",
  busy: "Stop", refused: "Stop", reserved: "Spend", flight: "Spend", dayEnded: "Stop",
  stored: "Answer", badAnswer: "Stop", failed: "Stop", notSent: "Stop",
};
export const group = (v) => phases[v.pc];
export const colors = { Start: "blue", "Saved answer": "teal", Ledger: "yellow", Spend: "orange", Stop: "pink", Answer: "purple" };
