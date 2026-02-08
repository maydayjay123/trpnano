"""LiteLLM provider implementation for multi-provider support."""

import asyncio
import json
import os
import re
import uuid
from typing import Any

import litellm
from litellm import acompletion

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    For models that don't support native tool calling (Groq/Llama),
    tools are injected into the system prompt and parsed from text output.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5"
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model

        # Detect OpenRouter by api_key prefix, api_base, or model prefix
        self.is_openrouter = (
            (api_key and api_key.startswith("sk-or-")) or
            (api_base and "openrouter" in api_base) or
            default_model.startswith("openrouter/")
        )

        # Track if using custom endpoint (vLLM, etc.)
        self.is_vllm = bool(api_base) and not self.is_openrouter

        # Detect Groq — use text-based tool calling instead of native
        self.is_groq = "groq" in default_model.lower()

        # Configure LiteLLM based on provider
        if api_key:
            if self.is_openrouter:
                os.environ["OPENROUTER_API_KEY"] = api_key
            elif self.is_vllm:
                os.environ["OPENAI_API_KEY"] = api_key
            elif "deepseek" in default_model:
                os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
            elif "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)
            elif "zhipu" in default_model or "glm" in default_model or "zai" in default_model:
                os.environ.setdefault("ZHIPUAI_API_KEY", api_key)
            elif self.is_groq:
                os.environ.setdefault("GROQ_API_KEY", api_key)

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True

    def _build_tool_system_prompt(self, tools: list[dict[str, Any]]) -> str:
        """Build a system prompt snippet describing tools for text-based calling."""
        lines = [
            "\n\n# Available Tools",
            "To call a tool, output EXACTLY this format on its own line:",
            "TOOL_CALL: {\"name\": \"tool_name\", \"arguments\": {\"key\": \"value\"}}",
            "You MUST use this exact format. Do NOT use any other format.",
            "After outputting a TOOL_CALL, STOP and wait for the result.",
            "",
            "Available tools:",
        ]
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "")
            desc = func.get("description", "")
            params = func.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])

            param_parts = []
            for pname, pinfo in props.items():
                req = " (required)" if pname in required else ""
                param_parts.append(f"    - {pname}: {pinfo.get('description', '')}{req}")

            lines.append(f"- {name}: {desc}")
            if param_parts:
                lines.extend(param_parts)

        lines.append("")
        return "\n".join(lines)

    def _inject_tools_into_messages(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """For Groq: inject tool descriptions into the system message."""
        tool_prompt = self._build_tool_system_prompt(tools)
        messages = [m.copy() for m in messages]

        # Append to existing system message or add new one
        for m in messages:
            if m.get("role") == "system":
                m["content"] = (m.get("content") or "") + tool_prompt
                return messages

        # No system message found, prepend one
        messages.insert(0, {"role": "system", "content": tool_prompt})
        return messages

    def _parse_text_tool_calls(self, content: str) -> list[ToolCallRequest]:
        """Parse TOOL_CALL: {...} from model text output."""
        calls = []
        for match in re.finditer(r'TOOL_CALL:\s*(\{.*?\})\s*$', content, re.MULTILINE):
            try:
                data = json.loads(match.group(1))
                name = data.get("name", "")
                args = data.get("arguments", {})
                if name:
                    calls.append(ToolCallRequest(
                        id=f"text_{uuid.uuid4().hex[:8]}",
                        name=name,
                        arguments=args,
                    ))
            except json.JSONDecodeError:
                continue
        return calls

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        model = model or self.default_model

        # For OpenRouter, prefix model name if not already prefixed
        if self.is_openrouter and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"

        # For Zhipu/Z.ai, ensure prefix is present
        if ("glm" in model.lower() or "zhipu" in model.lower()) and not (
            model.startswith("zhipu/") or
            model.startswith("zai/") or
            model.startswith("openrouter/")
        ):
            model = f"zai/{model}"

        # For vLLM, use hosted_vllm/ prefix per LiteLLM docs
        if self.is_vllm:
            model = f"hosted_vllm/{model}"

        # For Gemini, ensure gemini/ prefix if not already present
        if "gemini" in model.lower() and not model.startswith("gemini/") and not model.startswith("openrouter/"):
            model = f"gemini/{model}"

        # For Groq: don't pass tools natively, inject into system prompt instead
        use_text_tools = self.is_groq and tools

        if use_text_tools:
            messages = self._inject_tools_into_messages(messages, tools)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Pass api_base directly for custom endpoints (vLLM, etc.)
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # Only pass native tools for non-Groq models
        if tools and not use_text_tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await acompletion(**kwargs)
                result = self._parse_response(response)

                # For Groq: parse tool calls from text output
                if use_text_tools and result.content:
                    text_calls = self._parse_text_tool_calls(result.content)
                    if text_calls:
                        # Strip the TOOL_CALL line from content
                        clean = re.sub(r'TOOL_CALL:\s*\{.*?\}\s*$', '', result.content, flags=re.MULTILINE).strip()
                        return LLMResponse(
                            content=clean or None,
                            tool_calls=text_calls,
                            finish_reason="tool_calls",
                            usage=result.usage,
                        )

                return result
            except Exception as e:
                err_str = str(e)

                # Auto-retry on rate limit with parsed delay
                if "rate_limit" in err_str.lower() or "429" in err_str:
                    wait = self._parse_retry_delay(err_str)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(wait)
                        continue

                return LLMResponse(
                    content=f"Error calling LLM: {err_str}",
                    finish_reason="error",
                )

        return LLMResponse(content="Rate limited after retries. Try again shortly.", finish_reason="error")

    @staticmethod
    def _parse_retry_delay(err_str: str) -> float:
        """Extract retry delay from rate limit error, default 30s."""
        match = re.search(r'try again in (\d+\.?\d*)s', err_str, re.IGNORECASE)
        if match:
            return min(float(match.group(1)) + 1, 60)
        return 30

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}

                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
