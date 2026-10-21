from dataclasses import dataclass
from enum import Enum
from typing import Any


class WriteCommandType(Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    # Critical for integrity: Execute multiple operations in one commit
    TRANSACTION = "transaction"
    # For complex logic that needs to run inside the writer (e.g. "deduct_credits_and_start_job")
    ACTION = "action"


@dataclass
class WriteCommand:
    id: str
    type: WriteCommandType
    model: str | None
    data: dict[str, Any]
    # The queue to send the result back to (managed by the client)
    reply_queue: Any = None
    # Monotonic client timestamp used only for opt-in benchmark timing. It is
    # process-local clock data, never persisted or returned as a write result.
    enqueued_monotonic_ns: int | None = None


@dataclass
class WriteResult:
    command_id: str
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None
