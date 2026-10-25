from apps.oa.approvals.sources.travel_evidence import (
    CandidateObservation,
    TravelEvidence,
)


def test_partial_target_facts_survive_when_another_candidate_supplies_downloads():
    partial = CandidateObservation(text="FLOW partial", scan_status="read_failed")
    complete = CandidateObservation(
        text="FLOW complete",
        scan_status="resources_found",
        download_targets=(
            {"id": "7", "downloadUrl": "https://trip.example.test/opera/getFile/7/a"},
        ),
    )
    result = TravelEvidence.assemble(
        [partial, complete],
        fields=[],
        request_id="12",
        flow_mark="FLOW",
        expected_resource_count=None,
    )
    assert result.errors == ()
    assert [fact["value"] for fact in result.facts] == ["FLOW partial", "FLOW complete"]
    assert result.download_targets[0]["source_request_id"] == "12"
    assert result.attachment_scan_record() is None
