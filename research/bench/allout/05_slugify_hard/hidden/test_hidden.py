import pytest

from slugify import slugify


def test_basic_and_case():
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Mixed CASE  ") == "mixed-case"


def test_punctuation_runs():
    assert slugify("A, b!") == "a-b"
    assert slugify("a---b___c") == "a-b-c"
    assert slugify("--edge--") == "edge"


def test_underscore_and_symbols():
    assert slugify("a_b") == "a-b"
    assert slugify("C++ & Rust") == "c-rust"
    assert slugify("50% off") == "50-off"


def test_empty():
    assert slugify("") == ""
    assert slugify("   ") == ""
    assert slugify("!!!") == ""


def test_none_raises():
    with pytest.raises(ValueError):
        slugify(None)


def test_transliteration_lower():
    assert slugify("Über Straße") == "uber-strasse"
    assert slugify("smørrebrød") == "smorrebrod"


def test_transliteration_upper():
    assert slugify("ÄÖÜ") == "aou"
    assert slugify("ÆØÅ") == "aeoa"


def test_unicode_letters_kept():
    assert slugify("中文 标题") == "中文-标题"
    assert slugify("日本語 test") == "日本語-test"


def test_digits_kept():
    assert slugify("Version 1.2.3") == "version-1-2-3"


def test_truncate_at_dash():
    text = "a" * 30 + "-" + "b" * 40
    assert slugify(text) == "a" * 30


def test_truncate_hard_when_no_dash():
    text = "x" * 80
    assert len(slugify(text)) == 60
    assert slugify(text) == "x" * 60


def test_truncate_strips_trailing_dash():
    text = "y" * 59 + "-" + "z" * 10
    out = slugify(text)
    assert len(out) == 59
    assert not out.endswith("-")


def test_dash_exactly_at_limit():
    text = "w" * 59 + "-more"
    assert slugify(text) == "w" * 59
