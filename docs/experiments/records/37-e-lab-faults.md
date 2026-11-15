# 记录 37：实验 E — 实验室故障路径复跑

记录 36 的盘点结论：worker 中断、lease、outbox、`response_lost`、采购确认幂等都已经有单测，不必再做 live 杀进程。这一步把那一批连同 429 进队列一起再跑一遍，确认产品改动没有把它们弄漂。

## 范围

```
./dev test tests/test_llm.py tests/test_provider_errors.py \
  tests/test_execution_contract.py tests/test_submit.py \
  tests/test_unclaimed_dispatch.py tests/test_execute_run_db_retry.py \
  tests/test_run_api.py
```

（第一次 124 passed / 1 failed，失败的是本轮新加的超时进队列用例，断言窗口太窄，见记录 38。修完后再跑该条 1 passed。ruff 通过。）

## 结果

已有实验室故障路径全部通过：

- worker 丢失不重发邮件：`test_a_lost_worker_resumes_without_sending_twice`
- lease 过期清扫、三次后失败
- outbox 超次死信、补投有界
- 副作用超时 / 丢响应 / 仍在 running / 过期 → `response_lost`，不自动再调
- 采购 confirm token 烧掉后再建单；双击竞态仍是一张单
- 429 按 Retry-After 再入队，不花业务 attempt

没有补 live worker-kill。副作用幂等靠 claim + token，单测已经把「第二次不会再执行」钉死。
