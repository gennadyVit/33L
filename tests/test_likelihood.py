"""Boundary tests for the runway 33L likelihood rule."""
from function_app import likelihood_of_33l


def test_high_likelihood_core():
    assert likelihood_of_33l(330) == ("High", 100)
    assert likelihood_of_33l(290) == ("High", 100)
    assert likelihood_of_33l(360) == ("High", 100)
    assert likelihood_of_33l(0) == ("High", 100)


def test_possible_near_boundary():
    # Within 10 degrees of 290 -> 80%
    assert likelihood_of_33l(285) == ("Possible", 80)
    assert likelihood_of_33l(280) == ("Possible", 80)


def test_possible_mid_range():
    # 10-20 degrees away -> 65%
    assert likelihood_of_33l(275) == ("Possible", 65)


def test_possible_far_edge():
    # 20-25 degrees away -> 50%
    assert likelihood_of_33l(265) == ("Possible", 50)


def test_unlikely_south():
    cat, pct = likelihood_of_33l(180)
    assert cat == "Unlikely"
    assert pct is None


def test_unlikely_east():
    cat, pct = likelihood_of_33l(90)
    assert cat == "Unlikely"
    assert pct is None