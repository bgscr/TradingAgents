from __future__ import annotations

import errno
import gzip
import json
from decimal import Decimal
from hashlib import sha256

import pytest

import tradingagents.evidence_artifacts as evidence_artifacts
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    CalculationLineage,
    CalculationReadinessDiagnostic,
    ClaimValidation,
    ClaimValidationStatus,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MarketSnapshotEvidence,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SourceFact,
)
from tradingagents.evidence_artifacts import persist_source_artifacts


@pytest.mark.unit
def test_persist_source_artifacts_is_content_addressed_deduplicated_and_idempotent(
    tmp_path,
):
    unicode_text = "市场快照：收盘价 12.34 元 🚀\n第二行保持原样"
    unicode_digest = sha256(unicode_text.encode("utf-8")).hexdigest()
    source_ref = f"acq.v1:test:{'a' * 64}"
    unicode_artifact = SourceArtifact(
        artifact_sha256=unicode_digest,
        source_ref=source_ref,
        tool_call_id="call-unicode-1",
        tool_name="get_news",
        raw_text=unicode_text,
    )
    duplicate = unicode_artifact.model_copy(
        update={"tool_call_id": "call-unicode-2"}
    )
    other_text = "second exact payload"
    other_digest = sha256(other_text.encode("utf-8")).hexdigest()
    other_artifact = SourceArtifact(
        artifact_sha256=other_digest,
        source_ref=f"acq.v1:test:{'b' * 64}",
        tool_call_id="call-other",
        tool_name="get_news",
        raw_text=other_text,
    )
    evidence = EvidenceState(
        source_artifacts=(other_artifact, unicode_artifact, duplicate),
        acquisition_outcomes=(
            SourceAcquisitionAvailable(
                provider="test-provider",
                capability="news",
                source_ref=source_ref,
                attempt=1,
                retrieved_at="2026-07-20T00:00:00+00:00",
                artifact=duplicate,
            ),
        ),
    )

    manifest = persist_source_artifacts(evidence, tmp_path)

    assert [entry.artifact_ref for entry in manifest.artifacts] == [
        f"artifact=sha256:{digest}"
        for digest in sorted((unicode_digest, other_digest))
    ]
    expected_text_by_digest = {
        unicode_digest: unicode_text,
        other_digest: other_text,
    }
    target_bytes = {}
    for entry in manifest.artifacts:
        digest = entry.artifact_ref.removeprefix("artifact=sha256:")
        target = (
            tmp_path
            / "evidence_artifacts"
            / "sha256"
            / digest[:2]
            / f"{digest}.utf8.gz"
        )
        compressed = target.read_bytes()
        raw_bytes = gzip.decompress(compressed)
        assert raw_bytes == expected_text_by_digest[digest].encode("utf-8")
        assert sha256(raw_bytes).hexdigest() == digest
        assert entry.encoding == "utf-8"
        assert entry.compression == "gzip"
        assert entry.byte_length == len(raw_bytes)
        target_bytes[target] = compressed

    assert len(manifest.artifacts) == 2
    assert persist_source_artifacts(evidence, tmp_path) == manifest
    assert {target: target.read_bytes() for target in target_bytes} == target_bytes


@pytest.mark.unit
def test_persist_source_artifacts_never_overwrites_conflicting_target(tmp_path):
    raw_text = "expected exact artifact"
    raw_bytes = raw_text.encode("utf-8")
    digest = sha256(raw_bytes).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="acq.v1:test:" + "c" * 64,
        tool_call_id="call-conflict",
        tool_name="get_news",
        raw_text=raw_text,
    )
    target = (
        tmp_path
        / "evidence_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.utf8.gz"
    )
    target.parent.mkdir(parents=True)
    original_target_bytes = gzip.compress(b"different artifact bytes", mtime=0)
    target.write_bytes(original_target_bytes)

    with pytest.raises(FileExistsError, match="conflicts"):
        persist_source_artifacts(
            EvidenceState(source_artifacts=(artifact,)),
            tmp_path,
        )

    assert target.read_bytes() == original_target_bytes


@pytest.mark.unit
def test_persist_source_artifacts_uses_exclusive_fallback_when_link_is_denied(
    tmp_path,
    monkeypatch,
):
    raw_text = "Windows fallback exact artifact"
    raw_bytes = raw_text.encode("utf-8")
    digest = sha256(raw_bytes).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="acq.v1:test:" + "d" * 64,
        tool_call_id="call-fallback",
        tool_name="get_news",
        raw_text=raw_text,
    )

    def deny_hard_link(*_args, **_kwargs):
        raise OSError(errno.EPERM, "hard links unavailable")

    monkeypatch.setattr(evidence_artifacts.os, "link", deny_hard_link)

    manifest = persist_source_artifacts(
        EvidenceState(source_artifacts=(artifact,)),
        tmp_path,
    )

    target = (
        tmp_path
        / "evidence_artifacts"
        / "sha256"
        / digest[:2]
        / f"{digest}.utf8.gz"
    )
    assert gzip.decompress(target.read_bytes()) == raw_bytes
    assert manifest.artifacts[0].artifact_ref == f"artifact=sha256:{digest}"
    assert tuple(target.parent.glob(f".{target.name}.*.tmp")) == ()


@pytest.mark.unit
def test_project_evidence_for_audit_is_safe_complete_and_permutation_invariant(
    tmp_path,
):
    from tradingagents.evidence_artifacts import project_evidence_for_audit

    unsafe_source_ref = (
        "https://secret.example/raw?token=TOP_SECRET_TOKEN&header=PRIVATE_HEADER"
    )
    identity_source_ref = "registry://PRIVATE_IDENTITY_SECRET"
    quote_one = "收盘价为 12.34 元"
    raw_one = (
        f"prefix {quote_one} suffix SECRET_MARKER_ONE "
        "Authorization: Bearer TOP_SECRET_TOKEN"
    )
    digest_one = sha256(raw_one.encode("utf-8")).hexdigest()
    artifact_one = SourceArtifact(
        artifact_sha256=digest_one,
        source_ref=unsafe_source_ref,
        tool_call_id="runtime-call-SECRET-1",
        tool_name="market_tool",
        raw_text=raw_one,
    )
    available_source_ref = "acq.v1:news:" + "e" * 64
    quote_two = "新闻情绪得分为 0.75"
    raw_two = (
        f"prefix {quote_two} suffix SECRET_MARKER_TWO "
        "https://nested.secret/header/X-Api-Key"
    )
    digest_two = sha256(raw_two.encode("utf-8")).hexdigest()
    artifact_two = SourceArtifact(
        artifact_sha256=digest_two,
        source_ref=available_source_ref,
        tool_call_id="runtime-call-SECRET-2",
        tool_name="news_tool",
        raw_text=raw_two,
    )
    lineage = CalculationLineage(
        calculation_id="close.adapter",
        calculation_version="1.0",
        input_artifact_sha256=digest_one,
        input_snapshot_id="snapshot:authoritative",
        effective_range_start="2026-07-18",
        effective_range_end="2026-07-19",
        observations_used=2,
        adjustment_basis="qfq",
        implementation_version="test-adapter-1",
        result_digest="f" * 64,
    )
    fact_one = SourceFact(
        fact_kind="canonical",
        fact_id="fact:" + "1" * 64,
        source_ref=unsafe_source_ref,
        tool_call_id="runtime-call-SECRET-1",
        tool_name="market_tool",
        artifact_sha256=digest_one,
        raw_text=quote_one,
        source_span_start=raw_one.index(quote_one),
        source_span_end=raw_one.index(quote_one) + len(quote_one),
        normalized_numeric_tokens=("12.34",),
        calculation_ids=("close.adapter",),
        canonical_field="close",
        normalized_value=Decimal("12.34"),
        unit="CNY",
        instrument_symbol="600000.SS",
        effective_date="2026-07-19",
        calculation_lineage=lineage,
    )
    fact_two = SourceFact(
        fact_kind="canonical",
        fact_id="fact:" + "2" * 64,
        source_ref=available_source_ref,
        tool_call_id="runtime-call-SECRET-2",
        tool_name="news_tool",
        artifact_sha256=digest_two,
        raw_text=quote_two,
        source_span_start=raw_two.index(quote_two),
        source_span_end=raw_two.index(quote_two) + len(quote_two),
        normalized_numeric_tokens=("0.75",),
        canonical_field="news_sentiment_score",
        normalized_value=Decimal("0.75"),
        unit="ratio",
        instrument_symbol="600000.SS",
        effective_date="2026-07-19",
    )
    claim_one = MaterialClaim(
        claim_id="claim.market.close",
        analyst="market",
        statement="SECRET claim statement one",
        source_refs=(unsafe_source_ref,),
        source_quote=quote_one,
        fact_ids=(fact_one.fact_id,),
        minimum_history_rows=2,
    )
    claim_two = MaterialClaim(
        claim_id="claim.news.sentiment",
        analyst="news",
        statement="SECRET claim statement two",
        source_refs=(available_source_ref,),
        source_quote=quote_two,
        fact_ids=(fact_two.fact_id,),
    )
    available = SourceAcquisitionAvailable(
        provider="provider-news",
        provider_order=1,
        capability="news",
        source_ref=available_source_ref,
        attempt=1,
        retrieved_at="2026-07-20T01:00:00+00:00",
        artifact=artifact_two,
    )
    unavailable_source_ref = "acq.v1:fundamentals:" + "9" * 64
    unavailable = SourceAcquisitionUnavailable(
        provider="provider-fundamentals",
        provider_order=2,
        capability="fundamentals",
        source_ref=unavailable_source_ref,
        attempt=2,
        retrieved_at="2026-07-20T01:01:00+00:00",
        retryable=True,
        reason=AcquisitionUnavailableReason.RATE_LIMITED,
        retry_after_seconds=1.5,
        http_status=429,
        calculation_readiness=CalculationReadinessDiagnostic(
            calculation_id="sma.close.200",
            required_observations=200,
            available_observations=20,
            input_artifact_sha256=digest_one,
        ),
    )
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="600000.SS",
            venue="XSHG",
            instrument_kind=InstrumentKind.EQUITY,
            currency="CNY",
            provenance=IdentityProvenance(
                provider="identity-registry",
                source_ref=identity_source_ref,
                retrieved_at="2026-07-20T00:59:00+00:00",
                artifact_sha256=digest_one,
            ),
            display_name="PRIVATE DISPLAY NAME",
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="600000.SS",
            provider="market-provider",
            retrieved_at="2026-07-20T00:58:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-07-20",
            effective_trading_date="2026-07-19",
            history_rows=200,
            frame_sha256="8" * 64,
            snapshot_id="snapshot:authoritative",
        ),
        material_claims=(claim_two, claim_one),
        source_facts=(fact_two, fact_one),
        source_artifacts=(artifact_one,),
        claim_validations=(
            ClaimValidation(
                claim_id=claim_two.claim_id,
                status=ClaimValidationStatus.SUPPORTED,
                detail="PRIVATE validation detail two",
                fact_ids=(fact_two.fact_id,),
            ),
            ClaimValidation(
                claim_id=claim_one.claim_id,
                status=ClaimValidationStatus.SUPPORTED,
                detail="PRIVATE validation detail one",
                fact_ids=(fact_one.fact_id,),
            ),
        ),
        sources=(
            EvidenceSource(
                source_id=available_source_ref,
                status=EvidenceStatus.AVAILABLE,
                required=False,
                detail="PRIVATE source detail two",
            ),
            EvidenceSource(
                source_id=unsafe_source_ref,
                status=EvidenceStatus.AVAILABLE,
                required=True,
                detail="PRIVATE source detail one",
            ),
        ),
        acquisition_outcomes=(unavailable, available),
    )
    manifest = persist_source_artifacts(evidence, tmp_path)

    projection = project_evidence_for_audit(evidence, manifest)
    serialized = json.dumps(
        projection.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    for forbidden_key in (
        "raw_text",
        "source_ref",
        "tool_call_id",
        "statement",
        "source_quote",
        "detail",
        "display_name",
    ):
        assert f'"{forbidden_key}"' not in serialized
    for forbidden_value in (
        raw_one,
        raw_two,
        quote_one,
        quote_two,
        unsafe_source_ref,
        identity_source_ref,
        "runtime-call-SECRET-1",
        "runtime-call-SECRET-2",
        claim_one.statement,
        claim_two.statement,
        "PRIVATE validation detail one",
        "PRIVATE validation detail two",
        "PRIVATE source detail one",
        "PRIVATE source detail two",
        "PRIVATE DISPLAY NAME",
        "SECRET_MARKER_ONE",
        "SECRET_MARKER_TWO",
        "TOP_SECRET_TOKEN",
        "PRIVATE_HEADER",
        "https://nested.secret/header/X-Api-Key",
    ):
        assert forbidden_value not in serialized

    expected_artifact_refs = {
        f"artifact=sha256:{digest_one}",
        f"artifact=sha256:{digest_two}",
    }
    assert {
        entry.artifact_ref for entry in projection.artifact_manifest.artifacts
    } == expected_artifact_refs
    projected_facts = {fact.fact_id: fact for fact in projection.source_facts}
    projected_fact = projected_facts[fact_one.fact_id]
    assert projected_fact.artifact_ref == f"artifact=sha256:{digest_one}"
    assert projected_fact.canonical_field == "close"
    assert projected_fact.normalized_value == Decimal("12.34")
    assert projected_fact.unit == "CNY"
    assert projected_fact.instrument_symbol == "600000.SS"
    assert projected_fact.effective_date == "2026-07-19"
    assert projected_fact.source_span_start == fact_one.source_span_start
    assert projected_fact.source_span_end == fact_one.source_span_end
    assert projected_fact.calculation_lineage == lineage
    assert projection.instrument_identity is not None
    assert projection.instrument_identity.provenance is not None
    assert projection.instrument_identity.provenance.source_id == (
        "source=sha256:" + sha256(identity_source_ref.encode("utf-8")).hexdigest()
    )
    assert projection.market_snapshot is not None
    assert projection.market_snapshot.effective_trading_date == "2026-07-19"
    assert {claim.claim_id for claim in projection.material_claims} == {
        claim_one.claim_id,
        claim_two.claim_id,
    }
    assert {validation.claim_id for validation in projection.claim_validations} == {
        claim_one.claim_id,
        claim_two.claim_id,
    }
    assert {source.source_id for source in projection.sources} == {
        "source=sha256:" + sha256(unsafe_source_ref.encode("utf-8")).hexdigest(),
        "source=sha256:" + sha256(available_source_ref.encode("utf-8")).hexdigest(),
    }
    available_projection = next(
        outcome
        for outcome in projection.acquisition_outcomes
        if outcome.outcome == "available"
    )
    assert available_projection.provider == "provider-news"
    assert available_projection.capability == "news"
    assert available_projection.provider_order == 1
    assert available_projection.attempt == 1
    assert available_projection.retrieved_at == "2026-07-20T01:00:00+00:00"
    assert available_projection.retryable is False
    assert available_projection.artifact_ref == f"artifact=sha256:{digest_two}"
    unavailable_projection = next(
        outcome
        for outcome in projection.acquisition_outcomes
        if outcome.outcome == "unavailable"
    )
    assert unavailable_projection.provider == "provider-fundamentals"
    assert unavailable_projection.capability == "fundamentals"
    assert unavailable_projection.provider_order == 2
    assert unavailable_projection.attempt == 2
    assert unavailable_projection.retrieved_at == "2026-07-20T01:01:00+00:00"
    assert unavailable_projection.retryable is True
    assert unavailable_projection.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert unavailable_projection.retry_after_seconds == 1.5
    assert unavailable_projection.http_status == 429
    assert unavailable_projection.calculation_readiness is not None
    assert unavailable_projection.calculation_readiness.required_observations == 200

    digest = projected_fact.artifact_ref.removeprefix("artifact=sha256:")
    persisted_text = gzip.decompress(
        (
            tmp_path
            / "evidence_artifacts"
            / "sha256"
            / digest[:2]
            / f"{digest}.utf8.gz"
        ).read_bytes()
    ).decode("utf-8")
    assert persisted_text[
        projected_fact.source_span_start : projected_fact.source_span_end
    ] == quote_one

    permuted = evidence.model_copy(
        update={
            "material_claims": tuple(reversed(evidence.material_claims)),
            "source_facts": tuple(reversed(evidence.source_facts)),
            "source_artifacts": tuple(reversed(evidence.source_artifacts)),
            "claim_validations": tuple(reversed(evidence.claim_validations)),
            "sources": tuple(reversed(evidence.sources)),
            "acquisition_outcomes": tuple(reversed(evidence.acquisition_outcomes)),
        }
    )
    permuted_manifest = persist_source_artifacts(permuted, tmp_path)
    permuted_projection = project_evidence_for_audit(permuted, permuted_manifest)
    assert permuted_projection.model_dump_json() == projection.model_dump_json()


@pytest.mark.unit
@pytest.mark.parametrize(
    "order",
    ((0, 1), (1, 0)),
    ids=("original-first", "conflict-first"),
)
def test_audit_projection_rejects_conflicting_facts_in_a_forged_state(
    tmp_path,
    order,
):
    from tradingagents.evidence_artifacts import project_evidence_for_audit

    raw_text = "close 12.34"
    digest = sha256(raw_text.encode("utf-8")).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="market_snapshot:600895.SS:2026-07-18",
        tool_call_id="market-call-1",
        tool_name="get_market_data",
        raw_text=raw_text,
    )
    fact_id = "fact:" + "1" * 64
    original = SourceFact(
        fact_id=fact_id,
        source_ref=artifact.source_ref,
        tool_call_id=artifact.tool_call_id,
        tool_name=artifact.tool_name,
        artifact_sha256=digest,
        raw_text=raw_text,
        source_span_start=0,
        source_span_end=len(raw_text),
    )
    conflicting = original.model_copy(update={"raw_text": "close 98.76"})
    evidence = EvidenceState(
        source_facts=(original,),
        source_artifacts=(artifact,),
    )
    manifest = persist_source_artifacts(evidence, tmp_path)
    facts = (original, conflicting)
    forged = evidence.model_copy(
        update={"source_facts": tuple(facts[index] for index in order)}
    )

    with pytest.raises(
        ValueError,
        match=f"Source fact ID {fact_id!r} was redefined\\.",
    ):
        project_evidence_for_audit(forged, manifest)


@pytest.mark.unit
@pytest.mark.parametrize(
    "arrangement",
    ((0, 1, 0), (1, 0, 1)),
    ids=("first-fact-repeated", "second-fact-repeated"),
)
def test_audit_projection_canonicalizes_exact_duplicate_facts_once(
    tmp_path,
    arrangement,
):
    from tradingagents.evidence_artifacts import project_evidence_for_audit

    raw_text = "close 12.34 | volume 500"
    digest = sha256(raw_text.encode("utf-8")).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="market_snapshot:600895.SS:2026-07-18",
        tool_call_id="market-call-1",
        tool_name="get_market_data",
        raw_text=raw_text,
    )
    excerpts = ("close 12.34", "volume 500")
    facts = tuple(
        SourceFact(
            fact_id="fact:" + str(index) * 64,
            source_ref=artifact.source_ref,
            tool_call_id=artifact.tool_call_id,
            tool_name=artifact.tool_name,
            artifact_sha256=digest,
            raw_text=excerpt,
            source_span_start=raw_text.index(excerpt),
            source_span_end=raw_text.index(excerpt) + len(excerpt),
        )
        for index, excerpt in enumerate(excerpts, start=1)
    )
    evidence = EvidenceState(
        source_facts=facts,
        source_artifacts=(artifact,),
    )
    manifest = persist_source_artifacts(evidence, tmp_path)
    forged = evidence.model_copy(
        update={
            "source_facts": tuple(facts[index] for index in arrangement),
        }
    )

    projection = project_evidence_for_audit(forged, manifest)

    assert tuple(fact.fact_id for fact in projection.source_facts) == (
        facts[0].fact_id,
        facts[1].fact_id,
    )
