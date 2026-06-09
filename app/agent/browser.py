from __future__ import annotations

from typing import Optional

from app.agent.toolcall import ToolCallAgent
from app.tool.base import ToolCollection
from app.tool.browser_use_tool import BrowserUseTool
from app.tool.crawl4ai import Crawl4AITool
from app.tool.terminate import Terminate
from app.tool.web_search import WebSearch


BROWSER_PROMPT = """\
You are a browser-focused AI agent. You navigate the web, interact with pages, \
extract information, and fill forms. Use the browser_use and crawl tools. \
When done, call terminate.
"""


class BrowserAgent(ToolCallAgent):
    name = "browser"
    system_prompt = BROWSER_PROMPT

    def __init__(self, mode=None, session_id: Optional[str] = None) -> None:
        tools = ToolCollection(
            BrowserUseTool(),
            WebSearch(),
            Crawl4AITool(),
            Terminate(),
        )
        super().__init__(tools=tools, mode=mode, session_id=session_id)
