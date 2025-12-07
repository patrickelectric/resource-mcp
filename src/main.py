import os
import re
from pathlib import Path
from urllib.parse import urlparse

from fastmcp import FastMCP
from gitingest import ingest_async
from github import Github
from loguru import logger
import wikipedia
from wikipedia import exceptions as wiki_exceptions


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCE_DIR = PROJECT_ROOT / "resource"


def _ensure_resource_dir() -> Path:
    """Ensure the resource directory exists and return its path."""
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    return RESOURCE_DIR


def _slugify_filename(name: str, default: str = "resource") -> str:
    """Create a filesystem-safe filename from a title-like string."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return slug or default


def create_mcp() -> FastMCP:
    """Create and configure the MCP server with resource tools."""
    resource_dir = _ensure_resource_dir()
    wikipedia_dir = resource_dir / "wikipedia"
    wikipedia_dir.mkdir(parents=True, exist_ok=True)
    mcp = FastMCP("resource-mcp")

    @mcp.tool
    def serve_manifest_mcp_json():
        with open(".well-known/mcp.json", "r") as f:
            return json.load(f)

    @mcp.tool
    def list_database() -> list[str]:
        """List all files under the resource directory. This is my personal database directory."""
        if not resource_dir.exists():
            return []

        files = [
            str(path.relative_to(resource_dir))
            for path in resource_dir.rglob("*")
            if path.is_file()
        ]
        files.sort()
        return files

    @mcp.tool
    def cat_database(path: str) -> str:
        """Return the contents of a file under the resource directory."""
        target = (resource_dir / path).resolve()
        root = resource_dir.resolve()

        logger.info(f"cat_database requested for {path}")
        if root not in target.parents and target != root:
            raise ValueError("Requested path is outside the resource directory.")

        if not target.is_file():
            raise FileNotFoundError(f"Resource file not found: {path}")

        logger.info(f"cat_database reading {target}")
        return target.read_text()

    @mcp.tool
    def search_wikipedia(query: str, limit: int = 10) -> dict[str, str | list[str] | None]:
        """
        Search Wikipedia for article titles and optional suggestion.
        """
        limit = max(1, min(limit, 50))
        logger.info(f"search_wikipedia query='{query}' limit={limit}")
        try:
            results = wikipedia.search(query, results=limit, suggestion=True)
        except Exception as exc:  # wikipedia raises generic exceptions for many errors
            raise RuntimeError(f"Failed to search Wikipedia: {exc}") from exc

        titles, suggestion = results if isinstance(results, tuple) else (results, None)
        return {"results": titles, "suggestion": suggestion}

    @mcp.tool
    def fetch_wikipedia_content(title: str) -> dict[str, str]:
        """
        Retrieve a Wikipedia page's content and save it under resource/wikipedia.
        """
        logger.info(f"fetch_wikipedia_content title='{title}'")
        try:
            page = wikipedia.page(title=title, auto_suggest=False)
            content = page.content
        except wiki_exceptions.DisambiguationError as exc:
            options = ", ".join(exc.options[:5])
            raise ValueError(f"Title is ambiguous: {title}. Try one of: {options}") from exc
        except wiki_exceptions.PageError as exc:
            raise FileNotFoundError(f"Wikipedia page not found: {title}") from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch Wikipedia page: {exc}") from exc

        safe_title = _slugify_filename(page.title, default="wikipedia-page")
        target = wikipedia_dir / f"{safe_title}.md"
        target.write_text(content)
        logger.info(f"fetch_wikipedia_content wrote {target}")

        return {
            "title": page.title,
            "path": str(target.relative_to(resource_dir)),
            "content": content,
        }

    @mcp.tool
    async def gitingest(url: str) -> str:
        """
        Fetch a repository digest via GitIngest, save as markdown, and return it.

        The digest is written to `<resource>/<name>.md`, where `<name>` comes from
        the last path segment of the provided URL.
        """
        logger.info(f"gitingest called for {url}")
        parsed = urlparse(url)
        segments = [
            re.sub(r"[^a-zA-Z0-9._-]+", "-", part) or "resource"
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        if not segments:
            segments = ["resource"]
        segments[-1] = f"{segments[-1]}.md"
        target = resource_dir.joinpath(*segments)
        target.parent.mkdir(parents=True, exist_ok=True)

        summary, tree, content = await ingest_async(url)
        digest = f"{summary}\n\n{tree}\n\n{content}"

        target.write_text(digest)
        logger.info(f"gitingest wrote digest to {target}")
        return digest

    @mcp.tool
    def search_repos(query: str, limit: int = 10) -> list[dict[str, str | None]]:
        """
        Search GitHub repositories with PyGithub.

        Uses `GITHUB_TOKEN` if set for higher rate limits; otherwise unauthenticated.
        Returns up to `limit` results with key repo fields.
        """
        token = os.getenv("GITHUB_TOKEN")
        gh = Github(login_or_token=token) if token else Github()

        limit = max(1, min(limit, 50))
        logger.info(f"search_repos query='{query}' limit={limit}")
        results = []
        for repo in gh.search_repositories(query=query)[:limit]:
            results.append(
                {
                    "full_name": repo.full_name,
                    "html_url": repo.html_url,
                    "description": repo.description,
                    "language": repo.language,
                    "stargazers_count": repo.stargazers_count,
                }
            )

        return results

    return mcp


def main() -> None:
    """Start the MCP server."""
    mcp = create_mcp()
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=1234,
        path="/resource",
    )


if __name__ == "__main__":
    main()
