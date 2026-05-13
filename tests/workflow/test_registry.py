from __future__ import annotations

from open_tulid.workflow import (
    ArgDefinition,
    OperationSpec,
    RuntimeRegistries,
    ValidationSpec,
    WorkerSpec,
    build_registries,
    get_builtin_registries,
    validate_registries,
)


class TestGetBuiltinRegistries:
    def test_returns_runtime_registries(self):
        regs = get_builtin_registries()
        assert isinstance(regs, RuntimeRegistries)

    def test_validates_cleanly(self):
        regs = get_builtin_registries()
        diags = validate_registries(regs)
        assert diags == ()

    def test_required_validations_present(self):
        regs = get_builtin_registries()
        required = {
            "project_build",
            "git_status_clean",
            "file_exists",
            "artifact_in_vault",
            "artifact_link_in_vault",
            "artifact_matches_template",
            "template_sections_present",
            "template_required_fields_present",
            "artifact_has_required_text",
            "branch_exists",
            "tests_pass",
            "link_target_exists",
        }
        missing = required - set(regs.validations.keys())
        assert not missing, f"missing validations: {missing}"

    def test_required_operations_present(self):
        regs = get_builtin_registries()
        required = {
            "move_task",
            "copy_file",
            "copy_field",
            "set_field",
            "link_artifact",
            "git_add",
            "git_commit",
            "git_reset_hard",
            "create_branch",
            "checkout_branch",
            "write_file",
            "append_event",
            "update_kanban_view",
        }
        missing = required - set(regs.operations.keys())
        assert not missing, f"missing operations: {missing}"

    def test_required_workers_present(self):
        regs = get_builtin_registries()
        required = {
            "local_llm",
            "shell_command",
            "human_approval",
            "noop",
        }
        missing = required - set(regs.workers.keys())
        assert not missing, f"missing workers: {missing}"

    def test_git_reset_hard_is_destructive(self):
        regs = get_builtin_registries()
        spec = regs.operations["git_reset_hard"]
        assert spec.destructive is True

    def test_git_reset_hard_requires_approval(self):
        regs = get_builtin_registries()
        spec = regs.operations["git_reset_hard"]
        assert spec.requires_approval is True


class TestBuildRegistries:
    def test_empty_registries(self):
        regs, diags = build_registries()
        assert regs is not None
        assert diags == ()

    def test_duplicate_validation_ids_rejected(self):
        regs, diags = build_registries(
            validations=[
                ValidationSpec(id="v1", implementation=object()),
                ValidationSpec(id="v1", implementation=object()),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_empty_validation_id_rejected(self):
        regs, diags = build_registries(
            validations=[
                ValidationSpec(id="", implementation=object()),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_missing_implementation_rejected(self):
        regs, diags = build_registries(
            validations=[
                ValidationSpec(id="v1", implementation=None),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_missing_implementation" for d in diags)

    def test_invalid_arg_type_rejected(self):
        regs, diags = build_registries(
            validations=[
                ValidationSpec(
                    id="v1",
                    implementation=object(),
                    args={"x": ArgDefinition(type="bogus_type")},
                ),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_invalid_argument_type" for d in diags)

    def test_duplicate_operation_ids_rejected(self):
        regs, diags = build_registries(
            operations=[
                OperationSpec(id="op1", implementation=object()),
                OperationSpec(id="op1", implementation=object()),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_empty_operation_id_rejected(self):
        regs, diags = build_registries(
            operations=[
                OperationSpec(id="", implementation=object()),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_missing_operation_implementation_rejected(self):
        regs, diags = build_registries(
            operations=[
                OperationSpec(id="op1", implementation=None),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_missing_implementation" for d in diags)

    def test_duplicate_worker_ids_rejected(self):
        regs, diags = build_registries(
            workers=[
                WorkerSpec(id="w1", implementation=object()),
                WorkerSpec(id="w1", implementation=object()),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_empty_worker_id_rejected(self):
        regs, diags = build_registries(
            workers=[
                WorkerSpec(id="", implementation=object()),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_missing_worker_implementation_rejected(self):
        regs, diags = build_registries(
            workers=[
                WorkerSpec(id="w1", implementation=None),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_missing_implementation" for d in diags)

    def test_cleanup_operation_unknown_rejected(self):
        regs, diags = build_registries(
            operations=[
                OperationSpec(
                    id="op1",
                    implementation=object(),
                    cleanup_operation="nonexistent_op",
                ),
            ],
        )
        assert regs is None
        assert any(d.code == "workflow.compile.registry_missing_implementation" for d in diags)

    def test_cleanup_operation_valid(self):
        regs, diags = build_registries(
            operations=[
                OperationSpec(id="cleanup_op", implementation=object()),
                OperationSpec(
                    id="main_op",
                    implementation=object(),
                    cleanup_operation="cleanup_op",
                ),
            ],
        )
        assert regs is not None
        assert diags == ()

    def test_valid_registries_build(self):
        regs, diags = build_registries(
            validations=[ValidationSpec(id="v1", implementation=object())],
            operations=[OperationSpec(id="op1", implementation=object())],
            workers=[WorkerSpec(id="w1", implementation=object())],
        )
        assert regs is not None
        assert diags == ()
        assert "v1" in regs.validations
        assert "op1" in regs.operations
        assert "w1" in regs.workers


class TestValidateRegistries:
    def test_valid_registries_no_diagnostics(self):
        regs = get_builtin_registries()
        diags = validate_registries(regs)
        assert diags == ()

    def test_single_entry_no_diagnostics(self):
        regs = RuntimeRegistries(
            validations={"v1": ValidationSpec(id="v1", implementation=object())},
            operations={},
            workers={},
        )
        diags = validate_registries(regs)
        assert diags == ()

    def test_missing_implementation_in_validation(self):
        regs = RuntimeRegistries(
            validations={"v1": ValidationSpec(id="v1", implementation=None)},
            operations={},
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_missing_implementation" for d in diags)

    def test_invalid_arg_type_in_validation(self):
        regs = RuntimeRegistries(
            validations={
                "v1": ValidationSpec(
                    id="v1",
                    implementation=object(),
                    args={"x": ArgDefinition(type="bogus")},
                )
            },
            operations={},
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_invalid_argument_type" for d in diags)

    def test_cleanup_op_unknown_in_validate(self):
        regs = RuntimeRegistries(
            validations={},
            operations={
                "op1": OperationSpec(
                    id="op1",
                    implementation=object(),
                    cleanup_operation="nonexistent",
                )
            },
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_missing_implementation" for d in diags)

    def test_duplicate_validation_ids_detected(self):
        regs = RuntimeRegistries(
            validations={
                "a": ValidationSpec(id="same", implementation=object()),
                "b": ValidationSpec(id="same", implementation=object()),
            },
            operations={},
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_duplicate_operation_ids_detected(self):
        regs = RuntimeRegistries(
            validations={},
            operations={
                "a": OperationSpec(id="same", implementation=object()),
                "b": OperationSpec(id="same", implementation=object()),
            },
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_duplicate_worker_ids_detected(self):
        regs = RuntimeRegistries(
            validations={},
            operations={},
            workers={
                "a": WorkerSpec(id="same", implementation=object()),
                "b": WorkerSpec(id="same", implementation=object()),
            },
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_key_spec_id_mismatch_validation(self):
        regs = RuntimeRegistries(
            validations={
                "map_key": ValidationSpec(id="different", implementation=object()),
            },
            operations={},
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_key_spec_id_mismatch_operation(self):
        regs = RuntimeRegistries(
            validations={},
            operations={
                "map_key": OperationSpec(id="different", implementation=object()),
            },
            workers={},
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)

    def test_key_spec_id_mismatch_worker(self):
        regs = RuntimeRegistries(
            validations={},
            operations={},
            workers={
                "map_key": WorkerSpec(id="different", implementation=object()),
            },
        )
        diags = validate_registries(regs)
        assert any(d.code == "workflow.compile.registry_duplicate_id" for d in diags)


class TestRegistryImmutability:
    def test_build_registries_validations_map_immutable(self):
        regs, diags = build_registries(
            validations=[ValidationSpec(id="v1", implementation=object())],
        )
        assert regs is not None
        import pytest
        with pytest.raises(TypeError):
            regs.validations["hacked"] = ValidationSpec(id="hacked", implementation=object())

    def test_build_registries_operations_map_immutable(self):
        regs, diags = build_registries(
            operations=[OperationSpec(id="op1", implementation=object())],
        )
        assert regs is not None
        import pytest
        with pytest.raises(TypeError):
            regs.operations["hacked"] = OperationSpec(id="hacked", implementation=object())

    def test_build_registries_workers_map_immutable(self):
        regs, diags = build_registries(
            workers=[WorkerSpec(id="w1", implementation=object())],
        )
        assert regs is not None
        import pytest
        with pytest.raises(TypeError):
            regs.workers["hacked"] = WorkerSpec(id="hacked", implementation=object())

    def test_builtin_registries_maps_immutable(self):
        regs = get_builtin_registries()
        import pytest
        with pytest.raises(TypeError):
            regs.validations["hacked"] = ValidationSpec(id="hacked", implementation=object())
