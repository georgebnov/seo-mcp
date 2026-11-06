# Changelog

## Unreleased

- Add a time frame, 1 page, 1 query, and a comparison of 2 periods or 2 pages to `get_seo_data`.
- A source that cannot apply a parameter now reports status `unsupported` and does not run.
- Add the `query_api` tool: 1 official read query for Search Console, GA4, PageSpeed, the Chrome UX Report, Bing, and Clarity, with a list of valid fields and values.
- Add Clarity session recording lists and dashboard questions through the endpoints of the official Clarity MCP server.
- Count each live Clarity call in a daily ledger, and let parallel server processes wait for 1 fetch of the same answer.
- List in the README what each dashboard shows and the API does not give.
