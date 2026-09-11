"""Module-level text stays in place."""


def decorator(function):
    return function


@decorator
def top_level(
    value: int,
) -> int:
    """Top-level documentation."""
    # solution-only comment
    return value + 1


class Outer:
    """Class documentation."""

    class Inner:
        @staticmethod
        async def async_target(value: int) -> int:
            """Async documentation."""
            # solution-only comment
            return value + 1


def untouched(value: int) -> int:
    # This function is intentionally not an exercise target.
    return value
