from __future__ import annotations

from typing import Any

from app.schema import ToolResult
from app.tool.base import BaseTool
from app.logger import logger


class Crawl4AITool(BaseTool):
    name = "crawl"
    description = (
        "Extract clean, readable content from a webpage (including JavaScript-heavy sites). "
        "Falls back to aiohttp + simple HTML stripping if crawl4ai is not installed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to crawl."},
            "max_length": {
                "type": "integer",
                "description": "Max characters of content to return (default 8000).",
                "default": 8000,
            },
        },
        "required": ["url"],
    }

    async def execute(self, url: str, max_length: int = 8000, **_: Any) -> ToolResult:
        # Try crawl4ai first
        try:
            from crawl4ai import AsyncWebCrawler
            async with AsyncWebCrawler(verbose=False) as crawler:
                result = await crawler.arun(url=url)
                content = result.markdown or result.extracted_content or ""
                return ToolResult(output=content[:max_length])
        except ImportError:
            logger.debug("crawl4ai not installed; falling back to aiohttp.")
        except Exception as e:
            logger.debug(f"crawl4ai failed: {e}; falling back.")

        # Fallback: aiohttp + strip HTML
        try:
            import aiohttp
            import re

            headers = {"User-Agent": "Mozilla/5.0 (compatible; ManusClawBot/1.0)"}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    html = await resp.text()  # Fix: remove invalid 'errors' kwarg (not supported by aiohttp)

            # Strip tags
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return ToolResult(output=text[:max_length])
        except Exception as e:
            return ToolResult(error=f"Crawl failed: {e}")
