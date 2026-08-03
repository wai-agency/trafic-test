from traffic_monitor.sources.nakordoni import _extract_checkpoints, _extract_wait_min


def test_extract_nested_v2_payload():
    payload = {
        "ok": True,
        "data": {
            "checkpoints": [
                {"ppid": "id_626", "name": "Maljevac", "wait_min": 149, "queue": 14},
            ]
        },
    }
    cps = _extract_checkpoints(payload)
    assert len(cps) == 1
    assert cps[0]["name"] == "Maljevac"
    assert _extract_wait_min(cps[0]) == 149
