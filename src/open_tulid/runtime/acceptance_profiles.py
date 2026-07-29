from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Mapping

from ruamel.yaml import YAML

from open_tulid.domain import DomainError

from .task_contracts import CHECK_ID_RE, CheckExpectation, SHELL_CONTROL_TOKENS


ACCEPTANCE_PROFILES_SCHEMA = "tulid.acceptance/v1"
ACCEPTANCE_PROFILE_KINDS = frozenset({
    "unit", "build", "static", "component", "vertical_slice", "host_smoke",
})


@dataclass(frozen=True)
class AcceptanceProfile:
    id: str
    kind: str
    argv: tuple[str, ...]
    timeout_seconds: int
    working_directory: str
    expect: CheckExpectation


@dataclass(frozen=True)
class AcceptanceProfileResult:
    profiles: Mapping[str, AcceptanceProfile] | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def load_acceptance_profiles(project_root: Path) -> AcceptanceProfileResult:
    path = project_root / "acceptance.yaml"
    if not path.is_file():
        return AcceptanceProfileResult(profiles={})
    try:
        raw = YAML(typ="safe").load(StringIO(path.read_text(encoding="utf-8")))
    except Exception as exc:
        return AcceptanceProfileResult(errors=(_error("acceptance_profiles.invalid_yaml", f"Cannot parse acceptance profiles: {exc}", str(path)),))
    if not isinstance(raw, Mapping) or set(raw) - {"schema", "profiles"}:
        return AcceptanceProfileResult(errors=(_error("acceptance_profiles.invalid_shape", "acceptance.yaml must contain only schema and profiles mappings.", str(path)),))
    if raw.get("schema") != ACCEPTANCE_PROFILES_SCHEMA or not isinstance(raw.get("profiles"), Mapping):
        return AcceptanceProfileResult(errors=(_error("acceptance_profiles.invalid_shape", f"acceptance.yaml requires schema {ACCEPTANCE_PROFILES_SCHEMA!r} and a profiles mapping.", str(path)),))
    profiles: dict[str, AcceptanceProfile] = {}
    errors: list[DomainError] = []
    for identifier, value in raw["profiles"].items():
        profile_id = str(identifier)
        location = f"{path}:profiles.{profile_id}"
        if not CHECK_ID_RE.fullmatch(profile_id) or not isinstance(value, Mapping):
            errors.append(_error("acceptance_profiles.profile_invalid", "Profile id or definition is invalid.", location))
            continue
        if set(value) - {"kind", "argv", "timeout_seconds", "working_directory", "expect"}:
            errors.append(_error("acceptance_profiles.profile_invalid", "Profile has unknown fields.", location))
            continue
        kind = value.get("kind")
        argv = value.get("argv")
        timeout = value.get("timeout_seconds", 120)
        working_directory = value.get("working_directory", ".")
        expect = value.get("expect", {})
        if kind not in ACCEPTANCE_PROFILE_KINDS or not isinstance(argv, list) or not argv or any(not isinstance(part, str) or not part.strip() for part in argv) or any(part in SHELL_CONTROL_TOKENS for part in argv) or isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600 or not isinstance(working_directory, str) or Path(working_directory).is_absolute() or ".." in Path(working_directory).parts or not isinstance(expect, Mapping):
            errors.append(_error("acceptance_profiles.profile_invalid", "Profile kind, argv, timeout, working directory, or expectation is invalid.", location))
            continue
        exit_code = expect.get("exit_code", 0)
        stdout = expect.get("stdout_contains", ())
        stderr = expect.get("stderr_contains", ())
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not _strings(stdout) or not _strings(stderr):
            errors.append(_error("acceptance_profiles.expect_invalid", "Profile expectation is invalid.", location))
            continue
        profiles[profile_id] = AcceptanceProfile(
            profile_id, kind, tuple(argv), timeout, working_directory,
            CheckExpectation(exit_code, tuple(stdout), tuple(stderr)),
        )
    return AcceptanceProfileResult(profiles=profiles if not errors else None, errors=tuple(errors))


def _strings(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) and item for item in value)


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
