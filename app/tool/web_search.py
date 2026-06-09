from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import Any

from app.config import Config
from app.logger import logger
from app.schema import ToolResult
from app.tool.base import BaseTool


async def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    try:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddg:
            results = await ddg.atext(query, max_results=max_results)
            return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in results]
    except Exception as e:
        logger.debug(f"DuckDuckGo search failed: {e}")
        return []


async def _search_bing(query: str, max_results: int) -> list[dict]:
    try:
        import aiohttp
        encoded = urllib.parse.quote(query)
        url = f"https://www.bing.com/search?q={encoded}&count={max_results}"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ManusClawBot/1.0)"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                html = await resp.text()
        import re
        results = []
        # FIX: Bing's HTML structure changed — use multiple patterns for resilience.
        # Pattern 1: Modern Bing — cite elements with data-url or href
        for m in re.finditer(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL
        ):
            if len(results) >= max_results:
                break
            url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if url.startswith("http") and title and "/search?" not in url:
                results.append({"title": title, "url": url, "snippet": ""})
        # Pattern 2: Fallback — li.b_algo result blocks
        if not results:
            for m in re.finditer(
                r'<li[^>]*class="b_algo"[^>]*>.*?<h2[^>]*>.*?href="([^"]+)"[^>]*>(.*?)</h2>',
                html, re.DOTALL
            ):
                if len(results) >= max_results:
                    break
                url = m.group(1)
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if url.startswith("http") and title:
                    results.append({"title": title, "url": url, "snippet": ""})
        return results
    except Exception as e:
        logger.debug(f"Bing search failed: {e}")
        return []


class WebSearch(BaseTool):
    name = "web_search"
    description = (
        "Search the web using a fallback chain: DuckDuckGo → Bing. "
        "Returns a list of results with titles, URLs and snippets."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "description": "Max results to return (default 5).", "default": 5},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, max_results: int | None = None, **_: Any) -> ToolResult:
        cfg = Config.get()
        engines = cfg.search.engines
        effective_max_results = max_results if max_results is not None else cfg.search.max_results

        for engine in engines:
            wait = 0.0
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(wait)
                    wait = min(wait * 2 + random.uniform(0.5, 1.5), 30)
                results: list[dict] = []
                try:
                    if engine == "duckduckgo":
                        results = await _search_duckduckgo(query, effective_max_results)
                    elif engine == "bing":
                        results = await _search_bing(query, effective_max_results)
                    else:
                        continue
                except Exception:
                    continue

                if results:
                    formatted = "\n\n".join(
                        f"[{i+1}] {r['title']}\n    {r['url']}\n    {r['snippet']}"
                        for i, r in enumerate(results)
                    )
                    return ToolResult(output=f"Search results for '{query}':\n\n{formatted}")

        return ToolResult(
            error="All search engines failed. Check your internet connection.",
            output=f"No results found for '{query}'.",
        )
