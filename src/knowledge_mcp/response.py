"""Standard response envelope and error codes."""

from typing import Any


class ResponseEnvelope:
    """Standard response envelope for all tools."""

    @staticmethod
    def success(message: str, data: Any = None) -> dict:
        """Create a success response."""
        return {
            "ok": True,
            "error": None,
            "message": message,
            "data": data or {}
        }

    @staticmethod
    def ok(message: str, data: Any = None) -> dict:
        """Alias for success() - create a success response."""
        return ResponseEnvelope.success(message, data)

    @staticmethod
    def error(code: str, message: str, data: Any = None) -> dict:
        """Create an error response."""
        return {
            "ok": False,
            "error": code,
            "message": message,
            "data": data or {}
        }


class ErrorCodes:
    """Common error codes across all servers."""
    UNEXPECTED_EXCEPTION = "unexpected_exception"
    INVALID_ARGUMENT = "invalid_argument"
    INVALID_INPUT = "invalid_input"  # Alias for INVALID_ARGUMENT
    NOT_FOUND = "not_found"
    IO_ERROR = "io_error"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    FORBIDDEN = "forbidden"
    POLICY_VIOLATION = "policy_violation"
    UNIT_NOT_ALLOWED = "unit_not_allowed"
    INTERNAL_ERROR = "internal_error"
    EXTERNAL_SERVICE_ERROR = "external_service_error"
