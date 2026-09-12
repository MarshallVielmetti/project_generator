"""A dependent pair of simulation exercises."""


class Integrator:
    """Advance a one-dimensional state by one fixed step."""

    def step(self, state: tuple[float, float], velocity: float) -> tuple[float, float]:
        """Advance ``state`` by ``velocity`` while retaining its second coordinate."""

        return (state[0] + velocity, state[1])


def rollout(
    initial: tuple[float, float], velocity: float, steps: int
) -> tuple[float, float]:
    """Apply :meth:`Integrator.step` repeatedly."""

    state = initial
    integrator = Integrator()
    for _ in range(steps):
        state = integrator.step(state, velocity)
    return state
