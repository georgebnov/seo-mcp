export const title = "1 dated source of get_seo_data: dates, days, and answer shape";
const flow = {
  asked: "An agent asks a dated source",
  named: "The source and 2 dates are named",
  paramError: "Stop with an error, no request",
  checked: "The dates are in order",
  tooLate: "Status error: the start is past the newest day, no request",
  endSet: "The end is set, and a later end is moved to the newest day",
  flight: "The report is on the network",
};
export const state = (v) => {
  if (v.pc === "covered") {
    return v.cov === "api"
      ? "The last complete day comes from the API now"
      : "The last complete day is an estimate";
  }
  if (v.pc === "answered") {
    return v.fetchedLast < 2 ? "A saved report from before the API moved serves" : "The API answers HTTP 200";
  }
  if (v.pc === "result") {
    if (v.status === "error") return "Status error: the answer has a wrong shape, nothing saved";
    if (v.shownLast > v.fetchedLast) return "Status ok: a day fetched as partial shows as complete";
    if (v.totals === "zero" && v.start > v.shownLast) return "Status ok: a range of partial days shows totals 0";
    return "Status ok, with coverage and partial_days";
  }
  if (v.pc === "gated") {
    if (v.delta === "shown" && v.first > v.start) return "A delta compares days with no data";
    return v.delta === "shown" ? "Delta is shown" : "Delta is null, and the reason says why";
  }
  return flow[v.pc];
};
const labels = {
  Ask: "the agent names a source, start_date, and end_date",
  DatesBackwards: "start_date is after end_date",
  DatesInOrder: "start_date is not after end_date",
  StartTooLate: "the start is past the newest day of the source",
  EndSet: "the start is not past the newest day",
  CoverageFromApi: "Search Console gives firstIncompleteDate",
  CoverageEstimate: "GA4, or Search Console gives no firstIncompleteDate",
  SendReport: "the server sends the report",
  SavedReport: "another source read the coverage first, and the API moved its last complete day since",
  BadShape: "the answer has no documented field",
  Result: "the answer has a documented field",
  DeltaNoRead: "the GA4 day rows have a row on the first day of the range",
};
export const step = (action, from, to) => {
  if (action === "Done") return null;
  if (action === "DeltaAfterRead") {
    return {
      found: "1 extra read gives the first day of data",
      noEvent: "1 extra read finds no data on any day",
      failed: "the extra read fails",
    }[to.read];
  }
  if (action === "Answer") {
    return {
      rows: "the API answers with rows",
      noRows: "the API answers a documented field and no rows",
      emptyObject: "the API answers {}",
      otherText: "the API answers other text, such as a message",
    }[to.reply];
  }
  return labels[action];
};
export const group = (v) => {
  if (["asked", "named", "checked", "endSet"].includes(v.pc)) return "Dates";
  if (["covered"].includes(v.pc)) return "Days";
  if (["flight", "answered"].includes(v.pc)) return "Report";
  if (["result", "gated"].includes(v.pc) && v.status === "ok") return "Result";
  return "Stop";
};
export const colors = { Dates: "blue", Days: "teal", Report: "orange", Stop: "pink", Result: "purple" };
