"""Module-level text stays in place."""


def decorator(function):
    return function


@decorator
def top_level(
    value: int,
) -> int:
    """Top-level documentation."""
    raise NotImplementedError("Exercise top-level is not implemented")


class Outer:
    """Class documentation."""

    class Inner:
        @staticmethod
        async def async_target(value: int) -> int:
            """Async documentation."""
            return value * 2


def untouched(value: int) -> int:
    # This function is intentionally not an exercise target.
    return value
