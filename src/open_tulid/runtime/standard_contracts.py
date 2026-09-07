from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Mapping

from ruamel.yaml import YAML

from open_tulid.domain import DomainError

from .task_contracts import SHELL_CONTROL_TOKENS

CONTRACT_SCHEMA = "tulid.contract/v1"
STANDARD_CONTRACT_SCHEMA = CONTRACT_SCHEMA
CONTRACT_FILENAME = "contract.yaml"
STANDARD_CONTRACT_FILENAME = CONTRACT_FILENAME


@dataclass(frozen=True)
class CommandExpectation:
    exit_code: int = 0


@dataclass(frozen=True)
class ProjectCommand:
    """A deterministic, explicit, non-empty argv command that Tulid runs in the
    completed worker workspace to verify the project.

    The command passes only when its configured exit expectation is met. There is
    no file/directory allowlist, no predicted-file requirement, and no per-task
    LLM authorship: the configured command list is the whole contract.
    """

    name: str
    argv: tuple[str, ...]
    working_directory: str = "."
    timeout_seconds: int = 300
    expect: CommandExpectation = CommandExpectation()


@dataclass(frozen=True)
class StandardRuntime:
    # Infrastructure concerns only; they never act as contract acceptance.
    container_user: str | None
    # OpenCode/agent runtime-home placement. Never a project-root file; the
    # runtime keeps it under the verifier-excluded runtime directory.
    opencode_config_home: str


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    visible_feedback: bool = True


@dataclass(frozen=True)
class StandardContract:
    """The project's single global verification contract: command list only.

    Project configuration is authoritative and human-authored. Tulid never
    synthesizes a contract with an LLM and never copies these commands into task
    artifacts.
    """

    schema: str
    runtime: StandardRuntime
    commands: tuple[ProjectCommand, ...] = ()
    retry: RetryPolicy = RetryPolicy()


@dataclass(frozen=True)
class StandardContractResult:
    contract: StandardContract | None = None
    errors: tuple[DomainError, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors


def find_standard_contract_path(project_root: Path) -> Path:
    return project_root / CONTRACT_FILENAME


def standard_contract_configured(project_root: Path) -> bool:
    return find_standard_contract_path(project_root).is_file()


def load_standard_contract(project_root: Path) -> StandardContractResult:
    path = find_standard_contract_path(project_root)
    if not path.is_file():
        return StandardContractResult()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return StandardContractResult(errors=(_error(
            "contract.read_failed",
            f"Cannot read the project contract: {exc}",
            str(path),
        ),))
    try:
        raw = YAML(typ="safe").load(StringIO(text))
    except Exception as exc:
        return StandardContractResult(errors=(_error(
            "contract.invalid_yaml",
            f"Project contract is not valid YAML: {exc}",
            str(path),
        ),))
    if raw is None:
        return StandardContractResult(errors=(_error(
            "contract.empty",
            "Project contract is empty.",
            str(path),
        ),))
    if not isinstance(raw, Mapping):
        return StandardContractResult(errors=(_error(
            "contract.invalid_shape",
            "Project contract must be a YAML mapping.",
            str(path),
        ),))
    errors: list[DomainError] = []
    _reject_unknown_fields(raw, {"schema", "runtime", "commands", "retry"}, errors, str(path))

    schema = raw.get("schema")
    if schema != CONTRACT_SCHEMA or not isinstance(schema, str):
        errors.append(_error(
            "contract.unsupported_schema",
            f"Project contract requires schema {CONTRACT_SCHEMA!r}.",
            f"{path}:schema",
        ))
    runtime = _parse_runtime(raw.get("runtime", {}), path, errors)
    commands = _parse_commands(raw.get("commands"), path, errors)
    retry = _parse_retry(raw.get("retry"), path, errors)
    if errors:
        return StandardContractResult(errors=tuple(errors))
    return StandardContractResult(contract=StandardContract(
        schema=schema,
        runtime=runtime,
        commands=commands,
        retry=retry,
    ))


def _parse_runtime(raw: object, path: Path, errors: list[DomainError]) -> StandardRuntime:
    location = f"{path}:runtime"
    if not isinstance(raw, Mapping):
        errors.append(_error("contract.runtime_invalid", "runtime must be a mapping.", location))
        return StandardRuntime(container_user=None, opencode_config_home=".open-tulid/home")
    _reject_unknown_fields(raw, {"container_user", "opencode_config_home"}, errors, location)
    container_user = raw.get("container_user")
    if container_user is not None and (not isinstance(container_user, str) or not container_user.strip()):
        errors.append(_error("contract.container_user_invalid", "runtime.container_user must be a non-empty string.", f"{location}.container_user"))
        container_user = None
    if container_user is not None:
        normalized = container_user.strip()
        parts = [p for p in normalized.split(":") if p != ""]
        uid = parts[0] if parts else ""
        gid = parts[1] if len(parts) > 1 else None
        if not uid.isdigit() or (gid is not None and not gid.isdigit()):
            errors.append(_error("contract.container_user_invalid", "runtime.container_user must be a numeric uid[:gid], for example '1000:1000'.", f"{location}.container_user"))
            container_user = None
    opencode_config_home = raw.get("opencode_config_home")
    default_home = ".open-tulid/home"
    if opencode_config_home is None:
        opencode_config_home = default_home
    if not isinstance(opencode_config_home, str) or not opencode_config_home.strip():
        errors.append(_error("contract.opencode_config_home_invalid", "runtime.opencode_config_home must be a non-empty string.", f"{location}.opencode_config_home"))
        opencode_config_home = default_home
    opencode_config_home = opencode_config_home.strip()
    if opencode_config_home in {".", "", "opencode.json"}:
        errors.append(_error(
            "contract.opencode_config_home_forbidden",
            "OpenCode config home must never be the project root; place it under an excluded runtime directory.",
            f"{location}.opencode_config_home",
        ))
    clean = Path(opencode_config_home)
    if clean.is_absolute() or any(part in {"", "."} for part in clean.parts):
        errors.append(_error(
            "contract.opencode_config_home_invalid",
            f"runtime.opencode_config_home must be a workspace-relative path: {opencode_config_home!r}.",
            f"{location}.opencode_config_home",
        ))
    return StandardRuntime(
        container_user=container_user.strip() if container_user else None,
        opencode_config_home=opencode_config_home,
    )


def _parse_commands(raw: object, path: Path, errors: list[DomainError]) -> tuple[ProjectCommand, ...]:
    location = f"{path}:commands"
    if raw is None:
        errors.append(_error("contract.commands_missing", "Project contract requires a non-empty commands list.", location))
        return ()
    if not isinstance(raw, list):
        errors.append(_error("contract.commands_invalid", "commands must be a list of command definitions.", location))
        return ()
    parsed: list[ProjectCommand] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item_location = f"{path}:commands[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_error("contract.command_invalid", "Each contract command must be a mapping.", item_location))
            continue
        _reject_unknown_fields(
            item,
            {"name", "argv", "working_directory", "timeout_seconds", "expect"},
            errors,
            item_location,
        )
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(_error("contract.command_name_missing", "Contract command requires a non-empty name.", f"{item_location}.name"))
            name = f"command_{index}"
        name = name.strip()
        if name in seen:
            errors.append(_error("contract.command_name_duplicate", f"Contract command name appears more than once: {name}", f"{item_location}.name"))
        seen.add(name)

        argv = _parse_argv(item.get("argv"), f"{item_location}.argv", errors)

        working_directory = item.get("working_directory", ".")
        if not isinstance(working_directory, str) or not working_directory.strip() or Path(working_directory).is_absolute() or ".." in Path(working_directory).parts:
            errors.append(_error("contract.command_working_directory_invalid", f"Contract command {name!r}.working_directory must be a workspace-relative directory.", f"{item_location}.working_directory"))
            working_directory = "."

        timeout_seconds = item.get("timeout_seconds", 300)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
            errors.append(_error("contract.command_timeout_invalid", f"Contract command {name!r}.timeout_seconds must be an integer from 1 to 3600.", f"{item_location}.timeout_seconds"))
            timeout_seconds = 300

        expect = CommandExpectation()
        expect_raw = item.get("expect")
        if expect_raw is not None:
            if not isinstance(expect_raw, Mapping):
                errors.append(_error("contract.command_expect_invalid", f"Contract command {name!r}.expect must be a mapping.", f"{item_location}.expect"))
            else:
                _reject_unknown_fields(expect_raw, {"exit_code"}, errors, f"{item_location}.expect")
                exit_code = expect_raw.get("exit_code", 0)
                if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                    errors.append(_error("contract.command_exit_code_invalid", f"Contract command {name!r}.expect.exit_code must be an integer.", f"{item_location}.expect.exit_code"))
                    exit_code = 0
                expect = CommandExpectation(exit_code=exit_code)

        parsed.append(ProjectCommand(
            name=name,
            argv=argv,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
            expect=expect,
        ))
    return tuple(parsed)


def _parse_argv(raw: object, location: str, errors: list[DomainError]) -> tuple[str, ...]:
    if raw is None:
        errors.append(_error("contract.command_argv_missing", "Contract command requires a non-empty argv array.", location))
        return ()
    if not isinstance(raw, list):
        errors.append(_error("contract.command_argv_invalid", "Contract command argv must be a list of strings.", location))
        return ()
    parts: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            errors.append(_error("contract.command_argv_item_invalid", f"Contract command argv entry {index} must be a non-empty string.", f"{location}[{index}]"))
            continue
        parts.append(item.strip())
    if not parts:
        errors.append(_error("contract.command_argv_empty", "Contract command requires a non-empty argv array.", location))
        return ()
    controls = tuple(part for part in parts if part in SHELL_CONTROL_TOKENS)
    if controls:
        errors.append(_error(
            "contract.command_shell_control",
            f"Contract command cannot contain shell control tokens: {', '.join(controls)}.",
            location,
        ))
    return tuple(parts)


def _parse_retry(raw: object, path: Path, errors: list[DomainError]) -> RetryPolicy:
    location = f"{path}:retry"
    if raw is None:
        return RetryPolicy()
    if not isinstance(raw, Mapping):
        errors.append(_error("contract.retry_invalid", "retry must be a mapping.", location))
        return RetryPolicy()
    _reject_unknown_fields(raw, {"max_attempts", "visible_feedback", "feedback"}, errors, location)
    max_attempts = raw.get("max_attempts", 3)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 0:
        errors.append(_error("contract.retry_max_attempts_invalid", "retry.max_attempts must be a non-negative integer.", f"{location}.max_attempts"))
        max_attempts = 3
    feedback = raw.get("visible_feedback", raw.get("feedback", True))
    if not isinstance(feedback, bool):
        errors.append(_error("contract.retry_feedback_invalid", "retry.visible_feedback must be a boolean.", f"{location}.visible_feedback"))
        feedback = True
    return RetryPolicy(max_attempts=max_attempts, visible_feedback=feedback)


def _reject_unknown_fields(
    raw: Mapping[str, object],
    allowed: set[str],
    errors: list[DomainError],
    location: str,
) -> None:
    for key in raw:
        if str(key) not in allowed:
            errors.append(_error(
                "contract.unknown_field",
                f"Unknown project contract field: {key}",
                f"{location}.{key}",
            ))


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)
