from traffic_monitor.sources.asfinag import _guess_location


def test_guess_karawanken():
    assert _guess_location("stau vor dem karawankentunnel a11") == "Karawanken"


def test_guess_tauern():
    assert _guess_location("a10 tauernautobahn stockender verkehr") == "Tauern/A10"
