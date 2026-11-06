# Agent guide: seo-mcp

A read-only stdio MCP server. Python, uv, ruff, mypy, pytest. Run `just check` before each PR.

## Rules

- Read-only. A provider calls only the endpoints in its `ENDPOINTS` list. `net.py` is the only module that opens a connection.
- Results, errors, and logs never hold a secret. A reason is fixed text, never exception text or an upstream body. 1 exception: on HTTP 400, `query_api` shows the `error.message` field of a Google API. `net.request(explain=True)` asks for it. The text is scrubbed, then cut.
- Scrub first, then cut text.
- The repo holds no private data: no real site, property id, home path, or key. `scripts/private_scan.py` checks files and git history.
- Tests mock only the network edge (`net.transport`).
- Keep it small. One file for each provider. Do not add a tool: add a source, or add a method to `APIS` for `query_api`.
- A way to get a thing goes in `tasks.py` as a task. A step that names a method, a field, or a filter must run in `tests/test_tasks.py`.
- A source never ignores a parameter. It declares what it applies in `ACCEPTS`, and it does not run with any other parameter.
- Each live Clarity call goes through `_cached` in `clarity.py`, so that it spends from the ledger.
- An endpoint with a daily call limit names its ledger in `metered`. `net.request` refuses it without a receipt from `quota.spend`.
- A lock goes through `quota.take_lock`. A blocking `flock` freezes the event loop, and `tests/test_lock_timeout.py` fails on one.
- An API with no number in its docs says so in `Quota.limits`. Never put a number there from a source that is not the official docs.
- A change to a flow in `models/` updates the model. Run `just models`. Only a rule that the code breaks today goes in `models/known-breaks.txt`.
- Commits use Conventional Commits. Work on a branch and open a PR.
