"""Smoke tests on the supplied (not synthetic) files, both suppliers."""

import json

import pytest

from app.core.planner import compute_orders
from app.datasets import DATA_DIR, DATASETS, load_dataset


@pytest.mark.parametrize("key", ["systeme", "iek"])
def test_real_partner_bundle(key):
    if not (DATA_DIR / key / DATASETS[key]["files"][0]).exists():
        pytest.skip("Partner data not present")
    ds = load_dataset(key)
    assert len(ds.metadata["sources"]) == 6
    assert len(ds.history) > 70000
    assert (ds.history.qty > 0).sum() > 60000
    assert len(ds.moq) > 500
    assert ds.showcase.code.is_unique
    r = compute_orders(ds.showcase, ds.history, ds.moq, DATASETS[key]["name"])
    assert r.lines and len(r.states) == len(ds.showcase)
    json.dumps(r.as_dict(), allow_nan=False)
    for line in r.lines:
        assert line.recommended_qty >= line.min_qty
        assert abs(line.recommended_qty / line.moq - round(line.recommended_qty / line.moq)) < 1e-6
        assert line.reason and len(line.forecast) == 4
    if key == "iek":
        assert r.as_dict()["unpriced_count"] == len(r.lines)
        assert any(line.in_transit > 0 for line in r.states)
