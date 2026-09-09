from open_tulid.domain import DomainError
from open_tulid.runtime.repairs import build_repair_packet, plan_repair
from open_tulid.runtime.verifier import (
    CoverageChange,
    VerificationCheckResult,
    VerificationReport,
)


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
            log_refs=("logs/000.stdout.log", "logs/000.stderr.log"),
        ),),
        classification_detail="failed_behavior",
        coverage_changes=(CoverageChange("delete", "tests/test_app.py"),),
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


def test_repair_packet_carries_stable_log_refs_and_granular_classification():
    packet = build_repair_packet(
        report=_report(),
        errors=(DomainError(code="verification.check_failed", message="unit failed"),),
    )
    # Distinguish a failed assertion from a missing interpreter/service/lockfile
    # through the granular classification, and reference the retained full logs
    # instead of copying all noisy output into the prompt.
    assert '"classification_detail": "failed_behavior"' in packet
    assert '"log_refs"' in packet
    assert "logs/000.stdout.log" in packet
    assert "logs/000.stderr.log" in packet
    # Coverage-regression evidence is returned so a disabled suite is visible.
    assert '"coverage_changes"' in packet
    assert "tests/test_app.py" in packet


def test_repair_packet_distinguishes_environment_blocker_from_failed_assertion():
    env_packet = build_repair_packet(
        report=_report("environment_failure"),
        errors=(DomainError(code="verification.check_environment", message="missing node"),),
    )
    assert '"classification": "environment_failure"' in env_packet


def test_contract_failure_gets_a_bounded_repair_packet():
    plan = plan_repair(
        report=_report("contract_failure"),
        errors=(DomainError(code="verification.path_forbidden", message="outside scope"),),
        repair_attempts=0,
    )

    assert plan.eligible is True
    assert plan.packet is not None


def test_environment_failure_does_not_create_a_worker_repair():
    plan = plan_repair(
        report=_report("environment_failure"),
        errors=(DomainError(code="verification.check_environment", message="unavailable"),),
        repair_attempts=0,
    )

    assert plan.eligible is False
    assert plan.reason == "environment_failure"


def test_repair_limit_blocks_additional_packet():
    plan = plan_repair(
        report=_report(),
        errors=(DomainError(code="verification.check_failed", message="unit failed"),),
        repair_attempts=2,
        max_repair_attempts=2,
    )

    assert plan.eligible is False
    assert plan.reason == "repair_limit_reached"
