"""Celery is configured off the app's Redis database. No broker connection."""

from __future__ import annotations

from backend.config import celery_broker_url, celery_result_url


def test_celery_urls_share_the_app_redis_host(monkeypatch):
    monkeypatch.setenv(
        "KA_ACCOUNT_REDIS_URL", "redis://:s3cret@redis.internal:6379/0?socket_timeout=2",
    )
    assert celery_broker_url() == "redis://:s3cret@redis.internal:6379/1?socket_timeout=2"
    assert celery_result_url() == "redis://:s3cret@redis.internal:6379/2?socket_timeout=2"


def test_celery_app_uses_those_urls_and_the_ping_task():
    from backend.celery.tasks import ping
    from backend.celery.worker import celery

    assert celery.conf.broker_url == celery_broker_url()
    assert celery.conf.result_backend == celery_result_url()
    assert celery_broker_url().endswith("/1")
    assert celery_result_url().endswith("/2")
    assert ping.name == "ka_account.ping"
    assert "ka_account.ping" in celery.tasks
    from backend.celery.tasks import execute_run
    assert execute_run.name == "ka_account.execute_run"
    assert "ka_account.execute_run" in celery.tasks
    assert celery.conf.task_acks_late is True
    assert celery.conf.task_reject_on_worker_lost is True
