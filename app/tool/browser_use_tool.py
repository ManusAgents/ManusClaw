from __future__ import annotations

import base64
from typing import Any, Optional

from app.schema import ToolResult
from app.tool.base import BaseTool
from app.logger import logger


class BrowserUseTool(BaseTool):
    name = "browser_use"
    description = (
        "Control a Playwright browser. Actions: navigate, click, type, screenshot, "
        "get_text, execute_js, new_tab, close_tab, back, forward."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "screenshot", "get_text",
                         "execute_js", "new_tab", "close_tab", "back", "forward"],
                "description": "Browser action to perform.",
            },
            "url": {"type": "string", "description": "URL for navigate/new_tab."},
            "selector": {"type": "string", "description": "CSS selector for click/type."},
            "text": {"type": "string", "description": "Text to type."},
            "js_code": {"type": "string", "description": "JavaScript to execute."},
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._browser: Any = None
        self._page: Any = None
        self._playwright: Any = None

    async def _ensure_browser(self) -> None:
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
                self._page = await self._browser.new_page()
            except ImportError:
                raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

    async def execute(self, action: str, url: Optional[str] = None,
                      selector: Optional[str] = None, text: Optional[str] = None,
                      js_code: Optional[str] = None, **_: Any) -> ToolResult:
        try:
            await self._ensure_browser()
        except RuntimeError as e:
            return ToolResult(error=str(e))

        page = self._page

        if action == "navigate":
            if not url:
                return ToolResult(error="url is required for navigate.")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return ToolResult(output=f"Navigated to {url}. Title: {await page.title()}")

        if action == "click":
            if not selector:
                return ToolResult(error="selector is required for click.")
            await page.click(selector)
            return ToolResult(output=f"Clicked {selector}.")

        if action == "type":
            if not selector or text is None:
                return ToolResult(error="selector and text required for type.")
            await page.fill(selector, text)
            return ToolResult(output=f"Typed into {selector}.")

        if action == "screenshot":
            img_bytes = await page.screenshot(type="jpeg", quality=80)
            b64 = base64.b64encode(img_bytes).decode()
            return ToolResult(output="Screenshot taken.", base64_image=b64)

        if action == "get_text":
            content = await page.evaluate("document.body.innerText")
            return ToolResult(output=content[:5000])

        if action == "execute_js":
            if not js_code:
                return ToolResult(error="js_code required.")
            result = await page.evaluate(js_code)
            return ToolResult(output=str(result))

        if action == "new_tab":
            self._page = await self._browser.new_page()
            if url:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return ToolResult(output="New tab opened.")

        if action == "close_tab":
            await page.close()
            pages = self._browser.contexts[0].pages if self._browser.contexts else []
            self._page = pages[-1] if pages else await self._browser.new_page()
            return ToolResult(output="Tab closed.")

        if action == "back":
            await page.go_back()
            return ToolResult(output="Went back.")

        if action == "forward":
            await page.go_forward()
            return ToolResult(output="Went forward.")

        return ToolResult(error=f"Unknown action: {action}")

    async def cleanup(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._page = None
        self._playwright = None
