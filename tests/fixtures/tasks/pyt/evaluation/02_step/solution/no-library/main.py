import json

CHUNKS = [[1, 2], [3], [4, 5]]


def _fallback_flatten(iterable):
    return [item for group in iterable for item in group]


def _helpers():
    try:
        from pyt import flatten
    except ImportError:
        pass
    else:
        return "pyt", flatten

    try:
        from more_itertools import flatten
    except ImportError:
        return "fallback", _fallback_flatten

    return "more_itertools", flatten


if __name__ == "__main__":
    mode, flatten = _helpers()
    print(f"CUTOVER_CHECK pyt:02_step mode={mode}")
    print(json.dumps(list(flatten(CHUNKS))))
