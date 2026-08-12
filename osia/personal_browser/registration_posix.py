"""Truthful placeholder for unvalidated POSIX Native Messaging packaging."""

from __future__ import annotations


def registration_status() -> dict:
    return {
        "state": "incompatible",
        "reason": "platform_not_physically_validated",
        "registered": False,
    }


def setup(*, development: bool = False) -> dict:
    del development
    return registration_status()


def remove() -> dict:
    return {"state": "incompatible", "removed": False}


__all__ = ["registration_status", "remove", "setup"]
