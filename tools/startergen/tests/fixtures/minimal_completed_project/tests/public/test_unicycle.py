from lab_project.dynamics.unicycle import UnicycleDynamics


def test_forward_motion() -> None:
    assert UnicycleDynamics().f((0.0, 0.5), 2.0) == (2.0, 0.5)
