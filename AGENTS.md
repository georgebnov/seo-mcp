# Agent guide: seo-mcp

A read-only stdio MCP server. Python, uv, ruff, mypy, pytest. Run `just check` before each PR.

## Rules

- Read-only. A provider calls only the endpoints in its `ENDPOINTS` list. `net.py` is the only module that opens a connection.
- Results, errors, and logs never hold a secret. A reason is fixed text, never exception text or an upstream body.
- Scrub first, then cut text.
- The repo holds no private data: no real site, property id, home path, or key. `scripts/private_scan.py` checks files and git history.
- Tests mock only the network edge (`net.transport`).
- Keep it small. One file for each provider. Do not add a tool: add a source.
- Commits use Conventional Commits. Work on a branch and open a PR.
