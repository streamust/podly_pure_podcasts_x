"""Single source of truth for the selected runtime writer backend."""

from __future__ import annotations

import os
from enum import StrEnum


class WriterBackend(StrEnum):
    PYTHON = "python"
    RUST = "rust"


def selected_writer_backend() -> WriterBackend:
    """Read and validate the shared startup/client backend setting.

    Rust is the default; ``python`` is the rollback. Invalid values fail closed so a typo
    cannot select a different writer implementation in one process.
    """
    value = os.environ.get("PODLY_WRITER_BACKEND", WriterBackend.RUST.value)
    try:
        return WriterBackend(value.strip().lower())
    except ValueError as exc:
        raise RuntimeError(
            "PODLY_WRITER_BACKEND must be either 'python' or 'rust'"
        ) from exc


if __name__ == "__main__":
    print(selected_writer_backend().value)
