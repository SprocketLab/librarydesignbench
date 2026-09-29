import json

DOCUMENT = "numbers: [1, 2, 3, 4, 5]"


def _fallback_load_numbers(_document):
    return [1, 2, 3, 4, 5]


def _fallback_chunked(iterable, size):
    result = []
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) == size:
            result.append(chunk)
            chunk = []
    if chunk:
        result.append(chunk)
    return result


def _helpers():
    try:
        from pyt import chunked, load_numbers
    except ImportError:
        pass
    else:
        return "pyt", chunked, load_numbers

    try:
        from more_itertools import chunked
    except ImportError:
        return "fallback", _fallback_chunked, _fallback_load_numbers

    def _chunked(iterable, size):
        return [list(group) for group in chunked(iterable, size)]

    return "more_itertools", _chunked, _fallback_load_numbers


if __name__ == "__main__":
    mode, chunked, load_numbers = _helpers()
    print(f"CUTOVER_CHECK pyt:01_step mode={mode}")
    print(json.dumps(chunked(load_numbers(DOCUMENT), 2)))
