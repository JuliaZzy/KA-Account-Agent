"""The desk no longer sends the dropdown id. Each step names its own model."""

from datetime import timedelta

import pytest

from backend.db import utcnow
from backend.llm.calls import (
    DEEPSEEK_FLASH,
    QWEN_PLAN,
    SCHOOL_CHAT,
    SCHOOL_PLAN,
    SCHOOL_REASONER,
    SCHOOL_VISION,
    ModelBudgetExceeded,
    ModelCapacityExceeded,
    call_model,
    qwen_json,
    school_json,
)
from backend.llm.client import DeepSeekClient, _remember_usage
from backend.services.run_names import EntryErrorCode


def test_usage_keeps_deepseek_and_qwen_cache_fields():
    from types import SimpleNamespace

    deepseek = SimpleNamespace()
    _remember_usage(deepseek, {
        "prompt_tokens": 100, "completion_tokens": 8,
        "prompt_cache_hit_tokens": 72, "prompt_cache_miss_tokens": 28,
    })
    assert deepseek.last_usage["cache_hit_tokens"] == 72
    assert deepseek.last_usage["cache_miss_tokens"] == 28
    assert deepseek.last_usage["prompt_cache_hit_tokens"] == 72

    qwen = SimpleNamespace()
    _remember_usage(qwen, {
        "input_tokens": 100, "output_tokens": 8,
        "prompt_tokens_details": {"cached_tokens": 64},
    })
    assert qwen.last_usage["cache_hit_tokens"] == 64
    assert qwen.last_usage["cache_miss_tokens"] == 36
    assert qwen.last_usage["prompt_tokens_details.cached_tokens"] == 64


def test_vendor_posts_dotted_tool_names_without_dots(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    posted: dict = {}

    def chat_completion(**kwargs):
        posted["tools"] = kwargs.get("tools")
        posted["messages"] = kwargs.get("messages")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id="c1", function=SimpleNamespace(name="po_draft", arguments="{}"),
            )],
        ))])

    client = DeepSeekClient()
    client.chat_completion = chat_completion
    called = call_model(
        "deepseek-flash", brand="deepseek", kind="chat_completion", expect="tools",
        client=client,
        messages=[{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "prev", "type": "function",
                "function": {"name": "po.draft", "arguments": "{}"},
            }],
        }],
        tools=[{"type": "function", "function": {
            "name": "po.draft", "description": "draft", "parameters": {},
        }}],
    )
    assert posted["tools"][0]["function"]["name"] == "po_draft"
    history = next(m for m in posted["messages"] if m.get("role") == "assistant")
    assert history["tool_calls"][0]["function"]["name"] == "po_draft"
    assert called.body.tool_calls[0].name == "po.draft"


def test_named_models():
    assert SCHOOL_CHAT == "qwen3.7-flash"
    assert SCHOOL_REASONER == "deepseek-flash"
    assert SCHOOL_PLAN == "qwen3.7-flash"
    assert SCHOOL_VISION == "deepseek-flash"
    assert DEEPSEEK_FLASH == "deepseek-flash"
    assert QWEN_PLAN == "qwen3.7-flash"


def test_assemble_stream_drains_async_generator():
    from types import SimpleNamespace

    from backend.llm.calls import _assemble_stream

    async def _chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content="你", reasoning_content=None,
        ))])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content="好", reasoning_content=None,
        ))])

    seen: list[str] = []

    def on_text(text: str, *, reasoning: str = "") -> None:
        del reasoning
        seen.append(text)

    assembled = _assemble_stream(_chunks(), on_text)
    assert assembled.choices[0].message.content == "你好"
    assert seen == ["你", "你好"]


def test_assemble_stream_keeps_sync_iterator():
    from types import SimpleNamespace

    from backend.llm.calls import _assemble_stream

    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content="hi", reasoning_content=None,
        ))]),
    ]
    assembled = _assemble_stream(chunks, lambda *_a, **_k: None)
    assert assembled.choices[0].message.content == "hi"


def test_qwen_stream_reports_text_while_the_stream_is_still_open():
    """on_text must fire per chunk, not after the whole stream is buffered."""
    from types import SimpleNamespace

    from backend.llm.calls import _call_qwen

    timeline: list[str] = []

    def _chunk(text: str):
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=text, reasoning_content=None,
        ))])

    async def _stream():
        timeline.append("chunk1 yielded")
        yield _chunk("你")
        # Resumed only after the consumer finished handling chunk 1.
        timeline.append("chunk2 yielded")
        yield _chunk("好")
        timeline.append("stream closed")

    class _Client:
        async def chat_completion(self, **_kwargs):
            return _stream()

    def on_text(text: str, *, reasoning: str = "") -> None:
        del reasoning
        timeline.append(f"on_text {text}")

    called = _call_qwen(
        _Client(), kind="chat_completion", model="qwen3.7-flash", system="s", messages=[],
        stream=True, temperature=None, tools=None, tool_choice=None, reasoning_effort="none",
        on_text=on_text,
    )

    assert timeline == [
        "chunk1 yielded", "on_text 你", "chunk2 yielded", "on_text 你好", "stream closed",
    ]
    assert called.response.choices[0].message.content == "你好"


def _slow_qwen_client(first_delay: float, gap: float, chunks: int = 3):
    import asyncio
    from types import SimpleNamespace

    def _chunk(text: str):
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=text, reasoning_content=None,
        ))])

    async def _stream():
        await asyncio.sleep(first_delay)
        for index in range(chunks):
            yield _chunk(str(index))
            await asyncio.sleep(gap)

    class _Client:
        async def chat_completion(self, **_kwargs):
            return _stream()

    return _Client()


def _qwen_call(client):
    from backend.llm.calls import _call_qwen

    return _call_qwen(
        client, kind="chat_completion", model="qwen3.7-flash", system="s", messages=[],
        stream=True, temperature=None, tools=None, tool_choice=None, reasoning_effort="none",
        on_text=lambda *_a, **_k: None,
    )


def test_qwen_stream_that_goes_silent_times_out_as_a_retryable_provider_error(monkeypatch):
    from backend.llm.errors import translate_provider_error

    monkeypatch.setenv("KA_ACCOUNT_QWEN_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("KA_ACCOUNT_QWEN_CHUNK_IDLE_SECONDS", "0.05")
    with pytest.raises(TimeoutError) as caught:
        _qwen_call(_slow_qwen_client(first_delay=0, gap=1.0))
    error = translate_provider_error(caught.value, provider="qwen", model="qwen3.7-flash")
    assert (error.kind, error.code, error.retryable) == ("timeout", "PROVIDER_TIMEOUT", True)


def test_qwen_call_over_the_total_budget_times_out(monkeypatch):
    monkeypatch.setenv("KA_ACCOUNT_QWEN_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("KA_ACCOUNT_QWEN_CHUNK_IDLE_SECONDS", "5")
    # Chunks keep arriving inside the idle limit, so only the total ceiling can stop it.
    with pytest.raises(TimeoutError):
        _qwen_call(_slow_qwen_client(first_delay=0, gap=0.1, chunks=50))


def test_qwen_call_inside_both_limits_completes(monkeypatch):
    monkeypatch.setenv("KA_ACCOUNT_QWEN_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("KA_ACCOUNT_QWEN_CHUNK_IDLE_SECONDS", "1")
    called = _qwen_call(_slow_qwen_client(first_delay=0.05, gap=0.01))
    assert called.response.choices[0].message.content == "012"


def test_qwen_json_sends_schema_and_turns_thinking_off(monkeypatch):
    from types import SimpleNamespace

    from backend.llm.client import QwenClient

    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.example/v1")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    posted: dict = {}

    def chat_completion(**kwargs):
        posted.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"tasks":[]}'))])

    client = QwenClient()
    client.chat_completion = chat_completion
    body = qwen_json(
        system="plan", messages=[{"role": "user", "content": "hi"}],
        schema={"name": "orchestrator_plan", "strict": True, "schema": {"type": "object"}},
        client=client,
    )
    assert body == {"tasks": []}
    assert posted["model"] == "qwen3.7-flash"
    assert posted["extra_body"] == {"enable_thinking": False}
    assert posted["response_format"]["type"] == "json_schema"
    assert posted["response_format"]["json_schema"]["name"] == "orchestrator_plan"


def test_model_capacity_is_an_explicit_retryable_error(monkeypatch):
    from backend.llm import calls as model_calls

    monkeypatch.setattr(model_calls, "acquire_model", lambda _brand: None)
    with pytest.raises(ModelCapacityExceeded) as raised:
        qwen_json(
            system="plan", messages=[{"role": "user", "content": "hi"}],
            schema={"name": "plan", "strict": True, "schema": {"type": "object"}},
            client=_VendorStub(),
        )
    assert raised.value.brand == "qwen"
    assert raised.value.model == QWEN_PLAN
    assert raised.value.code == "MODEL_CAPACITY_EXCEEDED"


def test_client_for_replaces_the_wrong_vendor(monkeypatch):
    from backend.llm.client import DeepSeekClient, QwenClient, client_for, open_client

    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.example/v1")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    qwen = open_client("qwen")
    assert isinstance(qwen, QwenClient)
    assert isinstance(client_for("qwen", qwen), QwenClient)
    assert isinstance(client_for("deepseek", qwen), DeepSeekClient)
    assert client_for("qwen", object()) is not qwen


class _VendorStub:
    """A chat-completion shaped client with no FakeLLM methods, so the real transport runs."""

    def __init__(self, usage: dict[str, int] | None = None) -> None:
        self.usage = usage
        self.calls = 0

    def chat_completion(self, model, messages, stream=False, max_tokens=None, temperature=None, **_kwargs):
        del model, messages, stream, max_tokens, temperature
        self.calls += 1
        if self.usage is not None:
            self.last_usage = self.usage
        return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}


def _open(db, key: str, **caps):
    run, _created = db.create_agent_run("dist-1", "u-a", "chat", key, {"message": "hi"}, **caps)
    assert db.claim_chat_turn(run["id"], "worker-test", utcnow() + timedelta(minutes=5)) is not None
    step = db.insert_step(run["id"], "model", "model", {})
    assert step is not None
    return run, step


def test_vendor_usage_is_actual_and_a_missing_usage_is_estimated(db):
    run, step = _open(db, "usage-source")
    metered = _VendorStub({
        "prompt_tokens": 11, "completion_tokens": 7,
        "cache_hit_tokens": 8, "cache_miss_tokens": 3, "cache_creation_tokens": 0,
    })
    school_json(
        SCHOOL_CHAT, system="count these", messages=[{"role": "user", "content": "hi"}],
        client=metered, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    bare = _VendorStub()
    school_json(
        SCHOOL_CHAT, system="汉字汉字汉字汉字", messages=[{"role": "user", "content": "abcd"}],
        client=bare, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    calls = db.run_trace("dist-1", run["id"])["model_calls"]
    assert {
        (row["usage_source"], row["prompt_tokens"], row["completion_tokens"]) for row in calls
    } == {("actual", 11, 7), ("estimated", 9, 3)}
    usage = db.list_usage_records(run["id"])
    assert {row["usage_source"] for row in usage} == {"actual", "estimated"}
    actual = next(row for row in usage if row["usage_source"] == "actual")
    assert actual["provider"] == "qwen" and actual["model"] == SCHOOL_CHAT
    assert actual["cache_hit_tokens"] == 8
    assert actual["cache_miss_tokens"] == 3
    assert actual["cache_creation_tokens"] == 0
    events = db.run_trace("dist-1", run["id"])["events"]
    started = next(event for event in events if event["event_type"] == "model.started")
    completed = next(event for event in events if event["event_type"] == "model.completed")
    assert started["payload_json"]["display"] == "正在思考..."
    assert started["payload_json"]["purpose"] == "probe"
    assert started["payload_json"]["trace_id"] == run["trace_id"]
    assert completed["payload_json"]["usage_source"] == "actual"
    assert completed["payload_json"]["trace_id"] == run["trace_id"]
    assert completed["payload_json"]["prompt_tokens"] == 11


def test_a_run_over_its_model_budget_does_not_call(db):
    run, step = _open(db, "over-calls", max_model_calls=1)
    client = _VendorStub({"prompt_tokens": 4, "completion_tokens": 1})
    school_json(
        SCHOOL_CHAT, system="once", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    with pytest.raises(ModelBudgetExceeded) as raised:
        school_json(
            SCHOOL_CHAT, system="twice", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert str(raised.value) == EntryErrorCode.COST_BUDGET_EXCEEDED
    assert client.calls == 1
    assert len(db.run_trace("dist-1", run["id"])["model_calls"]) == 1
    failed = [
        event for event in db.run_trace("dist-1", run["id"])["events"]
        if event["event_type"] == "model.failed"
    ]
    assert failed[-1]["payload_json"]["error"] == EntryErrorCode.COST_BUDGET_EXCEEDED
    assert failed[-1]["payload_json"]["trace_id"] == run["trace_id"]


def test_an_org_over_its_daily_tokens_does_not_call(db, monkeypatch):
    run, step = _open(db, "org-daily")
    client = _VendorStub({"prompt_tokens": 4, "completion_tokens": 1})
    school_json(
        SCHOOL_CHAT, system="once", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    monkeypatch.setenv("KA_ACCOUNT_ORG_DAILY_TOKENS", "1")
    with pytest.raises(ModelBudgetExceeded):
        school_json(
            SCHOOL_CHAT, system="twice", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert client.calls == 1


def test_an_org_accumulates_cost_cents_and_stops_at_daily_cost_cap(db, monkeypatch):
    run, step = _open(db, "org-daily-cost")
    # 25,000 uncached prompt tokens on qwen3.7-flash costs 0.5 cents per call; two calls round to 1 cent.
    client = _VendorStub({"prompt_tokens": 25_000, "completion_tokens": 0})
    school_json(
        SCHOOL_CHAT, system="first", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    school_json(
        SCHOOL_CHAT, system="second", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    assert db.org_daily_usage("dist-1")["estimated_cost_cents"] == 1
    monkeypatch.setenv("KA_ACCOUNT_ORG_DAILY_COST_CENTS", "1")
    with pytest.raises(ModelBudgetExceeded):
        school_json(
            SCHOOL_CHAT, system="third", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert client.calls == 2


def test_a_run_over_its_token_budget_does_not_call(db):
    run, step = _open(db, "over-tokens", max_tokens=5)
    client = _VendorStub({"prompt_tokens": 4, "completion_tokens": 1})
    school_json(
        SCHOOL_CHAT, system="once", messages=[{"role": "user", "content": "hi"}],
        client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    with pytest.raises(ModelBudgetExceeded):
        school_json(
            SCHOOL_CHAT, system="twice", messages=[{"role": "user", "content": "hi"}],
            client=client, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
        )
    assert client.calls == 1
