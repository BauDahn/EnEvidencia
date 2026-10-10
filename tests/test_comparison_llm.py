from enevidencia.comparison import AGREE, DIFFER, NO_VALUE
from enevidencia.comparison.llm import UNGROUPED, ClaimGroup, compare_grouped, group_claims
from enevidencia.llm import FakeLLM
from enevidencia.models import Claim, ClaimRecord, Source, SourceType


def rec(cid, value, statement="afirmación"):
    s = Source(url=f"https://x.com/{cid}", text="t", source_type=SourceType.SECONDARY)
    return ClaimRecord(Claim(statement, "Ana", "q", 1, 1, value=value, id=cid), s)


RECS = [rec(1, "2.650 €"), rec(2, "2650€"), rec(3, "2.400 €"), rec(4, "")]


def test_valid_grouping():
    llm = FakeLLM(json_responses=[{
        "groups": [{"topic": "Renta exigida", "claim_ids": [1, 2, 3]}, {"topic": "Fecha", "claim_ids": [4]}],
        "normalized": {"1": "2650 EUR", "2": "2650 EUR", "3": "2400 EUR"}}])
    groups = group_claims(RECS, llm)
    assert [g.topic for g in groups] == ["Renta exigida", "Fecha"]
    assert groups[0].normalized == {1: "2650 EUR", 2: "2650 EUR", 3: "2400 EUR"}
    res = compare_grouped(1, RECS, groups)
    assert res[0][1].status == DIFFER and len(res[0][1].groups) == 2
    assert res[1][1].status == NO_VALUE


def test_invented_and_duplicate_ids_discarded():
    llm = FakeLLM(json_responses=[{"groups": [
        {"topic": "A", "claim_ids": [1, 99, 2]}, {"topic": "B", "claim_ids": [2, 3, "x"]},
        {"topic": "Vacío", "claim_ids": [77]}], "normalized": {"99": "z", "1": "v"}}])
    groups = group_claims(RECS, llm)
    assert [(g.topic, g.claim_ids) for g in groups] == [("A", [1, 2]), ("B", [3]), (UNGROUPED, [4])]
    assert groups[0].normalized == {1: "v"}


def test_unassigned_go_to_sin_agrupar():
    groups = group_claims(RECS, FakeLLM(json_responses=[{"groups": [{"topic": "A", "claim_ids": [1]}]}]))
    assert groups[-1].topic == UNGROUPED and groups[-1].claim_ids == [2, 3, 4]
    assert sorted(i for g in groups for i in g.claim_ids) == [1, 2, 3, 4]


def test_llm_failure_everything_ungrouped():
    groups = group_claims(RECS, FakeLLM())
    assert len(groups) == 1 and groups[0].topic == UNGROUPED and groups[0].claim_ids == [1, 2, 3, 4]
    assert group_claims([], FakeLLM()) == []


def test_normalized_value_makes_code_detect_agreement():
    recs = [rec(1, "dos mil seiscientos"), rec(2, "2.600 €")]
    g = ClaimGroup("Renta", [1, 2], {1: "2600 EUR", 2: "2600 EUR"})
    (_, c), = compare_grouped(1, recs, [g])
    assert c.status == AGREE
    assert recs[0].claim.value == "dos mil seiscientos"  # el original no se modifica
