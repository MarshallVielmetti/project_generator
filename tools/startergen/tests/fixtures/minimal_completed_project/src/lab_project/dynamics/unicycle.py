"""Small completed implementation used only for contract validation."""


class UnicycleDynamics:
    """Return a simple forward-motion derivative."""

    def f(self, state: tuple[float, float], speed: float) -> tuple[float, float]:
        return (speed, state[1])
