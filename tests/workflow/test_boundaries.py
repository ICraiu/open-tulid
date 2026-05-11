from __future__ import annotations

import ast as pyast
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src"


class TestWorkflowEngineStandalone:
    def test_workflow_engine_does_not_import_open_tulid(self):
        we_init = SRC / "workflow_engine" / "__init__.py"
        source = we_init.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.Import, pyast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                if isinstance(node, pyast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("open_tulid"), \
                            f"workflow_engine imports {alias.name}"
                elif module:
                    assert not module.startswith("open_tulid"), \
                        f"workflow_engine imports from {module}"

    def test_workflow_engine_ast_does_not_import_open_tulid(self):
        ast_file = SRC / "workflow_engine" / "ast.py"
        source = ast_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid"), \
                    f"workflow_engine.ast imports from {module}"


class TestCompilerBoundaries:
    def test_compiler_may_import_workflow_engine(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        assert "workflow_engine" in source

    def test_compiler_does_not_import_open_tulid_cli(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid.cli"), \
                    f"compiler imports from {module}"
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("open_tulid.cli"), \
                        f"compiler imports {alias.name}"

    def test_compiler_does_not_import_open_tulid_vault(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid.vault"), \
                    f"compiler imports from {module}"
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("open_tulid.vault"), \
                        f"compiler imports {alias.name}"

    def test_compiler_does_not_import_subprocess(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    assert alias.name != "subprocess", \
                        "compiler imports subprocess"
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert module != "subprocess", \
                    "compiler imports from subprocess"

    def test_compiler_does_not_import_git(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    assert alias.name != "git", \
                        "compiler imports git"
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("git"), \
                    f"compiler imports from {module}"

    def test_compiler_does_not_import_os_or_pathlib(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    assert alias.name not in ("os", "pathlib"), \
                        f"compiler imports {alias.name}"
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert module not in ("os", "pathlib"), \
                    f"compiler imports from {module}"

    def test_compiler_does_not_write_files(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        assert "open(" not in source, "compiler should not open files"
        assert ".write(" not in source, "compiler should not write files"
        assert "Path(" not in source, "compiler should not use Path"

    def test_compiler_does_not_execute(self):
        compiler_file = SRC / "open_tulid" / "workflow" / "compiler.py"
        source = compiler_file.read_text()
        assert "subprocess" not in source
        assert "os.system" not in source
        assert "os.popen" not in source

    def test_builtins_does_not_import_open_tulid_cli(self):
        builtins_file = SRC / "open_tulid" / "workflow" / "builtins.py"
        source = builtins_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid.cli"), \
                    f"builtins imports from {module}"

    def test_builtins_does_not_import_open_tulid_vault(self):
        builtins_file = SRC / "open_tulid" / "workflow" / "builtins.py"
        source = builtins_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid.vault"), \
                    f"builtins imports from {module}"

    def test_registry_does_not_import_open_tulid_cli(self):
        registry_file = SRC / "open_tulid" / "workflow" / "registry.py"
        source = registry_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid.cli"), \
                    f"registry imports from {module}"

    def test_registry_does_not_import_open_tulid_vault(self):
        registry_file = SRC / "open_tulid" / "workflow" / "registry.py"
        source = registry_file.read_text()
        tree = pyast.parse(source)
        for node in pyast.walk(tree):
            if isinstance(node, pyast.ImportFrom):
                module = getattr(node, "module", "") or ""
                assert not module.startswith("open_tulid.vault"), \
                    f"registry imports from {module}"
