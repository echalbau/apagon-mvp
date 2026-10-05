from datetime import datetime

from apagon.ingest.weather import parse_payloads


def _payload(times, temps):
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "apparent_temperature": temps,
            "relative_humidity_2m": [70] * len(times),
            "precipitation": [0.0] * len(times),
            "wind_gusts_10m": [20.0] * len(times),
        }
    }


def test_parse_payloads_flags_forecast_and_skips_empty_hours():
    times = ["2026-10-05T10:00", "2026-10-05T11:00", "2026-10-05T12:00"]
    payloads = [
        _payload(times, [30.0, 31.0, 32.0]),
        {"hourly": {"time": times, "temperature_2m": [None, None, 25.0]}},  # variable faltante + horas vacías
    ]
    now = datetime(2026, 10, 5, 11, 30)
    rows = parse_payloads(payloads, ["ZUL", "MER"], now, "2026-10-05 11:30:00")

    zul = [r for r in rows if r[0] == "ZUL"]
    assert [(r[1], r[2]) for r in zul] == [
        ("2026-10-05 10:00:00", 0),
        ("2026-10-05 11:00:00", 0),
        ("2026-10-05 12:00:00", 1),
    ]
    mer = [r for r in rows if r[0] == "MER"]
    assert len(mer) == 1 and mer[0][1] == "2026-10-05 12:00:00" and mer[0][3] == 25.0 and mer[0][4] is None
