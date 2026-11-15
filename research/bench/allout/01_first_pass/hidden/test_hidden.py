from shout import shout  # pyright: ignore[reportMissingImports]


def test_collapse_whitespace():
    assert shout("a\t\tb\n\rc") == "A B C"


def test_unicode_upper():
    assert shout("straße") == "STRASSE"


def test_leading_trailing_newlines():
    assert shout("\n\n  x  \n") == "X"
