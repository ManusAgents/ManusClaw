from __future__ import annotations

"""
NonNativeToolCallingMixin — Function Calling for Models Without Native Support
==============================================================================

Many LLMs (especially open-source models) do not support native function/tool
calling. This mixin converts tool schemas into prompt instructions and parses
structured responses back into tool calls.

Features:
- Converts OpenAI-format tool schemas to prompt instructions
- Parses structured responses back into tool calls
- Handles JSON extraction from various formats (code blocks, tags, raw JSON)
- Retry on parse failures with configurable attempts
- Support for multi-tool responses
- Robust error handling with graceful degradation

Usage::

    class MyLLM(NonNativeToolCallingMixin):
        async def _raw_chat(self, messages, **kwargs):
            # Your raw LLM call here
            ...

    llm = MyLLM()
    response = await llm.ask_tool(messages, tools=tool_defs)
    # response.tool_calls will be populated even if the model
    # doesn't support native function calling
"""

import json
import re
from typing import Any, Optional

from app.logger import logger
from app.schema import Message, Role, ToolCall, Function


# ──────────────────────────────────────────────────────────────────────────────
# Tool Schema to Prompt Conversion
# ──────────────────────────────────────────────────────────────────────────────

_TOOL_SYSTEM_PROMPT_TEMPLATE = """You are a helpful assistant that can use tools to accomplish tasks.

You have access to the following tools:

{tool_descriptions}

When you need to use a tool, you MUST respond with a JSON code block in the following format:

```json
{{
  "tool_calls": [
    {{
      "name": "<tool_name>",
      "arguments": {{
        <parameter_name>: <parameter_value>
      }}
    }}
  ]
}}
```

You can call multiple tools at once by adding more objects to the "tool_calls" array.

If you do not need to use any tool, just respond with your text directly without any JSON block.

IMPORTANT RULES:
1. Always use the exact tool names as listed above.
2. Always provide all required parameters for each tool.
3. Parameter values must match the expected types (string, number, boolean, etc.).
4. You can mix text and tool calls in your response, but tool calls must be in the JSON block format.
5. If a tool call fails, analyze the error and try again with corrected parameters.
"""

_TOOL_DESCRIPTION_TEMPLATE = """### {name}
{description}

Parameters:
{parameters}"""

_PARAM_TEMPLATE = """- {name} ({type}){required}: {description}"""

_OPTIONAL_PARAM_TEMPLATE = """- {name} ({type}, optional): {description}"""


def tools_to_prompt(tools: list[dict[str, Any]]) -> str:
    """Convert OpenAI-format tool definitions to a system prompt.

    Args:
        tools: List of tool definitions in OpenAI format:
            [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]

    Returns:
        A system prompt string describing the available tools.
    """
    tool_descriptions: list[str] = []

    for tool in tools:
        func = tool.get("function", {})
        name = func.get("name", "unknown")
        description = func.get("description", "No description available.")
        params_schema = func.get("parameters", {})

        # Build parameter descriptions
        param_lines: list[str] = []
        properties = params_schema.get("properties", {})
        required = set(params_schema.get("required", []))

        for param_name, param_schema in properties.items():
            if not isinstance(param_schema, dict):
                continue
            param_type = param_schema.get("type", "any")
            param_desc = param_schema.get("description", "")
            is_required = param_name in required

            if is_required:
                param_lines.append(
                    _PARAM_TEMPLATE.format(
                        name=param_name,
                        type=param_type,
                        required=" (required)",
                        description=param_desc,
                    )
                )
            else:
                param_lines.append(
                    _OPTIONAL_PARAM_TEMPLATE.format(
                        name=param_name,
                        type=param_type,
                        description=param_desc,
                    )
                )

        if not param_lines:
            param_lines.append("- (no parameters)")

        tool_desc = _TOOL_DESCRIPTION_TEMPLATE.format(
            name=name,
            description=description,
            parameters="\n".join(param_lines),
        )
        tool_descriptions.append(tool_desc)

    return _TOOL_SYSTEM_PROMPT_TEMPLATE.format(
        tool_descriptions="\n\n".join(tool_descriptions)
    )


# ──────────────────────────────────────────────────────────────────────────────
# JSON Extraction from Responses
# ──────────────────────────────────────────────────────────────────────────────

def extract_json_from_response(text: str) -> Optional[dict[str, Any]]:
    """Extract JSON from a model response in various formats.

    Handles:
    - JSON code blocks (```json ... ```)
    - Generic code blocks (``` ... ```)
    - XML-style tags (<tool_call>...</tool_call>)
    - Raw JSON objects
    - Mixed text and JSON

    Args:
        text: The raw model response text.

    Returns:
        Parsed JSON dict, or None if no JSON found.
    """
    if not text or not text.strip():
        return None

    # Strategy 1: Extract from ```json code blocks
    json_block = _extract_json_code_block(text)
    if json_block is not None:
        return json_block

    # Strategy 2: Extract from generic code blocks
    generic_block = _extract_generic_code_block(text)
    if generic_block is not None:
        return generic_block

    # Strategy 3: Extract from XML-style tags
    xml_block = _extract_xml_tag_json(text)
    if xml_block is not None:
        return xml_block

    # Strategy 4: Find raw JSON object in text
    raw_json = _extract_raw_json(text)
    if raw_json is not None:
        return raw_json

    return None


def _extract_json_code_block(text: str) -> Optional[dict[str, Any]]:
    """Extract JSON from a ```json code block."""
    pattern = r"```json\s*\n?(.*?)\n?\s*```"
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        result = _try_parse_json(match)
        if result is not None:
            return result
    return None


def _extract_generic_code_block(text: str) -> Optional[dict[str, Any]]:
    """Extract JSON from a generic code block."""
    pattern = r"```\s*\n?(.*?)\n?\s*```"
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        result = _try_parse_json(match)
        if result is not None:
            return result
    return None


def _extract_xml_tag_json(text: str) -> Optional[dict[str, Any]]:
    """Extract JSON from XML-style tags like <tool_call>...</tool_call>."""
    # Common tag names used for tool calls
    tag_names = ["tool_call", "tool_calls", "function_call", "function_calls", "json", "response"]
    for tag in tag_names:
        pattern = rf"<{tag}>(.*?)</{tag}>"
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        for match in matches:
            result = _try_parse_json(match)
            if result is not None:
                return result
    return None


def _extract_raw_json(text: str) -> Optional[dict[str, Any]]:
    """Find a raw JSON object in the text."""
    # Look for JSON objects starting with {
    brace_depth = 0
    start = -1

    for i, char in enumerate(text):
        if char == '{':
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif char == '}':
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                candidate = text[start:i + 1]
                result = _try_parse_json(candidate)
                if result is not None:
                    return result
                start = -1

    return None


def _try_parse_json(text: str) -> Optional[dict[str, Any]]:
    """Try to parse a string as JSON, with fallback repairs."""
    text = text.strip()
    if not text:
        return None

    # Direct parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try fixing common issues:

    # 1. Trailing commas
    cleaned = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 2. Single quotes instead of double quotes
    try:
        # Replace single-quoted keys/values with double-quoted
        fixed = re.sub(r"'([^']*)'", r'"\1"', cleaned)
        result = json.loads(fixed)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 3. Remove comments (// and /* */)
    no_comments = re.sub(r'//.*?$', '', cleaned, flags=re.MULTILINE)
    no_comments = re.sub(r'/\*.*?\*/', '', no_comments, flags=re.DOTALL)
    try:
        result = json.loads(no_comments)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 4. Unquoted keys
    # Add quotes around keys that aren't quoted
    fixed_keys = re.sub(r'(\{|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1 "\2":', no_comments)
    try:
        result = json.loads(fixed_keys)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    return None


def parse_tool_calls_from_json(
    data: dict[str, Any],
    tool_call_prefix: str = "tc_",
) -> list[ToolCall]:
    """Parse tool calls from the extracted JSON data.

    Handles various formats:
    - {"tool_calls": [{"name": ..., "arguments": ...}]}
    - {"name": ..., "arguments": ...}  (single tool call)
    - {"function": "name", "parameters": {...}}

    Args:
        data: The parsed JSON data.
        tool_call_prefix: Prefix for generated tool call IDs.

    Returns:
        List of ToolCall objects.
    """
    tool_calls: list[ToolCall] = []

    # Format 1: {"tool_calls": [...]}
    if "tool_calls" in data:
        calls = data["tool_calls"]
        if isinstance(calls, list):
            for i, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                tc = _parse_single_tool_call(call, f"{tool_call_prefix}{i}", tool_call_prefix)
                if tc:
                    tool_calls.append(tc)
        elif isinstance(calls, dict):
            tc = _parse_single_tool_call(calls, f"{tool_call_prefix}0", tool_call_prefix)
            if tc:
                tool_calls.append(tc)

    # Format 2: Single tool call at top level
    elif "name" in data and ("arguments" in data or "parameters" in data or "args" in data):
        tc = _parse_single_tool_call(data, f"{tool_call_prefix}0", tool_call_prefix)
        if tc:
            tool_calls.append(tc)

    # Format 3: {"function": "name", "parameters": {...}}
    elif "function" in data:
        tc = _parse_single_tool_call(
            {"name": data["function"], "arguments": data.get("parameters", data.get("arguments", {}))},
            f"{tool_call_prefix}0",
            tool_call_prefix,
        )
        if tc:
            tool_calls.append(tc)

    # Format 4: {"action": "name", "action_input": {...}} (ReAct format)
    elif "action" in data:
        tc = _parse_single_tool_call(
            {"name": data["action"], "arguments": data.get("action_input", {})},
            f"{tool_call_prefix}0",
            tool_call_prefix,
        )
        if tc:
            tool_calls.append(tc)

    # Format 5: {"tool": "name", "input": {...}}
    elif "tool" in data:
        tc = _parse_single_tool_call(
            {"name": data["tool"], "arguments": data.get("input", data.get("arguments", {}))},
            f"{tool_call_prefix}0",
            tool_call_prefix,
        )
        if tc:
            tool_calls.append(tc)

    return tool_calls


def _parse_single_tool_call(
    call: dict[str, Any],
    default_id: str,
    tool_call_prefix: str,
) -> Optional[ToolCall]:
    """Parse a single tool call from a dict.

    Args:
        call: Dict with tool call data.
        default_id: Default ID to use if none is provided.
        tool_call_prefix: Prefix for generated tool call IDs.

    Returns:
        A ToolCall object, or None if parsing fails.
    """
    try:
        name = call.get("name", call.get("function", call.get("tool", "")))
        if not name or not isinstance(name, str):
            return None

        # Extract arguments
        args = call.get("arguments", call.get("parameters", call.get("args", call.get("input", call.get("action_input", {})))))

        # Arguments can be a dict or a JSON string
        if isinstance(args, dict):
            args_json = json.dumps(args)
        elif isinstance(args, str):
            # Validate it's valid JSON
            try:
                json.loads(args)
                args_json = args
            except json.JSONDecodeError:
                # Try wrapping it
                args_json = json.dumps({"value": args})
        else:
            args_json = json.dumps({})

        # Get or generate ID
        call_id = call.get("id", f"{tool_call_prefix}{abs(hash(name + args_json)) % 100000}")

        return ToolCall(
            id=call_id,
            type="function",
            function=Function(name=name, arguments=args_json),
        )

    except Exception as e:
        logger.debug(f"[NonNativeToolCalling] Failed to parse tool call: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# NonNativeToolCallingMixin
# ──────────────────────────────────────────────────────────────────────────────

class NonNativeToolCallingMixin:
    """Mixin that adds tool calling support to LLM clients that don't have it natively.

    This mixin intercepts tool-calling requests and:
    1. Converts tool schemas into prompt instructions
    2. Sends the modified prompt to the LLM
    3. Parses the structured response back into tool calls
    4. Retries on parse failures

    To use this mixin, the class must implement:
    - ``_raw_chat(messages, **kwargs) -> dict``: The raw LLM chat method
    - ``model`` property or attribute: The model name

    Usage::

        class MyClient(NonNativeToolCallingMixin):
            def __init__(self, model):
                self.model = model

            async def _raw_chat(self, messages, **kwargs):
                # Raw LLM call without tool support
                ...

        client = MyClient(model="my-model")
        # Now client.chat(tools=[...]) will work via prompt conversion
    """

    # Maximum retries for parsing tool calls
    MAX_PARSE_RETRIES: int = 2

    # Models known to NOT support native tool calling
    NON_NATIVE_TOOL_MODELS: set[str] = {
        "llama", "llama2", "llama3", "llama-2", "llama-3",
        "mistral-7b", "mixtral",
        "phi", "phi-2", "phi-3",
        "qwen", "yi", "solar",
        "deepseek-coder", "codellama",
        "falcon", "mpt", "starcoder",
    }

    def _supports_native_tools(self) -> bool:
        """Check if the current model supports native tool calling.

        Override this method for custom detection logic.
        """
        model_name = getattr(self, "model", "").lower()
        # Models known to support native tools
        native_models = {
            "gpt-4", "gpt-3.5-turbo", "gpt-4o", "gpt-4-turbo",
            "claude-3", "claude-3-5", "claude-sonnet", "claude-haiku", "claude-opus",
            "gemini", "gemini-1.5", "gemini-2.0",
            "mistral-large", "mistral-medium", "mistral-small",
            "o1", "o3",
        }
        for nm in native_models:
            if nm in model_name:
                return True

        # Check known non-native models
        for nm in self.NON_NATIVE_TOOL_MODELS:
            if nm in model_name:
                return False

        # Default: assume native support for commercial models
        return True

    async def chat_with_tool_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Chat with automatic tool calling fallback.

        If the model supports native tool calling, passes tools through.
        Otherwise, converts tools to prompt instructions and parses the
        response back into tool calls.

        Args:
            messages: List of message dicts in OpenAI format.
            tools: Optional list of tool definitions.
            **kwargs: Additional kwargs for the chat method.

        Returns:
            Response dict in OpenAI Chat Completion format.
        """
        if tools is None or not tools:
            # No tools — just call raw chat
            return await self._raw_chat(messages, **kwargs)

        if self._supports_native_tools():
            # Try native tool calling first
            try:
                return await self._raw_chat(messages, tools=tools, **kwargs)
            except Exception as e:
                error_str = str(e).lower()
                # If the error suggests tools aren't supported, fall back
                if any(kw in error_str for kw in (
                    "tool", "function", "not supported", "unsupported",
                    "does not support", "invalid request",
                )):
                    logger.info(
                        "[NonNativeToolCalling] Native tool calling failed, "
                        "falling back to prompt-based tool calling"
                    )
                else:
                    raise

        # Use prompt-based tool calling
        return await self._chat_with_prompt_tools(messages, tools, **kwargs)

    async def _chat_with_prompt_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a chat with tools converted to prompt instructions.

        This method:
        1. Injects a system prompt describing the tools
        2. Sends the modified messages to the LLM
        3. Parses the response for tool calls
        4. Retries if parsing fails

        Args:
            messages: List of message dicts.
            tools: List of tool definitions.
            **kwargs: Additional kwargs.

        Returns:
            Response dict in OpenAI format with tool_calls populated.
        """
        # Generate the tool description prompt
        tool_prompt = tools_to_prompt(tools)

        # Inject the tool prompt into the messages
        modified_messages = self._inject_tool_prompt(messages, tool_prompt)

        # Try to get a response with parseable tool calls
        last_response: Optional[dict[str, Any]] = None

        for attempt in range(self.MAX_PARSE_RETRIES + 1):
            response = await self._raw_chat(modified_messages, **kwargs)
            last_response = response

            # Try to extract tool calls from the response
            extracted = self._extract_tool_calls_from_response(response)

            if extracted is not None:
                # Successfully parsed tool calls
                return extracted

            if attempt < self.MAX_PARSE_RETRIES:
                # Add a retry prompt asking the model to use the correct format
                logger.info(
                    f"[NonNativeToolCalling] Parse attempt {attempt + 1} failed, "
                    f"retrying with format reminder"
                )
                retry_msg = {
                    "role": "user",
                    "content": (
                        "I couldn't parse your tool call. Please respond with a JSON code block "
                        "using the format specified in the system prompt. "
                        "Make sure your JSON is valid and includes the 'tool_calls' key."
                    ),
                }
                # Get the assistant's last response
                assistant_content = self._get_response_content(response)
                modified_messages = modified_messages + [
                    {"role": "assistant", "content": assistant_content},
                    retry_msg,
                ]

        # All parse attempts failed — return the raw response
        logger.warning(
            "[NonNativeToolCalling] Could not parse tool calls after "
            f"{self.MAX_PARSE_RETRIES + 1} attempts. Returning raw response."
        )
        return last_response or {}

    def _inject_tool_prompt(
        self,
        messages: list[dict[str, Any]],
        tool_prompt: str,
    ) -> list[dict[str, Any]]:
        """Inject the tool description prompt into the messages.

        Adds the tool prompt to the existing system message, or creates
        a new system message if none exists.

        Args:
            messages: The original message list.
            tool_prompt: The tool description prompt.

        Returns:
            Modified message list with tool prompt injected.
        """
        modified = list(messages)

        # Check if there's already a system message
        has_system = any(m.get("role") == "system" for m in modified)

        if has_system:
            # Append tool prompt to existing system message
            new_messages = []
            for m in modified:
                if m.get("role") == "system":
                    new_content = (m.get("content") or "") + "\n\n" + tool_prompt
                    new_messages.append({"role": "system", "content": new_content})
                else:
                    new_messages.append(m)
            return new_messages
        else:
            # Insert new system message at the beginning
            return [{"role": "system", "content": tool_prompt}] + modified

    def _extract_tool_calls_from_response(
        self,
        response: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Try to extract tool calls from a response.

        Args:
            response: The raw LLM response dict.

        Returns:
            Modified response with tool_calls, or None if parsing failed.
        """
        # Get the response content
        content = self._get_response_content(response)
        if not content:
            return None

        # Try to extract JSON
        extracted_json = extract_json_from_response(content)
        if extracted_json is None:
            return None

        # Parse tool calls from the JSON
        tool_calls = parse_tool_calls_from_json(extracted_json)
        if not tool_calls:
            return None

        # Build the response in OpenAI format
        text_content = self._remove_json_from_text(content)

        message: dict[str, Any] = {
            "role": "assistant",
            "content": text_content or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }

        return {
            "choices": [{"message": message}],
            "usage": response.get("usage", {}),
        }

    @staticmethod
    def _get_response_content(response: dict[str, Any]) -> str:
        """Extract the text content from a response dict."""
        try:
            choices = response.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "") or ""
        except (IndexError, KeyError, TypeError):
            pass
        return ""

    @staticmethod
    def _remove_json_from_text(text: str) -> str:
        """Remove JSON code blocks from text, keeping the non-JSON parts."""
        # Remove ```json blocks
        cleaned = re.sub(r"```json\s*\n?.*?\n?\s*```", "", text, flags=re.DOTALL)
        # Remove generic code blocks that look like JSON
        cleaned = re.sub(r"```\s*\n?\{.*?\}\n?\s*```", "", cleaned, flags=re.DOTALL)
        # Clean up whitespace
        cleaned = cleaned.strip()
        return cleaned


# ──────────────────────────────────────────────────────────────────────────────
# Standalone Helper Functions
# ──────────────────────────────────────────────────────────────────────────────

def convert_tools_to_prompt(tools: list[dict[str, Any]]) -> str:
    """Standalone function to convert tools to a prompt string.

    Convenience wrapper around ``tools_to_prompt()``.
    """
    return tools_to_prompt(tools)


def parse_response_tool_calls(text: str) -> list[ToolCall]:
    """Standalone function to parse tool calls from response text.

    Convenience wrapper that combines JSON extraction and tool call parsing.

    Args:
        text: The raw model response text.

    Returns:
        List of ToolCall objects (may be empty).
    """
    extracted = extract_json_from_response(text)
    if extracted is None:
        return []
    return parse_tool_calls_from_json(extracted)
