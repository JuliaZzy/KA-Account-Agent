"""Errors raised by model transport and capacity guards."""

from __future__ import annotations


class ModelCapacityExceeded(Exception):
    """No local concurrency seat was available for this model provider."""

    code = "RATE_LIMITED"
    retryable = True

    def __init__(self, brand: str, model: str, message: str | None = None) -> None:
        self.brand = brand
        self.model = model
        super().__init__(message or f"model capacity is full for {brand}")
