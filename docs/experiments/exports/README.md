# 压测 / 冒烟原始输出

`scripts/load_test_chat.py` 的 `--output` JSON 放这里，不要写到仓库根目录。

```bash
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --requests 10 \
  --concurrency 5 \
  --output docs/experiments/exports/load-test-smoke-10.json
```

对应实验说明在上一级 `docs/experiments/*.md`。
