from slugify import slugify


def test_casefold_unicode():
    assert slugify("Straße") == "strasse"


def test_collapse_underscores():
    assert slugify("a__b  c") == "a-b-c"


def test_leading_trailing_separators():
    assert slugify("--Hello--") == "hello"
