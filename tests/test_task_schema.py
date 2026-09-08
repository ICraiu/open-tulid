from __future__ import annotations

from open_tulid.vault.task_schema import (
    DUPLICATE_HEADING,
    TITLE_MISSING,
    parse_task_body,
    validate_task_structure,
)


def _body(*sections: str, title: str | None = "# Task") -> str:
    parts: list[str] = []
    if title:
        parts.append(title)
    parts.append("The task description.\n")
    for name, content in sections:
        parts.append(f"## {name}\n{content}\n")
    return "\n".join(parts)


class TestParseTaskBody:
    def test_extracts_title_description_and_sections(self):
        parsed = parse_task_body(_body(
            ("Why", "Because."),
            ("What", "It changes things."),
        ))
        assert parsed.title == "Task"
        assert parsed.description_lines == ("The task description.",)
        assert [section.name for section in parsed.sections] == ["Why", "What"]
        assert parsed.sections[0].content == ("Because.",)

    def test_title_optional_and_description_before_first_section(self):
        parsed = parse_task_body("Only prose.\n")
        assert parsed.title is None
        assert parsed.description_lines == ("Only prose.",)
        assert parsed.sections == ()

    def test_duplicate_headings_preserved_not_coalesced(self):
        parsed = parse_task_body(_body(
            ("Why", "First."),
            ("Why", "Second."),
        ))
        assert parsed.duplicate_heading_names == ("Why",)
        assert [section.name for section in parsed.sections] == ["Why", "Why"]

    def test_content_by_heading_merges_duplicates(self):
        parsed = parse_task_body(_body(
            ("Why", "First."),
            ("Why", "Second."),
        ))
        assert parsed.content_by_heading()["Why"] == ["First.", "Second."]


class TestValidateTaskStructure:
    def test_accepts_valid_body(self):
        assert validate_task_structure(_body(("Why", "Because."))) == ()

    def test_missing_title_reported_when_required(self):
        errors = validate_task_structure("No heading here.\n")
        assert len(errors) == 1
        assert errors[0].code == TITLE_MISSING

    def test_missing_title_not_required_when_disabled(self):
        assert validate_task_structure("No heading here.\n", require_title=False) == ()

    def test_duplicate_heading_reported(self):
        errors = validate_task_structure(_body(
            ("Why", "First."),
            ("Why", "Second."),
        ))
        assert len(errors) == 1
        assert errors[0].code == DUPLICATE_HEADING
        assert "Why" in errors[0].message

    def test_location_attached(self):
        errors = validate_task_structure(
            _body(("Why", "First."), ("Why", "Second.")),
            location="tasks/1.md",
        )
        assert errors[0].location == "tasks/1.md"

    def test_duplicate_of_required_and_optional_sections(self):
        errors = validate_task_structure(_body(
            ("Why", "First."),
            ("What", "Second."),
            ("What", "Third."),
        ))
        assert [error.code for error in errors] == [DUPLICATE_HEADING]
