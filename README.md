# resource-mcp

Minimal MCP server built with FastMCP and packaged for uv.

## Development

1. Run the server:
   ```bash
   uv run src/main.py
   ```
   The server listens on `0.0.0.0:1234` at the `/resource` path.

### Tools

- `list_resources()`: returns all files under the project-level `resource/` directory (relative paths).
- `cat_resource(path)`: returns the contents of a file inside the `resource/` directory.
- `gitingest(url)`: downloads a repository digest via GitIngest, saves it as `<path>.md` mirroring the URL path under `resource/`, and returns the combined summary/tree/content. Example: `gitingest("https://gitingest.com/eclipse-zenoh/zenoh")` writes `resource/eclipse-zenoh/zenoh.md`.
- `search_repos(query, limit=10)`: searches GitHub repositories via PyGithub and returns up to `limit` results with basic metadata. Uses `GITHUB_TOKEN` if set for higher rate limits.

### Codex integration

Add the MCP endpoint to Codex:

```bash
codex mcp add resource --url http://0.0.0.0:1234/resource
```
