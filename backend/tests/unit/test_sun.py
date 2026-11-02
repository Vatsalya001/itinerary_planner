from datetime import date

from app.domain.sun import sun_times


def test_bangalore_november_sunset_is_about_ten_to_six():
    sunrise, sunset = sun_times(date(2026, 11, 7), 12.9716, 77.5946, "Asia/Kolkata")
    assert abs(sunset - (17 * 60 + 50)) <= 6
    assert abs(sunrise - (6 * 60 + 10)) <= 8


def test_london_november_and_polar_night():
    _, sunset = sun_times(date(2026, 11, 7), 51.5072, -0.1276, "Europe/London")
    assert abs(sunset - (16 * 60 + 20)) <= 8
    assert sun_times(date(2026, 12, 21), 78.22, 15.65, "Arctic/Longyearbyen") is None
