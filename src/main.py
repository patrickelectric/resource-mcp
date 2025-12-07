import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastmcp import FastMCP
from gitingest import ingest_async
from github import Github
from loguru import logger
import wikipedia
from wikipedia import exceptions as wiki_exceptions
from youtube_transcript_api import YouTubeTranscriptApi


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


def _extract_video_id(value: str) -> str:
    """
    Extract the YouTube video ID from a URL or raw ID.

    Supports full URLs, share links, and bare 11-character IDs.
    """
    parsed = urlparse(value.strip())
    if parsed.scheme and parsed.netloc:  # URL form
        if parsed.netloc in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.lstrip("/")
        else:
            query_params = parse_qs(parsed.query)
            candidate = query_params.get("v", [parsed.path.split("/")[-1]])[0]
    else:
        candidate = value

    video_id = re.sub(r"[^a-zA-Z0-9_-]", "", candidate)
    if len(video_id) != 11:
        raise ValueError("Could not extract a valid 11-character YouTube video ID.")
    return video_id


def create_mcp() -> FastMCP:
    """Create and configure the MCP server with resource tools."""
    resource_dir = _ensure_resource_dir()
    wikipedia_dir = resource_dir / "wikipedia"
    wikipedia_dir.mkdir(parents=True, exist_ok=True)
    youtube_dir = resource_dir / "youtube"
    youtube_dir.mkdir(parents=True, exist_ok=True)
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
    def get_youtube_transcript(
        video_url: str,
        languages: list[str] | None = None,
        save: bool = True,
    ) -> dict[str, str | list[dict[str, float | str]] | None]:
        """
        Fetch the transcript for a YouTube video.

        - Accepts a full YouTube URL or a bare 11-character video ID.
        - `languages` is an ordered preference list (e.g. ["en", "en-US", "es"]);
          defaults to English if not provided.
        - When `save` is True, the transcript is stored as JSON under
          `resource/youtube/<video_id>.json` and the relative path is returned.
        """
        video_id = _extract_video_id(video_url)
        lang_pref = [languages] if isinstance(languages, str) else languages
        lang_pref = lang_pref or ["en"]

        logger.info(f"get_youtube_transcript video_id='{video_id}' langs={lang_pref}")
        try:
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=lang_pref)
            transcript = fetched.to_raw_data()
            language = fetched.language
        except Exception as exc:
            raise RuntimeError(f"Error while fetching YouTube transcript: {exc}") from exc

        path: str | None = None
        if save:
            target = youtube_dir / f"{_slugify_filename(video_id)}.json"
            payload = {
                "video_id": video_id,
                "language": language,
                "transcript": transcript,
            }
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            path = str(target.relative_to(resource_dir))
            logger.info(f"get_youtube_transcript wrote {target}")

        return {"video_id": video_id, "language": language, "path": path, "transcript": transcript}

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


#
# Expose a module-level MCP instance so FastMCP CLI tools can discover it.
#
mcp = create_mcp()


def main() -> None:
    """Start the MCP server."""
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=1234,
        path="/mcp",
    )


if __name__ == "__main__":
    main()
