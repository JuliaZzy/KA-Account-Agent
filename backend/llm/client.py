"""One client per model vendor.

Each chat method returns the vendor's own response and stores that call's token
counts on `last_usage` (`prompt_tokens`, `completion_tokens`). A stream stores
them as the chunks are read. OCR is not a chat client.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any

import requests
from openai import AsyncOpenAI, OpenAI

from ..config import LLMSettings, vendor_endpoint
from .errors import ProviderError, provider_error_from_http, translate_provider_error


def _usage_count(usage: Any, *names: str) -> int:
    if usage is None:
        return 0
    for name in names:
        raw = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0
    return 0


def _remember_usage(client: Any, usage: Any) -> None:
    """Chat completions say prompt/completion. The Responses API says input/output."""
    client.last_usage = {
        "prompt_tokens": _usage_count(usage, "prompt_tokens", "input_tokens"),
        "completion_tokens": _usage_count(usage, "completion_tokens", "output_tokens"),
    }


def _remembering_stream(client: Any, stream: Iterator[Any]) -> Iterator[Any]:
    for chunk in stream:
        usage = chunk.get("usage") if isinstance(chunk, dict) else getattr(chunk, "usage", None)
        if usage is not None:
            _remember_usage(client, usage)
        yield chunk


async def _remembering_async_stream(client: Any, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
    async for chunk in stream:
        usage = chunk.get("usage") if isinstance(chunk, dict) else getattr(chunk, "usage", None)
        if usage is not None:
            _remember_usage(client, usage)
        yield chunk


def _finish_response(client: Any, resp: Any, *, stream: bool) -> Any:
    if stream:
        _remember_usage(client, None)
        return _remembering_stream(client, resp)
    _remember_usage(client, getattr(resp, "usage", None))
    return resp


def _finish_async_response(client: Any, resp: Any, *, stream: bool) -> Any:
    if stream:
        _remember_usage(client, None)
        return _remembering_async_stream(client, resp)
    _remember_usage(client, getattr(resp, "usage", None))
    return resp


class GLMClient:
    """One Responses API call. The caller supplies the model and this turn's input."""

    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or LLMSettings()
        endpoint = vendor_endpoint("glm")
        self._client = OpenAI(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            timeout=self.settings.timeout,
            max_retries=0,
        )

    def response(
        self,
        *,
        model: str,
        input: str | list[dict[str, Any]],
        previous_response_id: str | None = None,
        store: bool | None = None,
        stream: bool = False,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model, "input": input}
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        if store is not None:
            kwargs["store"] = store
        if stream:
            kwargs["stream"] = True
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if reasoning_effort:
            kwargs["extra_body"] = {"reasoning": {"effort": reasoning_effort}}
        return _finish_response(self, self._client.responses.create(**kwargs), stream=stream)


# Caller shapes. This class does not run tools or keep the conversation.
#
# Multi-turn. `store=True` on the first call, then pass its id forward.
# The id lasts 7 days. Omit `previous_response_id` on the first turn.
#   first = client.response(model="glm-5.3", input="我的名字是李雷。", store=True)
#   second = client.response(
#       model="glm-5.3", input="我叫什么名字？", previous_response_id=first.id, store=True,
#   )
#
# Stream. `stream=True` returns an iterator. Text arrives as
# `response.output_text.delta`. The id for the next turn is on `response.completed`.
#
# Reasoning. Pass `reasoning_effort` only for the turns that should think.
#   client.response(model="glm-5.3", input="...", reasoning_effort="max")
#
# Tools. The caller runs the function, then sends the output as the next input.
#   first = client.response(
#       model="glm-5.3", input="北京今天天气怎么样？", tools=tools, tool_choice="auto", store=True,
#   )
#   calls = [item for item in first.output if getattr(item, "type", None) == "function_call"]
#   second = client.response(
#       model="glm-5.3",
#       previous_response_id=first.id,
#       store=True,
#       input=[{"type": "function_call_output", "call_id": calls[0].call_id, "output": result}],
#   )


class QwenClient:
    """DashScope. `chat_completion` is Chat Completions; `response` is one Responses call."""

    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or LLMSettings()
        endpoint = vendor_endpoint("qwen")
        self._client = AsyncOpenAI(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            timeout=self.settings.timeout,
            max_retries=0,
        )

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        # extra_body={"enable_thinking": True}
        extra_body: dict[str, Any] | None = None,
        stream: bool = False,
        # stream_options={"include_usage": True}，流式最后一个包带上输入输出 token
        stream_options: dict[str, Any] | None = None,
        # response_format={"type": "json_object"}
        # response_format={"type": "json_schema", "json_schema": schema}
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        parallel_tool_calls: bool = True,  # 是否并行调用工具
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if stream:
            kwargs["stream"] = True
        if stream_options:
            kwargs["stream_options"] = stream_options
        if response_format:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = parallel_tool_calls
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        resp = await self._client.chat.completions.create(**kwargs)
        return _finish_async_response(self, resp, stream=stream)

    async def response(
        self,
        *,
        model: str,
        input: str | list[dict[str, Any]],
        # extra_body={"enable_thinking": True}
        extra_body: dict[str, Any] | None = None,
        stream: bool = False,
        # 内置工具：web_search、web_extractor、code_interpreter、web_search_image、image_search、file_search、mcp
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        previous_response_id: str | None = None,
        store: bool | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model, "input": input}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if stream:
            kwargs["stream"] = True
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        if store is not None:
            kwargs["store"] = store
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if reasoning_effort:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        resp = await self._client.responses.create(**kwargs)
        return _finish_async_response(self, resp, stream=stream)

# 定义异步任务列表
"""
async def task(question):
    print(f"发送问题: {question}")
    response = await client.chat.completions.create(
        messages=[
            {"role": "system", "content": "You are a helpful assistant." },
            {"role": "user", "content": question}
        ],
        model="qwen-plus",  # 模型列表：https://help.aliyun.com/zh/model-studio/getting-started/models
    )
    print(f"模型回复: {response.choices[0].message.content}")

# 主异步函数
async def main():
    questions = ["你是谁？", "你会什么？", "天气怎么样？"]
    tasks = [task(q) for q in questions]
    await asyncio.gather(*tasks)

if __name__ == '__main__':
    # 设置事件循环策略
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # 运行主协程
    asyncio.run(main(), debug=False)
"""

# 流式输出 + 输入输出token量
"""
import os
from openai import OpenAI

# 1. 准备工作：初始化客户端
client = OpenAI(
    # 建议通过环境变量配置API Key，避免硬编码。
    api_key=os.environ["DASHSCOPE_API_KEY"],
    # API Key与地域强绑定，请确保base_url与API Key的地域一致。
    base_url="https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)

# 2. 发起流式请求
completion = client.chat.completions.create(
    model="qwen-plus",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "请介绍一下自己"}
    ],
    stream=True,
    stream_options={"include_usage": True}
)

# 3. 处理流式响应
# 用列表暂存响应片段，最后 join 比逐次 += 字符串更高效
content_parts = []
print("AI: ", end="", flush=True)

for chunk in completion:
    if chunk.choices:
        content = chunk.choices[0].delta.content or ""
        print(content, end="", flush=True)
        content_parts.append(content)
    elif chunk.usage:
        print("\n--- 请求用量 ---")
        print(f"输入 Tokens: {chunk.usage.prompt_tokens}")
        print(f"输出 Tokens: {chunk.usage.completion_tokens}")
        print(f"总计 Tokens: {chunk.usage.total_tokens}")

full_response = "".join(content_parts)
# print(f"\n--- 完整回复 ---\n{full_response}")


====================思考过程====================

好的，用户问“你是谁”，我需要给出一个准确且友好的回答。首先，我要确认自己的身份，即千问，由阿里巴巴集团旗下的通义实验室研发。接下来，应该说明我的主要功能，比如回答问题、创作文字、逻辑推理等。同时，要保持语气亲切，避免过于技术化，让用户感觉轻松。还要注意不要使用复杂术语，确保回答简洁明了。另外，可能需要加入一些互动元素，邀请用户提问，促进进一步交流。最后，检查是否有遗漏的重要信息，比如我的中文名称“千问”和英文名称“Qwen”，以及所属公司和实验室。确保回答全面且符合用户期望。
====================完整回复====================

你好！我是千问，是阿里巴巴集团旗下的通义实验室自主研发的超大规模语言模型。我可以回答问题、创作文字、进行逻辑推理、编程等，旨在为用户提供高质量的信息和服务。你可以叫我Qwen，或者直接叫我千问。有什么我可以帮你的吗？
"""



class DeepSeekClient:
    """Official DeepSeek. `chat_completion` is Chat Completions; `response` is one Responses call.

    The Responses API is stateless: `previous_response_id` and `store` are rejected
    by omission. Send the whole history in `input`.
    """

    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or LLMSettings()
        endpoint = vendor_endpoint("deepseek")
        self._client = OpenAI(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            timeout=self.settings.timeout,
            max_retries=0,
        )

    def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        # extra_body={"thinking": {"type": "enabled"}}  或 {"type": "disabled"}
        extra_body: dict[str, Any] | None = None,
        stream: bool = False,
        # stream_options={"include_usage": True}，必须和 stream=True 一起用
        stream_options: dict[str, Any] | None = None,
        # response_format={"type": "json_object"}，只有 text / json_object
        response_format: dict[str, Any] | None = None,
        # none 关闭思考；low / high / max 开启。默认 high
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if stream:
            kwargs["stream"] = True
        if stream and stream_options:
            kwargs["stream_options"] = stream_options
        if response_format:
            kwargs["response_format"] = response_format
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        return _finish_response(self, self._client.chat.completions.create(**kwargs), stream=stream)

    def response(
        self,
        *,
        model: str,
        input: str | list[dict[str, Any]],
        instructions: str | None = None,
        stream: bool = False,
        # none 关闭思考；low / high / max 开启。默认 high
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        # tools：function；custom 仅 name="apply_patch"；web_search 仅 deepseek-v4-pro。
        # file_search、code_interpreter、mcp 会被忽略。
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        # text={"format": {"type": "json_object"}}
        # text={"format": {"type": "json_schema", "name": "...", "schema": {...}}}
        text: dict[str, Any] | None = None,
        top_logprobs: int | None = None,
        user: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model, "input": input}
        if instructions:
            kwargs["instructions"] = instructions
        if stream:
            kwargs["stream"] = True
        if reasoning_effort:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if text:
            kwargs["text"] = text
        if top_logprobs is not None:
            kwargs["top_logprobs"] = top_logprobs
        if user:
            kwargs["user"] = user
        return _finish_response(self, self._client.responses.create(**kwargs), stream=stream)

"""
for attempt in range(1, 6):  # 最多重试 5 次，避免无限循环
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=(30, 600))
        except requests.RequestException as e:
            print(f"请求异常(第{attempt}次): {e}")
            time.sleep(2)
            continue

        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()

        # 非 200：打印出来你就知道是不是 401/403/429 等
        print(f"HTTP {resp.status_code} (第{attempt}次): {resp.text[:500]}")
        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            break  # 4xx(非429)通常重试没意义
        time.sleep(2)

    raise RuntimeError("请求失败，见上方 HTTP 输出")

if __name__ == "__main__":
    question = "请介绍下你自己？"
    print(query(question))
"""

def _campus_endpoint() -> tuple[str, dict[str, str]]:
    endpoint = vendor_endpoint("sjtu")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {endpoint.api_key}"}
    return endpoint.base_url.rstrip("/") + "/chat/completions", headers


def _campus_stream(client: Any, body: dict[str, Any], *, model: str) -> Iterator[Any]:
    """Campus SSE. Each yielded object is one chat-completion chunk."""
    _remember_usage(client, None)
    try:
        resp = requests.post(
            client.url,
            headers=client.headers,
            json=body,
            timeout=(30, 600),
            stream=True,
        )
    except requests.RequestException as exc:
        raise translate_provider_error(exc, provider="sjtu", model=model) from exc
    if resp.status_code != 200:
        try:
            raise provider_error_from_http(resp, provider="sjtu", model=model)
        finally:
            resp.close()
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not str(line).startswith("data:"):
                continue
            data = str(line)[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict) and chunk.get("usage") is not None:
                _remember_usage(client, chunk["usage"])
            yield chunk
    except requests.RequestException as exc:
        raise translate_provider_error(exc, provider="sjtu", model=model) from exc
    finally:
        resp.close()

class SJTUClient:
    """SJTU gateway, called with requests as in the campus sample."""

    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or LLMSettings()
        self.url, self.headers = _campus_endpoint()

    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        # 深度推理是另一个调用名，例如 deepseek-reasoner。思考文字在返回的 reasoning_content 里。
        # 看图把 content 写成列表，不要另开接口：
        # {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        # {"type": "text", "text": "..."}
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        if stream:
            body["stream_options"] = {"include_usage": True}
            return _campus_stream(self, body, model=model)
        try:
            resp = requests.post(self.url, headers=self.headers, json=body, timeout=(30, 600))
        except requests.RequestException as exc:
            raise translate_provider_error(exc, provider="sjtu", model=model) from exc
        if resp.status_code != 200:
            raise provider_error_from_http(resp, provider="sjtu", model=model)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                "sjtu", model, "bad_response", status_code=resp.status_code, retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                "sjtu", model, "bad_response", status_code=resp.status_code, retryable=False,
            )
        _remember_usage(self, data.get("usage"))
        return data


class GLMOCRClient:
    """Zhipu layout parsing. The body is the model name and one PDF data URL."""

    def __init__(self, *, api_key: str, url: str, timeout: float = 180):
        self.api_key = api_key
        self.url = url
        self.timeout = timeout

    def parse(self, data: bytes) -> dict[str, Any]:
        body = json.dumps({
            "model": "glm-ocr",
            "file": "data:application/pdf;base64," + base64.b64encode(data).decode(),
        }).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")[:300]
            raise ValueError(f"GLM-OCR HTTP {err.code}: {detail}") from err
        except urllib.error.URLError as err:
            raise ValueError(f"GLM-OCR request failed: {err.reason}") from err
        if not isinstance(payload, dict):
            raise ValueError("GLM-OCR returned a non-object response.")
        if payload.get("error"):
            raise ValueError(f"GLM-OCR error: {payload['error']}")
        return payload


def campus_client() -> SJTUClient:
    return SJTUClient()


def deepseek_client() -> DeepSeekClient:
    return DeepSeekClient()


def glm_client() -> GLMClient:
    return GLMClient()


def qwen_client() -> QwenClient:
    return QwenClient()


VENDORS = (SJTUClient, DeepSeekClient, GLMClient, QwenClient)


def open_client(brand: str) -> Any:
    """The vendor client for this brand. Keys come from the environment."""
    if brand == "fake":
        raise ValueError("fake brand requires an injected test double")
    if brand == "sjtu":
        return campus_client()
    if brand == "deepseek":
        return deepseek_client()
    if brand == "glm":
        return glm_client()
    if brand == "qwen":
        return qwen_client()
    raise ValueError(f"unknown model brand {brand}")


def client_for(brand: str, injected: Any | None) -> Any:
    """The client for this call. None opens the vendor named by `brand`.

    A test double is used as given. A vendor object of another brand is replaced.
    """
    if brand == "fake":
        if injected is None:
            raise ValueError("fake brand requires an injected test double")
        return injected
    wanted = {
        "sjtu": SJTUClient,
        "deepseek": DeepSeekClient,
        "glm": GLMClient,
        "qwen": QwenClient,
    }.get(brand)
    if wanted is None:
        raise ValueError(f"unknown model brand {brand}")
    if injected is None:
        return open_client(brand)
    if isinstance(injected, wanted):
        return injected
    if isinstance(injected, VENDORS):
        return open_client(brand)
    return injected