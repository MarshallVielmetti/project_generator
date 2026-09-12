from mvp_project.statistics import mean


def test_mean() -> None:
    assert mean([2.0, 4.0, 6.0]) == 4.0
