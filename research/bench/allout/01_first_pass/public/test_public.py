from shout import shout  # pyright: ignore[reportMissingImports]


def test_basic():
    assert shout("hello world") == "HELLO WORLD"


def test_trim_and_empty():
    assert shout("  hi  ") == "HI"
    assert shout("   ") == ""
