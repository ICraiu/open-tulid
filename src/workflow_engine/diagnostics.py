from __future__ import annotations

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class SourceSpan:
    path: str
    line: int | None = None
    column: int | None = None


@dataclasses.dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    path: str | None = None
    line: int | None = None
    column: int | None = None
