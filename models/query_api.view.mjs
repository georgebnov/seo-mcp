export const title = "1 query_api call: dates and answer shape";
const names = {
  asked: "An agent calls query_api",
  catalog: "The list of APIs, methods, and tasks, no network",
  taskLookup: "The server matches the task words, no network",
  recipeRows: "The exact calls for each provider, no network",
  noTask: "Status unsupported with the task names, no guess",
  apiKnown: "The API is known",
  fields: "The fields, values, docs, and gaps of the API, no network",
  methodKnown: "The method is known",
  siteKnown: "The fields are valid and the site is known",
  datesSet: "The dates pass, and a future end is the newest day",
  inputError: "An error, no query sent",
  flight: "The read query is on the network",
  rows: "The agent gets the scrubbed rows",
  apiMessage: "The agent gets the scrubbed text of the API",
  fixedError: "The agent gets a fixed error text",
};
const backwards = (v) => v.dates === "startAfterEndRelative";
export const state = (v) => {
  if (v.pc === "datesSet" && backwards(v)) return "The dates pass, but the start is after the end";
  if (v.pc === "flight" && backwards(v)) return "A query with the start after the end is on the network";
  return names[v.pc];
};
const labels = {
  TaskWithMethod: "the call has a task and also a method or a request",
  NoApiNoTask: "the call has no api and no task",
  TaskAsked: "the call has a task and no api",
  ApiNamed: "the api name is known",
  ApiUnknown: "the api name is unknown",
  TaskOnApi: "the call has a task for this api",
  TaskMatches: "a whole phrase matches a task and a provider has a recipe",
  TaskNoMatch: "no task matches, or this api has no recipe",
  NoMethod: "the call has no method",
  MethodNamed: "the method name is known",
  MethodUnknown: "the method name is unknown",
  FieldWrong: "a field is unknown or a needed field is missing",
  FieldsGood: "the fields are good and the site resolves",
  SiteBad: "the site does not resolve",
  ValueWrong: "a value is wrong for the method",
  DatesRefused: "2 YYYY-MM-DD dates are in the wrong order, or the start is past the newest day",
  LocalRefusal: "the ledger refuses the call",
  SavedAnswer: "a saved answer of the same request is inside its time",
  SharedFailure: "the same request failed inside the window",
  ReadQuery: "the server fills the site and the key and sends 1 listed read request",
  Rows: "the API answers with a documented field",
  EmptySitemapIsNoData: "Search Console answers {} for a sitemap read: no sitemap",
  EmptyObjectFails: "the API answers HTTP 200 with no documented field",
  OtherTextFails: "the API answers HTTP 200 with no documented field",
  Google400: "a Google API answers HTTP 400 with a message",
  Other400: "the API answers another error, or the source times out",
  OtherFailure: "the API answers another error, or the source times out",
};
export const step = (action, from, to) => {
  if (action === "Done") return null;
  // The chart hides the steps after a backwards GA4 range that repeat the arrows of a good range.
  if (backwards(from) && ["LocalRefusal", "SharedFailure", "SavedAnswer"].includes(action)) return null;
  if (backwards(from) && from.pc === "flight" && action !== "Google400") return null;
  if (action === "DatesPass") {
    return to.dates === "startAfterEndRelative"
      ? "a GA4 date is relative, so the order check does not run"
      : "the dates are in order and the start is not past the newest day";
  }
  return labels[action];
};
const phases = {
  asked: "Check", apiKnown: "Check", methodKnown: "Check", siteKnown: "Check", datesSet: "Check",
  catalog: "Discovery", taskLookup: "Discovery", recipeRows: "Discovery", fields: "Discovery",
  noTask: "Stop", inputError: "Stop", flight: "Network",
  rows: "Answer", apiMessage: "Stop", fixedError: "Stop",
};
export const group = (v) => phases[v.pc];
export const colors = { Check: "blue", Discovery: "teal", Network: "orange", Stop: "pink", Answer: "purple" };
