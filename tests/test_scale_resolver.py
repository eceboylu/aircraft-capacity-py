"""
Madde 20.2/20.3 - dört ölçek resolver'ı ve cross-scale conflict
denetimi.
"""
from app.queue.domain.airport_scale import (
    ScaleCodeSet,
    find_cross_scale_conflicts,
    resolve_airport_scale,
)

MEGA = ScaleCodeSet(iata_codes=frozenset({"IST"}), icao_codes=frozenset({"LTFM"}))
LARGE = ScaleCodeSet(iata_codes=frozenset({"ESB"}), icao_codes=frozenset({"LTAC"}))
MEDIUM = ScaleCodeSet(iata_codes=frozenset({"ADB"}), icao_codes=frozenset({"LTBJ"}))
SMALL = ScaleCodeSet(iata_codes=frozenset({"ASR"}), icao_codes=frozenset({"LTAF"}))


def test_mega_airport_resolves_to_mega():
    assert resolve_airport_scale("IST", "LTFM", MEGA, LARGE, MEDIUM, SMALL) == "mega"


def test_large_airport_resolves_to_large():
    assert resolve_airport_scale("ESB", "LTAC", MEGA, LARGE, MEDIUM, SMALL) == "large"


def test_medium_airport_resolves_to_medium():
    assert resolve_airport_scale("ADB", "LTBJ", MEGA, LARGE, MEDIUM, SMALL) == "medium"


def test_small_airport_resolves_to_small():
    assert resolve_airport_scale("ASR", "LTAF", MEGA, LARGE, MEDIUM, SMALL) == "small"


def test_unmatched_airport_resolves_to_none():
    assert resolve_airport_scale("ZZZZ", "ZZZZ", MEGA, LARGE, MEDIUM, SMALL) is None


def test_icao_fallback_when_iata_missing_from_all_lists():
    unusual = ScaleCodeSet(iata_codes=frozenset(), icao_codes=frozenset({"LTFM"}))
    assert resolve_airport_scale("XXX", "LTFM", unusual, LARGE, MEDIUM, SMALL) == "mega"


def test_no_conflicts_among_disjoint_lists():
    conflicts = find_cross_scale_conflicts(MEGA, LARGE, MEDIUM, SMALL)
    assert conflicts == {"iata": {}, "icao": {}}


def test_cross_scale_iata_conflict_is_detected():
    conflicting_large = ScaleCodeSet(
        iata_codes=frozenset({"ESB", "IST"}), icao_codes=frozenset({"LTAC"})
    )
    conflicts = find_cross_scale_conflicts(MEGA, conflicting_large, MEDIUM, SMALL)
    assert "mega&large" in conflicts["iata"]
    assert conflicts["iata"]["mega&large"] == {"IST"}


def test_cross_scale_icao_conflict_is_detected():
    conflicting_medium = ScaleCodeSet(
        iata_codes=frozenset({"ADB"}), icao_codes=frozenset({"LTBJ", "LTFM"})
    )
    conflicts = find_cross_scale_conflicts(MEGA, LARGE, conflicting_medium, SMALL)
    assert "mega&medium" in conflicts["icao"]
    assert conflicts["icao"]["mega&medium"] == {"LTFM"}


def test_mega_large_medium_small_all_pairwise_conflicts_detected():
    """Her 4 tier'ın HERHANGİ ikisi arasındaki çakışma denetlenmeli."""
    shared = "DUP"
    mega = ScaleCodeSet(iata_codes=frozenset({shared}), icao_codes=frozenset())
    large = ScaleCodeSet(iata_codes=frozenset({shared}), icao_codes=frozenset())
    medium = ScaleCodeSet(iata_codes=frozenset({shared}), icao_codes=frozenset())
    small = ScaleCodeSet(iata_codes=frozenset({shared}), icao_codes=frozenset())

    conflicts = find_cross_scale_conflicts(mega, large, medium, small)
    pairs = set(conflicts["iata"].keys())
    assert pairs == {"mega&large", "mega&medium", "mega&small", "large&medium", "large&small", "medium&small"}
    assert all(codes == {shared} for codes in conflicts["iata"].values())
