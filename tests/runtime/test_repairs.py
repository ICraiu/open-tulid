from open_tulid.domain import DomainError
from open_tulid.runtime.repairs import build_repair_packet, plan_repair
from open_tulid.runtime.verifier import VerificationCheckResult, VerificationReport


def _report(classification: str = "implementation_failure") -> VerificationReport:
    return VerificationReport(
        schema="tulid.verification/v1",
        classification=classification,
        baseline_sha256="baseline",
        post_manifest_sha256="post",
        edited=("src/example.py",),
        checks=(VerificationCheckResult(
            id="unit", status="failed", argv=("pytest",), exit_code=1,
            stdout="x" * 2_100, stderr="failure",
        ),),
    )


def test_implementation_failure_gets_evidence_only_bounded_repair_packet():
    plan = plan_repair(
        report=_report(),
        errors=(DomainError(code="verification.check_failed", message="unit failed"),),
        repair_attempts=0,
    )

    assert plan.eligible is True
    assert plan.packet is not None
    assert "# Open Tulid Repair" in plan.packet
    assert "src/example.py" in plan.packet
    assert "[truncated]" in plan.packet
    assert "Task Body" not in plan.packet
    assert "Execution Contract" not in plan.packet


def test_non_implementation_failure_does_not_spend_or_create_repair():
    plan = plan_repair(
        report=_report("contract_failure"),
        errors=(DomainError(code="verification.path_forbidden", message="outside scope"),),
        repair_attempts=0,
    )

    assert plan.eligible is False
    assert plan.reason == "contract_failure"
    assert plan.packet is None


def test_repair_limit_blocks_additional_packet():
    plan = plan_repair(
        report=_report(),
        errors=(DomainError(code="verification.check_failed", message="unit failed"),),
        repair_attempts=2,
        max_repair_attempts=2,
    )

    assert plan.eligible is False
    assert plan.reason == "repair_limit_reached"
