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
    call_model,
    qwen_json,
    school_json,
)
from backend.llm.client import DeepSeekClient, SJTUClient
from backend.services.run_names import EntryErrorCode


class _Posted:
    def __init__(self) -> None:
        self.url = ""
        self.headers: dict[str, str] = {}
        self.stream = False

    def __call__(self, url, headers, json, timeout, stream=False):
        del json, timeout
        self.url = url
        self.headers = headers
        self.stream = stream
        return _CampusResponse()


class _CampusResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def iter_lines(self, decode_unicode=False):
        del decode_unicode
        return iter(())

    def close(self):
        return None


def test_campus_client_posts_to_sjtu_base_url(monkeypatch):
    monkeypatch.setenv("SJTU_BASE_URL", "https://campus.example/api/v1/")
    monkeypatch.setenv("SJTU_API_KEY", "campus-key")
    posted = _Posted()
    monkeypatch.setattr("backend.llm.client.requests.post", posted)
    client = SJTUClient()
    client.chat_completion("deepseek-chat", [{"role": "user", "content": "hi"}])
    assert posted.url == "https://campus.example/api/v1/chat/completions"
    assert posted.headers["Authorization"] == "Bearer campus-key"
    assert posted.stream is False
    list(client.chat_completion("deepseek-chat", [{"role": "user", "content": "hi"}], stream=True))
    assert posted.url == "https://campus.example/api/v1/chat/completions"
    assert posted.stream is True


def test_campus_client_requires_sjtu_env(monkeypatch):
    monkeypatch.setenv("SJTU_BASE_URL", "https://campus.example/api/v1")
    monkeypatch.delenv("SJTU_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SJTU_API_KEY"):
        SJTUClient()
    monkeypatch.setenv("SJTU_API_KEY", "campus-key")
    monkeypatch.delenv("SJTU_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SJTU_BASE_URL"):
        SJTUClient()


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
    assert SCHOOL_CHAT == "deepseek-chat"
    assert SCHOOL_REASONER == "deepseek-reasoner"
    assert SCHOOL_PLAN == "minimax"
    assert SCHOOL_VISION == "qwen"
    assert DEEPSEEK_FLASH == "deepseek-flash"
    assert QWEN_PLAN == "qwen3.7-flash"


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


def test_client_for_replaces_the_wrong_vendor(monkeypatch):
    from backend.llm.client import DeepSeekClient, SJTUClient, client_for, open_client

    monkeypatch.setenv("SJTU_BASE_URL", "https://campus.example/api/v1")
    monkeypatch.setenv("SJTU_API_KEY", "campus-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    campus = open_client("sjtu")
    assert isinstance(campus, SJTUClient)
    assert isinstance(client_for("sjtu", campus), SJTUClient)
    assert isinstance(client_for("deepseek", campus), DeepSeekClient)
    assert client_for("sjtu", object()) is not campus


class _Campus:
    """A campus-shaped client with no FakeLLM methods, so the real transport runs."""

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
    assert db.claim_run(run["id"], "worker-test", utcnow() + timedelta(minutes=5)) is not None
    step = db.insert_step(run["id"], "model", "model", {})
    assert step is not None
    return run, step


def test_vendor_usage_is_actual_and_a_missing_usage_is_estimated(db):
    run, step = _open(db, "usage-source")
    metered = _Campus({"prompt_tokens": 11, "completion_tokens": 7})
    school_json(
        SCHOOL_CHAT, system="count these", messages=[{"role": "user", "content": "hi"}],
        client=metered, db=db, run_id=run["id"], step_id=step["id"], purpose="probe",
    )
    bare = _Campus()
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
    assert actual["provider"] == "sjtu" and actual["model"] == SCHOOL_CHAT
    events = db.run_trace("dist-1", run["id"])["events"]
    started = next(event for event in events if event["event_type"] == "model.started")
    completed = next(event for event in events if event["event_type"] == "model.completed")
    assert started["payload_json"]["display"] == "正在思考"
    assert started["payload_json"]["purpose"] == "probe"
    assert started["payload_json"]["trace_id"] == run["trace_id"]
    assert completed["payload_json"]["usage_source"] == "actual"
    assert completed["payload_json"]["trace_id"] == run["trace_id"]
    assert completed["payload_json"]["prompt_tokens"] == 11


def test_a_run_over_its_model_budget_does_not_call(db):
    run, step = _open(db, "over-calls", max_model_calls=1)
    client = _Campus({"prompt_tokens": 4, "completion_tokens": 1})
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
    client = _Campus({"prompt_tokens": 4, "completion_tokens": 1})
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


def test_a_run_over_its_token_budget_does_not_call(db):
    run, step = _open(db, "over-tokens", max_tokens=5)
    client = _Campus({"prompt_tokens": 4, "completion_tokens": 1})
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
