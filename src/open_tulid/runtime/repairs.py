"""Bounded, evidence-only repair packets for rejected executions."""

from __future__ import annotations

import json
from dataclasses import dataclass

from open_tulid.domain import DomainError
from open_tulid.runtime.verifier import VerificationReport


REPAIR_PACKET_SCHEMA = "tulid.repair/v1"
DEFAULT_MAX_REPAIR_ATTEMPTS = 2
_EVIDENCE_LIMIT = 2_000


@dataclass(frozen=True)
class RepairPlan:
    eligible: bool
    packet: str | None = None
    reason: str | None = None


def plan_repair(*, report: VerificationReport | None, errors: tuple[DomainError, ...], repair_attempts: int, max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS) -> RepairPlan:
    """Return a repair packet only for bounded implementation failures."""
    classification = report.classification if report is not None else None
    if classification != "implementation_failure":
        return RepairPlan(False, reason=classification or "verification_failure")
    if repair_attempts >= max_repair_attempts:
        return RepairPlan(False, reason="repair_limit_reached")
    return RepairPlan(True, packet=build_repair_packet(report=report, errors=errors))


def build_repair_packet(*, report: VerificationReport, errors: tuple[DomainError, ...]) -> str:
    """Render only verifier evidence; never re-inject task or instruction context."""
    failed_checks = [{"id": check.id, "exit_code": check.exit_code,
                      "stdout": _bounded(check.stdout), "stderr": _bounded(check.stderr)}
                     for check in report.checks if check.status != "passed"]
    report_data = report.to_dict()
    evidence = {
        "schema": REPAIR_PACKET_SCHEMA,
        "classification": report.classification,
        "verification_report": report_data,
        "failed_check_evidence": failed_checks,
        "current_diff_summary": report_data["changes"],
        "errors": [{"code": error.code, "message": error.message, "location": error.location} for error in errors],
    }
    return "\n".join((
        "# Open Tulid Repair",
        "Fix only the verified implementation failure in the existing workspace.",
        "Keep the frozen scope unchanged. Do not plan, broaden scope, or modify the contract.",
        "Submit completion again after making the smallest repair that addresses this evidence.",
        "", "```json", json.dumps(evidence, sort_keys=True, indent=2), "```",
    )) + "\n"


def _bounded(value: str) -> str:
    return value if len(value) <= _EVIDENCE_LIMIT else value[:_EVIDENCE_LIMIT] + "\n[truncated]"
