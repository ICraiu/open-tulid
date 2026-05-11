from __future__ import annotations

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class WorkflowCompileDiagnostic:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    path: str | None = None
    line: int | None = None
    column: int | None = None
