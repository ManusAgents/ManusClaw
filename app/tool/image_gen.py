from __future__ import annotations
"""Image generation tool — uses FAL.ai or falls back to a descriptive mock."""
import os
from app.tool.base import BaseTool
from app.schema import ToolResult


class ImageGenerationTool(BaseTool):
    name = "image_generate"
    description = (
        "Generate an image from a text prompt. Uses FAL.ai if FAL_KEY is set, "
        "otherwise returns a descriptive mock. Saves image to workspace/images/."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Image description prompt"},
            "size":   {"type": "string", "description": "e.g. 1024x1024", "default": "1024x1024"},
            "model":  {"type": "string", "description": "FAL model slug", "default": "fal-ai/flux/schnell"},
        },
        "required": ["prompt"],
    }

    async def execute(self, prompt: str, size: str = "1024x1024", model: str = "fal-ai/flux/schnell") -> ToolResult:
        fal_key = os.getenv("FAL_KEY", "")
        if fal_key:
            return await self._real_generate(prompt, size, model, fal_key)
        return self._mock_generate(prompt, size)

    async def _real_generate(self, prompt: str, size: str, model: str, api_key: str) -> ToolResult:
        try:
            import aiohttp
            from pathlib import Path
            w, h = (size.split("x") + ["1024", "1024"])[:2]
            payload = {"prompt": prompt, "image_size": {"width": int(w), "height": int(h)}}
            headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as s:
                async with s.post(f"https://fal.run/{model}", json=payload, headers=headers) as r:
                    data = await r.json()
            url = (data.get("images") or [{}])[0].get("url", "")
            if url:
                out_dir = Path("workspace/images")
                out_dir.mkdir(parents=True, exist_ok=True)
                slug = prompt[:30].replace(" ", "_").lower()
                fname = out_dir / f"{slug}.png"
                async with aiohttp.ClientSession() as s:
                    async with s.get(url) as r:
                        fname.write_bytes(await r.read())
                return ToolResult(output=f"Image saved to {fname}\nURL: {url}")
            return ToolResult(error="FAL returned no image URL")
        except Exception as e:
            return ToolResult(error=f"Image generation error: {e}")

    def _mock_generate(self, prompt: str, size: str) -> ToolResult:
        return ToolResult(
            output=(
                f"[MockImageGen] Would generate:\n"
                f"  Prompt: {prompt}\n"
                f"  Size: {size}\n"
                f"  Set FAL_KEY for real generation via FAL.ai"
            )
        )
