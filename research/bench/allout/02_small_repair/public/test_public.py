from chunk import chunks  # pyright: ignore[reportMissingImports]


def test_even_split():
    assert chunks([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_rejects_non_positive():
    try:
        chunks([1], 0)
    except ValueError:
        pass
    else:
        raise AssertionError("size=0 应当抛 ValueError")
