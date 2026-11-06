export const title = "1 get_seo_data call, following 1 source";
const flow = {
  asked: "An agent calls get_seo_data",
  site: "The site is known",
  params: "The parameters are valid",
  sources: "The source names are known",
  siteError: "Stop with an error, no API request",
  paramError: "Stop with an error, no API request",
  sourceError: "Stop with an error, no API request",
  gathered: "All sources have a status",
  result: "The agent gets 1 scrubbed result",
};
const one = {
  running: "The source runs",
  called: "The source request is on the network",
  ok: "The source has status ok",
  thin: "The source has status ok",
  error: "The source has status error",
  notConnected: "The source has status not_connected",
  unsupported: "The source has status unsupported, no API request",
};
export const state = (v) => (v.pc === "running" ? one[v.st.One] : flow[v.pc]);
const labels = {
  SiteFound: "the sites file has the site",
  SiteBad: "the sites file or the site is not found",
  ParamsGood: "each parameter is valid",
  ParamsBad: "a parameter is wrong for every source",
  SourcesKnown: "each source name is known",
  SourcesUnknown: "a source name is unknown or the list is empty",
  OutOfRange: "the source sees a value out of its range, such as last_days 5 for Clarity",
  NoFieldOrKey: "the site has no field or key for the source",
  RefusedLocally: "the server refuses the call, such as at a quota",
  Request: "the source sends its read request",
  Rows: "the API answers with rows",
  ThinRows: "the API answers with too few rows, so thin_data is set",
  ApiError: "the API answers an error",
  TimesOut: "the source times out after 100 seconds",
  AllDone: "the last source of the call finishes",
  Scrub: "the server removes secrets from the result",
};
export const step = (action, from, to) => {
  if (action === "Done") return null;
  if (action === "Start") {
    return to.st.One === "unsupported"
      ? "the source cannot apply a parameter of the call"
      : "the source can apply each parameter, and all sources start at the same time";
  }
  return labels[action];
};
export const group = (v) => {
  if (v.pc === "running") return { running: "Run", called: "Run", unsupported: "Stop" }[v.st.One] ?? "Source status";
  if (["siteError", "paramError", "sourceError"].includes(v.pc)) return "Stop";
  return ["gathered", "result"].includes(v.pc) ? "Result" : "Check";
};
export const colors = { Check: "blue", Run: "orange", "Source status": "yellow", Stop: "pink", Result: "purple" };
