# Agent guide: seo-mcp

A read-only stdio MCP server. Python, uv, ruff, mypy, pytest. Run `just check` before each PR.

## Rules

- Read-only. A provider calls only the endpoints in its `ENDPOINTS` list. `net.py` is the only module that opens a connection.
- Results, errors, and logs never hold a secret. A reason is fixed text, never exception text or an upstream body. 1 exception: on HTTP 400, `query_api` shows the `error.message` field of a Google API. `net.request(explain=True)` asks for it. The text is scrubbed, then cut.
- Scrub first, then cut text.
- The repo holds no private data: no real site, property id, home path, or key. `scripts/private_scan.py` checks files and git history.
- Tests fake only the network edge (`net.transport`). A test can also set these inputs, and nothing else:
  - The clock: `params._clock`, and `datetime` in a module, replaced by a `datetime` subclass in the test file that overrides only `now()`.
  - The environment and the working folder: `monkeypatch.setenv`, `delenv`, and `chdir`. The config file and the cache folder come from env vars.
  - The standard input: `sys.stdin`.
  - The time and size limits: `core.SOURCE_TIMEOUT_SECONDS`, `quota.LOCK_WAIT_SECONDS`, and `shared.PRUNE_BATCH`.
  - The import of a real script of `scripts/`: `sys.path` and `sys.modules`. A test can empty `_google._tokens`.
  - `tests/test_fakes.py` fails on each other fake. A new input goes on its `ALLOWED` list and on this list.
- Keep it small. One file for each provider: `pagespeed.py` holds PageSpeed and the Chrome UX Report, and `_google.py` holds the shared Google sign-in. Add no dependency: use the standard library or a dependency that `pyproject.toml` already has.
- The server has 3 tools: `list_sites`, `get_seo_data`, and `query_api`. `tests/test_stdio.py` pins them. Do not add a tool: add a source, or add a method to `APIS` for `query_api`.
- Setup is a terminal command, `seo-mcp setup`, that the user runs. It is not an MCP tool.
- A way to get a thing goes in `tasks.py` as a task. A step that names a method, a field, or a filter must run in `tests/test_tasks.py`.
- A source never ignores a parameter. It declares what it applies in `ACCEPTS`, and it does not run with any other parameter.
- Each live Clarity call goes through `_cached` in `clarity.py`, so that it spends from the ledger.
- Each API call goes through `shared.request` or `shared.once`, and a call that spends from a ledger uses `shared.once`. A repeat then sends nothing. The Google token call is the 1 exception. `tests/test_shared.py` lists the only direct `net.request` calls.
- A cache key holds the credential. Build it with `shared.entry` or `shared.named` and `auth=shared.identity(...)`. A ledger key never holds the credential.
- An endpoint with a daily call limit names its ledger in `metered`. `net.request` refuses it without a receipt from `quota.spend`.
- A lock goes through `quota.take_lock`. A blocking `flock` freezes the event loop, and `tests/test_lock_timeout.py` fails on one.
- An API with no number in its docs says so in `Quota.limits`. Never put a number there from a source that is not the official docs.
- A change to a flow in `models/` updates the model. Run `just models`. Only a rule that the code breaks today goes in `models/known-breaks.txt`.
- Commits use Conventional Commits. Work on a branch and open a PR.
