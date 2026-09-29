from collections.abc import Iterable
from typing import TypeVar

import yaml

T = TypeVar("T")


def chunked(iterable: Iterable[T], size: int) -> list[list[T]]:
    if size <= 0:
        raise ValueError("size must be positive")
    chunk: list[T] = []
    result: list[list[T]] = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) == size:
            result.append(chunk)
            chunk = []
    if chunk:
        result.append(chunk)
    return result


def flatten(iterable: Iterable[Iterable[T]]) -> list[T]:
    return [item for group in iterable for item in group]


def load_numbers(document: str) -> list[int]:
    data = yaml.safe_load(document)
    values = data["numbers"]
    return [int(value) for value in values]
