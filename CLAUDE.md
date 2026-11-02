# CLAUDE.md

> 先读 [README.md](README.md)：架构、目录结构、数据层设计、能力现状表都在那里，本文件不重复。
> 每个模块顶部的 docstring 说明了它自己的职责和设计取舍——改一个模块前先读它的 docstring。
>
> 本文件只放**读代码推断不出来、但会改变你怎么动手**的东西。

## 命令

不要在宿主机调 `uvicorn` / `pytest` / `npm`，也不要创建 `.venv` 来跑这个项目。统一走 `./dev`
（Windows 是 `dev.bat`）：它把每条命令送到 Docker Compose。桌面是 `frontend/` 里的 Vite 应用，
`./dev` 会拉起 Postgres + Redis + Milvus + API + Celery worker + 前端：

```
./dev            桌面 UI（Vite + React） http://localhost:5173
./dev api        HTTP API（FastAPI）    http://127.0.0.1:8000/docs
./dev db         只起 Postgres，等到可连
./dev catalog    把 data/off/ 的 Open Food Facts 快照导入产品目录（几十万行；快照用 scripts/fetch_off_products.py 拉）
./dev test       全部测试，不联网（Python）。前端单测：./dev test-web
./dev lint       ruff
./dev py ...     容器里的 python
./dev build      依赖变了才需要
./dev kill       停掉栈（保留 Postgres 数据卷）
```

本机只需要 Docker Desktop。新终端把仓库根目录放进 PATH 之后可以直接敲 `dev`。

## 数据库

- **PostgreSQL 是唯一的引擎，没有 SQLite 回落**，这是刻意的——双引擎意味着每个 schema 决策都要在两边都成立，
  Postgres 的原生类型就一直用不上（时间戳卡在字符串上很久，就是那段历史留下的）。不要为了"方便测试"把它加回来。
- Postgres 跑在 Compose 服务 `postgres` 里（镜像 `postgres:17`，库名 `ka_account`），**不往本机暴露 5432**。
  连接串由 compose 注入，不要在 `.env` 里写 `KA_ACCOUNT_DB_URL`（宿主机上的 `127.0.0.1:5432` 在容器里是错的）。
  Redis（`KA_ACCOUNT_REDIS_URL`）和 Milvus（`MILVUS_URI`）同样由 compose 注入，不要写成 `127.0.0.1`。
- 测试用 `KA_ACCOUNT_TEST_DB_URL`（同样由 compose 注入，指向容器内的 `postgres` 库当管理连接）。
  每个测试建一个一次性库、结束丢弃。测试拿数据库只有一个入口：
  `tests/conftest.py` 的 `make_db` 夹具，不要在测试里直接构造 `Database(...)`。没设这个变量测试会 skip，不会静默换引擎。
- 改表的流程是固定的：改 `db/orm.py` → `./dev py -m alembic revision --autogenerate -m "..."` → **人工检查生成的迁移文件** →
  `tests/test_db.py::test_migrations_match_the_models` 会在模型和迁移不一致时失败。绝不手写 `CREATE TABLE`。
- `db/` 之外的任何代码都**不许 import `orm`**。对外只有 `Database` 门面，它进出的是 pydantic 模型和普通 dict。

## 写代码时的硬约束

这几条是这个项目存在的理由，违反了就不是这个项目了：

1. **采购单的价格和规则由代码计算。** 这条只约束 `po/`：价格、SKU、MOQ、商业规则来自确定性代码。
   模型只把对话填进固定 slot，不算总价，也不判断规则是否通过、是否可以提交。
   合同分析、报表叙述、检索回答由各自的模型调用完成。
2. **副作用幂等。** confirm token 在建单**之前**烧掉。
3. **所有业务数据入库，按真实表建模**（列、外键、索引），不要 JSON blob 表。

## 约定

- **代码里的注释和 docstring 用英文**，README 和面向用户的文档用中文。跟着周围代码的密度走——
  这个库的注释解释的是"为什么"，不是"这行在干什么"。
- **测试不调模型。** 模型调用一律用 `FakeLLM`（`llm/` 是模型客户端，从 `backend.llm` import）。桌面页面的数据行为用
  `tests/test_desk_api.py` 打 HTTP；展示用的纯函数在 `frontend/` 里用 Vitest。数据库不是假的：测试连 Compose 里那份 Postgres。
- ruff：`line-length = 110`，规则集见 `pyproject.toml`。提交前 `./dev lint`。

## 坑

- **`.env` 里有真实的 API key**，已被 gitignore。不要提交、不要打印它的内容、不要把它的值写进任何输出。
  改配置示例改 `.env.example`。数据库密码不进 `.env`：compose 写死本地开发用的 `ka_account/ka_account`。
- **时间戳是 `timestamptz`，写入的值一律是 UTC-aware 的 `datetime`**（`database.utcnow`），不要再写 ISO 字符串。
  但**纯日期**（合同期限、报表期间）仍然是 `YYYY-MM-DD` 文本，这是有意的——它们是日历标签，背后没有具体时刻。
  改这两类字段前先确认自己在改哪一类。
- **桌面是 `frontend/`（Vite + React + TypeScript + Vitest）**，只通过 `app.py` 挂上的 router 跟后端说话。
  领域逻辑不受影响：`service.py` 以下完全不认识任何 UI 框架。不要把页面逻辑写回 Python。
- HTTP 请求体在 `schemas/`（`chat.py`、`memory.py`），**必须定义在模块层**再由 router import。
  因为有 `from __future__ import annotations`，定义在 `create_app` 内部的类会被 FastAPI 静默当成 query parameter。
- 容器里访问宿主机上的服务（Ollama 等）用 `host.docker.internal`，不要写 `127.0.0.1`。
