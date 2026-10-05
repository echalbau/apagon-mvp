import pytest

from apagon.classify import Geocoder, parse_message


@pytest.fixture
def geo(conn):
    return Geocoder.from_db(conn)


@pytest.mark.parametrize(
    "text, default, expected",
    [
        ("Se fue la luz en Maracaibo", None, ("outage", "ZUL")),
        ("Volvió la luz en San Cristóbal después de 6 horas sin luz", None, ("restore", "TAC")),
        ("Bajón fuertísimo en Valencia", None, ("dip", "CAR")),
        ("Llegó la luz en Catia La Mar", None, ("restore", "LGU")),        # gana sobre "catia" (DC)
        ("Aquí en San Carlos del Zulia no hay luz", None, ("outage", "ZUL")),  # gana sobre "san carlos" (COJ)
        ("Sin luz en San Carlos", None, ("outage", "COJ")),
        ("APAGÓN EN MATURÍN", None, ("outage", "MON")),
        ("Ya hay luz en Barquisimeto", None, ("restore", "LAR")),
        ("Fluctuaciones de voltaje en Ureña", None, ("dip", "TAC")),
        ("Bajón y se fue la luz en Cabimas", None, ("outage", "ZUL")),      # el corte pesa más que el bajón
        ("No se fue la luz, gracias a Dios", "ZUL", (None, "ZUL")),
        ("¿Hay gasolina en Barquisimeto?", None, (None, "LAR")),
        ("Se fue la luz otra vez", "ZUL", ("outage", "ZUL")),               # hereda la región del canal
        ("Me pagaron 100 bolívares", None, (None, None)),
    ],
)
def test_parse_message(geo, text, default, expected):
    assert parse_message(text, geo, default) == expected
