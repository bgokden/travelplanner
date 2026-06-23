"""CLI surface tests that need no network (attribution + offline artifact plan)."""

from travelplanner.cli import main
from travelplanner.models import CostLevel, Mode
from travelplanner.graph.scheduled import (
    Stop, Timetable, make_trip, save_timetable)


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


def test_plan_with_timetable_artifact_offline(tmp_path, capsys):
    # A prebuilt artifact lets 'plan' run fully offline: no auto-compose, no
    # network. Build a tiny flight network, save it, and plan over it.
    tt = Timetable()
    tt.add_stop(Stop("A", "Aport", 52.30, 4.76, tz="Europe/Amsterdam"))
    tt.add_stop(Stop("B", "Bport", 47.46, 8.55, tz="Europe/Zurich"))
    tt.add_trip(make_trip("FL", Mode.FLIGHT,
                          [("A", "10:00", "10:00"), ("B", "11:30", "11:30")],
                          cost_level=CostLevel.HIGH))
    path = str(tmp_path / "tt.json")
    save_timetable(tt, path)

    rc = main(["plan", "52.30,4.76", "47.46,8.55", "--timetable", path,
               "--at", "2026-07-01T07:00", "--top", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"artifact {path}" in out            # header names the artifact source
    assert "flight" in out.lower()              # routed over the loaded flight
    assert "OpenFlights" in out                 # artifact embeds flight data: credited
    assert "total 4h 30m" in out                # humanized, not "total 4:30:00"
    assert ".000" not in out and "00:00.5" not in out   # no leaked microseconds

    # --top-n is an accepted alias of --top (matches the top_n kwarg): identical run.
    rc2 = main(["plan", "52.30,4.76", "47.46,8.55", "--timetable", path,
                "--at", "2026-07-01T07:00", "--top-n", "2"])
    assert rc2 == 0
    assert capsys.readouterr().out == out
