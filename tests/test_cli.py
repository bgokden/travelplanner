"""CLI surface tests that need no network (the attribution command)."""

from travelplanner.cli import main


def test_attribution_lists_open_datasets(capsys):
    rc = main(["attribution"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OpenFlights" in out
    assert "Open Database License (ODbL)" in out
    assert "Mobility Database" in out
    assert "OpenStreetMap" in out          # road data is credited too


def test_attribution_one_endpoint_asks_for_both(capsys):
    # A single endpoint cannot select feeds, so it prints the general notice and a
    # hint -- and must not try to resolve the missing destination or hit network.
    rc = main(["attribution", "Amsterdam"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "give both origin and destination" in out
    assert "OpenFlights" in out
