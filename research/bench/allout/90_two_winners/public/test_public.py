from slugify import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_trim_and_empty():
    assert slugify("  Hello---World  ") == "hello-world"
    assert slugify("") == ""
