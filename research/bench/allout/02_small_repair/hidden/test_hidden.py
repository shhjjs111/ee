from chunk import chunks  # pyright: ignore[reportMissingImports]


def test_tail():
    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]


def test_empty():
    assert chunks([], 3) == []


def test_negative_size():
    try:
        chunks([1], -1)
    except ValueError:
        pass
    else:
        raise AssertionError("负数应当抛 ValueError")
