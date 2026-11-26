from types import SimpleNamespace

from tradingagents.comparison_evidence import ComparisonRecorder


def test_recorded_comparison_requires_an_observed_artifact():
    digest = "a" * 64
    recorder = ComparisonRecorder("one observed value 5")
    record = recorder.observer("inline_source", digest)
    record("5", "5", "exact", None, True)

    evidence = recorder.finish([SimpleNamespace(artifact_sha256=digest)])
    assert evidence.status == "recorded"
    assert evidence.checks[0].artifact_sha256 == digest
    assert evidence.financial_unit_status == "unverified"
    assert recorder.finish([]).unavailable_reason == "binding_unavailable"


def test_recorder_does_not_publish_sensitive_source_text():
    recorder = ComparisonRecorder("https://example.invalid/private")
    evidence = recorder.finish([])
    assert evidence.unavailable_reason == "sensitive_input"
    assert not evidence.checks
    assert "example.invalid" not in evidence.model_dump_json()
