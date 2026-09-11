"""Small statistics exercises."""


def mean(values: list[float]) -> float:
    """Return the arithmetic mean of a non-empty sequence."""

    return sum(values) / len(values)
