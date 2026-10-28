"""Typed failures. `exit_code` is the process exit code and the HTTP `exitCode`."""

from __future__ import annotations

from tgdl.contract import (
    EXIT_AUTH,
    EXIT_FAILURE,
    EXIT_PARTIAL,
    EXIT_REJECTED,
    EXIT_USAGE,
)


class TgdlError(Exception):
    exit_code = EXIT_FAILURE

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(message)
        self.extra = extra


class UsageError(TgdlError):
    exit_code = EXIT_USAGE


class AuthError(TgdlError):
    exit_code = EXIT_AUTH


class PartialError(TgdlError):
    exit_code = EXIT_PARTIAL


class RejectedError(TgdlError):
    exit_code = EXIT_REJECTED
