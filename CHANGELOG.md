# Changelog

## Unreleased

- Add a time frame, 1 page, 1 query, and a comparison of 2 periods or 2 pages to `get_seo_data`.
- A source that cannot apply a parameter now reports status `unsupported` and does not run.
- Add the `query_api` tool: 1 official read query for Search Console, GA4, PageSpeed, the Chrome UX Report, Bing, and Clarity, with a list of valid fields and values.
- Add Clarity session recording lists and dashboard questions through the endpoints of the official Clarity MCP server.
- Count each live Clarity call in a daily ledger, and let parallel server processes wait for 1 fetch of the same answer.
- Check each Clarity recording filter value before a call spends quota, and list the valid values, the URL operators, and the units in `query_api`.
- Show the question that Clarity answered in `dashboard_query`, and label `recordings` as a sample.
- Add `task` to `query_api`: an agent asks for a thing such as dead clicks, and gets the exact calls, the fields, and the meaning for each provider, with no network call.
- Fix the Bing methods that take a URL: the API refused the URL in quotes with HTTP 400.
- Fix the Clarity cache key: it holds the project, so a changed `clarity_project` cannot read the answer of the old project.
- Show quota with no network call in `list_sites` and `query_api` discovery: the used count, the limit, and what is left, with the published limits and doc links of each API.
- Show `calls_used_today`, `daily_limit`, `calls_left`, and the reset time in each answer that spends quota, also in a failure. Add `quota_warning` when few calls are left.
- Count URL Inspection calls in a daily ledger, and read the real GA4 token quota from each report.
- Stop the day when the Clarity Data Export API answers HTTP 429.
- Add a rule to the Clarity click tasks: when the dashboard count is 0, do not call `recordings`.
- List in the README what each dashboard shows and the API does not give.
- Fix the call ledgers: a day that is not a real date, or is after today, now fails closed as a broken ledger. It restarted the count before.
- Stop a second Clarity call after a failure: a waiter or a repeat of a request that can have reached the API gets the same failure for 60 seconds and spends nothing.
- Fix a bad HTTP 200 answer of Clarity: the server checks the shape before it saves an answer, so a source no longer fails with `AttributeError` until the next UTC day. The reason says what happened.
- Add a 2 second limit to the ledger lock: a stuck process no longer freezes all sources, and a metered call that cannot get the lock sends nothing.
