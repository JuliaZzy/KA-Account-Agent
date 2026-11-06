"""Environment → settings. Loads `<repo>/.env` once, never overriding real env vars.

Provider keys follow the model brand; feature knobs follow the capability.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=False)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


@dataclass(frozen=True)
class LLMSettings:
    timeout: int = 7200
    # Vendor chat clients force SDK retries off. Worker / Run retry is the only layer.
    max_retries: int = 0


@dataclass(frozen=True)
class VendorEndpoint:
    """URL and key for one vendor. Both come from `{BRAND}_BASE_URL` / `{BRAND}_API_KEY`."""

    base_url: str
    api_key: str


_VENDOR_ENV = {
    "sjtu": ("SJTU_BASE_URL", "SJTU_API_KEY"),
    "deepseek": ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"),
    "glm": ("GLM_BASE_URL", "GLM_API_KEY"),
    "qwen": ("QWEN_BASE_URL", "QWEN_API_KEY"),
}


def vendor_endpoint(brand: str) -> VendorEndpoint:
    """The only place chat clients read a URL and key."""
    names = _VENDOR_ENV.get(brand)
    if names is None:
        raise ValueError(f"unknown model brand {brand}")
    url_name, key_name = names
    base_url, api_key = env(url_name), env(key_name)
    if not base_url or not api_key:
        raise RuntimeError(f"{url_name} and {key_name} must be set.")
    return VendorEndpoint(base_url=base_url, api_key=api_key)


@dataclass(frozen=True)
class EmbeddingSettings:
    """Not the chat model. Milvus only stores vectors; this endpoint computes them."""

    base_url: str | None
    api_key: str
    model: str
    dimensions: int


def embedding_settings() -> EmbeddingSettings:
    raw = env("EMBEDDING_DIMENSIONS") or "1024"
    try:
        dimensions = int(raw)
    except ValueError:
        dimensions = 1024
    return EmbeddingSettings(
        base_url=env("EMBEDDING_BASE_URL") or None,
        api_key=env("EMBEDDING_API_KEY") or env("LLM_API_KEY") or env("OPENAI_API_KEY") or "",
        model=env("EMBEDDING_MODEL") or "text-embedding-v4",
        dimensions=dimensions,
    )


@dataclass(frozen=True)
class RerankSettings:
    """Not the chat model and not the embedding model.

    ``BAAI/bge-reranker-v2-m3`` is a cross-encoder: one score for the question concatenated with
    one passage. It covers Chinese and English, and a batch of 20 short chunks is the size it
    is used at. The client speaks the Cohere-style `POST /rerank` body (`model`, `query`,
    `documents`, `top_n`). No base URL means hybrid retrieval returns the fused list and does
    not call the reranker.
    """

    base_url: str | None
    api_key: str
    model: str


def rerank_settings() -> RerankSettings:
    return RerankSettings(
        base_url=env("RERANK_BASE_URL") or None,
        api_key=env("RERANK_API_KEY") or "",
        model=env("RERANK_MODEL") or "BAAI/bge-reranker-v2-m3",
    )


def redis_url() -> str:
    """Redis, from `KA_ACCOUNT_REDIS_URL`. Compose injects `redis://redis:6379/0`."""
    url = env("KA_ACCOUNT_REDIS_URL")
    if not url:
        raise RuntimeError(
            "KA_ACCOUNT_REDIS_URL is not set. Run via ./dev (Docker Compose injects it). "
            "Inside the stack it is redis://redis:6379/0"
        )
    return url


def _redis_with_db(index: int) -> str:
    """Same host as `redis_url()`, different Redis database number."""
    parts = urlsplit(redis_url())
    return urlunsplit(parts._replace(path=f"/{index}"))


def celery_broker_url() -> str:
    """Celery broker: Redis database 1.

    Database 0 is the app's own keys (session locks, turn results, FX cache).
    A separate database keeps Celery's key layout off those keys, and keeps a
    flush of the queue from wiping them. Derived from `KA_ACCOUNT_REDIS_URL`,
    so there is no broker URL to point at `127.0.0.1`.
    """
    return _redis_with_db(1)


def celery_result_url() -> str:
    """Celery result backend: Redis database 2, beside the broker on database 1."""
    return _redis_with_db(2)


def milvus_uri() -> str:
    """Milvus, from `MILVUS_URI`. Compose injects `http://milvus:19530`."""
    return env("MILVUS_URI")


def db_url() -> str:
    """The application database, from `KA_ACCOUNT_DB_URL`. PostgreSQL only.

    There is deliberately no fallback engine. A second engine means every schema decision
    has to hold on both, which is what kept timestamps as ISO-8601 text instead of
    `timestamptz`; and a silent fallback would let a misconfigured deploy come up on an
    empty database instead of failing. Missing config is an error, not a different mode."""
    url = env("KA_ACCOUNT_DB_URL")
    if not url:
        raise RuntimeError(
            "KA_ACCOUNT_DB_URL is not set. Run via ./dev (Docker Compose injects it). "
            "If you are inside a container without compose, set it to "
            "postgresql+psycopg://ka_account:ka_account@postgres:5432/ka_account"
        )
    return url
