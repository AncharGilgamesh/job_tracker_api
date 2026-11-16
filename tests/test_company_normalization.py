"""Pure company-name normalization."""

import ast
from pathlib import Path

import pytest

from job_tracker.services import companies
from job_tracker.services.companies import (
    InvalidCompanyNameError,
    normalize_company_name,
)


@pytest.mark.parametrize(
    ("raw", "display_name", "normalized_name"),
    [
        ("  Acme   Corporation ", "Acme Corporation", "acme corporation"),
        ("Acme\t\n  Corporation", "Acme Corporation", "acme corporation"),
        ("  Smith & Sons, LLC. ", "Smith & Sons, LLC.", "smith & sons, llc."),
        ("AcMe LLC", "AcMe LLC", "acme llc"),
        ("Straße", "Straße", "strasse"),
        ("\ufb01rm", "firm", "firm"),
        ("Ａｃｍｅ", "Acme", "acme"),
        ("\u212a", "K", "k"),
        ("ς", "ς", "σ"),
    ],
)
def test_normalize_company_name(
    raw: str,
    display_name: str,
    normalized_name: str,
) -> None:
    result = normalize_company_name(raw)
    assert result.name == display_name
    assert result.normalized_name == normalized_name
    assert normalize_company_name(result.name) == result


def test_repeated_whitespace_does_not_change_matching() -> None:
    spaced = normalize_company_name("  Acme    Corporation  ")
    compact = normalize_company_name("Acme Corporation")
    assert spaced == compact


def test_mixed_case_names_compare_case_insensitively() -> None:
    display = normalize_company_name("AcMe Corporation")
    folded = normalize_company_name("acme corporation")
    assert display.normalized_name == folded.normalized_name
    assert display.name == "AcMe Corporation"


def test_normalized_name_uses_unicode_casefold() -> None:
    germany = normalize_company_name("Straße")
    assert germany.normalized_name == "strasse"
    assert "Straße".lower() != germany.normalized_name
    sigma = normalize_company_name("ς")
    assert sigma.name == "ς"
    assert sigma.normalized_name == "σ"
    assert "ς".lower() != sigma.normalized_name


@pytest.mark.parametrize(
    "raw", ["", " ", "   ", "\t\n", "\u00a0", "\u3000", " \u00a0\t "]
)
def test_empty_name_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidCompanyNameError) as exc_info:
        normalize_company_name(raw)
    assert str(exc_info.value) == "A company name is required"


@pytest.mark.parametrize("raw", [None, 12, b"Acme"])
def test_non_string_name_is_rejected(raw: object) -> None:
    with pytest.raises(TypeError) as exc_info:
        normalize_company_name(raw)  # type: ignore[arg-type]
    assert str(exc_info.value) == "Company name must be a string."


def test_company_service_stays_independent_of_http_and_normalization_libraries() -> (
    None
):
    source = Path(companies.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    allowed = {"dataclasses", "datetime", "unicodedata", "uuid"}
    for name in imported:
        root = name.split(".", maxsplit=1)[0]
        assert root in allowed or root in {"sqlalchemy", "job_tracker"}
        assert not name.startswith("fastapi")
        assert not name.startswith("job_tracker.api")
    assert "session.begin(" not in source
