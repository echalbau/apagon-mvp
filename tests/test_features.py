from datetime import datetime, timedelta

import numpy as np

from apagon.classify import Geocoder
from apagon.features import aggregate_region_hour, build_features
from apagon.ingest.store import RawMessage, store_messages


def _report(i, author, when, text="Se fue la luz en Maracaibo"):
    return RawMessage("test", str(i), "canal", author, when, text)


def test_labels_and_target_window(conn):
    t = datetime(2026, 9, 1, 12)
    geo = Geocoder.from_db(conn)
    msgs = [_report(i, f"user{i}", t + timedelta(minutes=10 + i)) for i in range(3)]
    msgs.append(_report(99, "user0", t + timedelta(minutes=30)))  # el mismo autor no cuenta dos veces
    assert store_messages(conn, msgs, geo) == 4
    assert store_messages(conn, msgs, geo) == 0  # idempotente
    aggregate_region_hour(conn)

    row = conn.execute("SELECT n_reports, n_authors, label FROM region_hour WHERE region_id='ZUL'").fetchone()
    assert tuple(row) == (4, 3, 1)

    df = build_features(conn, t - timedelta(hours=10), t + timedelta(hours=10), horizon_h=6)
    zul = df[df["region_id"] == "ZUL"].set_index("hour")
    h = lambda k: t + timedelta(hours=k)  # noqa: E731

    assert all(zul.loc[h(k), "target"] == 1 for k in range(-6, 0))  # el evento cae en (k, k+6]
    assert zul.loc[h(-7), "target"] == 0
    assert zul.loc[h(0), "target"] == 0
    assert zul.loc[h(4), "target"] == 0
    assert np.isnan(zul.loc[h(5), "target"])  # futuro incompleto → sin etiqueta
    assert zul.loc[h(0), "hours_since_event"] == 0 and zul.loc[h(3), "hours_since_event"] == 3
    assert zul.loc[h(1), "ev_3h"] == 1 and zul.loc[h(3), "ev_3h"] == 0
    assert zul.loc[h(0), "nat_regions_now"] == 1
    assert df.loc[df["region_id"] == "DC", "target"].fillna(0).sum() == 0
