"""Synthetic filing metadata through the actual financial tool and row binding."""
import json
from hashlib import sha256

import pytest

from tests.test_current_tushare_statements import (
    Clock,
    current_statements,
    dispatcher,
    request,
    runtime_config,
    statement_rows,
)
from tradingagents.dataflows.financial_dispatch import FinancialReportingFrequency
from tradingagents.financial_observations import bind_current_statement_observation


def dispatch_synthetic(tmp_path, monkeypatch):
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Transport:
        def query(self, endpoint, **params):
            calls.append((endpoint, params))
            return statement_rows(endpoint)

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Transport())
    clock = Clock()
    req = request("get_income_statement", frequency=FinancialReportingFrequency.ANNUAL)
    result = dispatcher(config, clock=clock, sleeper=clock.sleep).dispatch(req)
    assert len(calls) == 1 and result.artifact is not None
    quote = next(line for line in result.value.splitlines() if line.endswith(",reported"))
    observation = bind_current_statement_observation(
        raw_text=result.value, quote=quote, source_ref=result.artifact.source_ref,
        tool_call_id=req.tool_call_id, tool_name=req.tool_name,
        artifact_sha256=result.artifact.artifact_sha256, symbol="600895.SS")
    assert observation is not None
    return result, quote, observation


def test_actual_tool_keeps_reported_filing_context(tmp_path, monkeypatch):
    _, _, observation = dispatch_synthetic(tmp_path, monkeypatch)
    assert observation.model_dump(mode="json").get("filing_context") == {
        "report_type": "1", "consolidation_scope": "consolidated", "update_flag": "1"}
    assert not observation.statement_verified and not observation.decision_qualified


def rebind(result, quote, content):
    return bind_current_statement_observation(
        raw_text=content, quote=quote, source_ref=result.artifact.source_ref,
        tool_call_id=result.artifact.tool_call_id, tool_name=result.artifact.tool_name,
        artifact_sha256=sha256(content.encode()).hexdigest(), symbol="600895.SS")


def test_other_report_type_cannot_claim_cumulative_type_one_view(tmp_path, monkeypatch):
    result, quote, old = dispatch_synthetic(tmp_path, monkeypatch)
    content = result.value.replace('"report_type":"one"', '"report_type":"two"')
    assert content != result.value
    changed = rebind(result, quote, content)
    assert changed is not None and changed.value == old.value
    assert changed.filing_context is None


def project(evidence):
    from tradingagents.evidence_artifacts import (
        SourceArtifactManifest,
        SourceArtifactManifestEntry,
        project_evidence_for_audit,
    )
    entries = tuple(
        SourceArtifactManifestEntry(artifact_ref="artifact=sha256:" + a.artifact_sha256,
                                   byte_length=len(a.raw_text.encode()))
        for a in evidence.source_artifacts)
    provenance = evidence.instrument_identity.provenance
    if provenance is not None:
        # The synthetic identity has an independent logical manifest entry.
        entries += (SourceArtifactManifestEntry(
            artifact_ref="artifact=sha256:" + provenance.artifact_sha256, byte_length=1),)
    manifest = SourceArtifactManifest(artifacts=entries)
    return project_evidence_for_audit(evidence, manifest)


def test_audit_retains_only_reverified_financial_row_metadata():
    from tests.test_financial_observation_binding import submit_row
    evidence, _ = submit_row()
    assert evidence.source_facts[0].financial_observation is not None
    fact = project(evidence).source_facts[0].model_dump(mode="json")
    context = fact.get("financial_context")
    assert context is not None
    assert context["field"] == "net_income" and context["unit"] == "CNY"
    assert context["period_end"] == "2024-12-31"
    assert "filing_context" not in context
    assert "value" not in context and "raw_text" not in context


def sample():
    from tests.test_financial_observation_binding import CONTENT, ROW
    from tradingagents.financial_observations import filing_context_header
    artifact = "financial-provider-artifact:v1:" + "a" * 64
    content, quote = CONTENT.replace("a" * 64, artifact), ROW.replace("a" * 64, artifact)
    header = filing_context_header([(artifact, "1", "consolidated", "1")])
    assert header is not None
    return content, quote, header


def submitted(content, quote, statement=None):
    from tests.test_financial_observation_binding import submit_row
    return submit_row(content=content, quote=quote, statement=statement)


def with_header(content, header):
    if not header:
        return content
    first, tail = content.split("\n", 1)
    return first + "\n" + header + "\n" + tail


def test_report_and_audit_keep_scope_without_qualifying_the_claim():
    from tradingagents.evidence import decision_ready_material_claims
    content, quote, header = sample()
    evidence, update = submitted(with_header(content, header), quote)
    observation = evidence.source_facts[0].financial_observation
    assert observation.filing_context.consolidation_scope == "consolidated"
    assert "报表范围=合并" in update["fundamentals_report"]
    assert "供应商报表类型=1" in update["fundamentals_report"]
    assert project(evidence).source_facts[0].financial_context.filing_context == observation.filing_context
    assert decision_ready_material_claims(evidence) == ()


@pytest.mark.parametrize("case", [
    "absent", "duplicate_header", "duplicate_key", "scope_conflict", "other_type",
    "extra_field", "nested", "oversize", "too_many", "wrong_identity", "bad_json",
])
def test_invalid_optional_metadata_does_not_change_base_observation(case):
    from tradingagents.financial_observations import FILING_CONTEXT_PREFIX
    content, quote, header = sample()
    raw = json.loads(header.removeprefix(FILING_CONTEXT_PREFIX))
    key = next(iter(raw))
    if case == "absent":
        header = ""
    elif case == "duplicate_header":
        header += "\n" + header
    elif case == "duplicate_key":
        header = header.replace('"report_type":"one"', '"report_type":"one","report_type":"one"')
    elif case == "scope_conflict":
        header = header.replace('"consolidated"', '"parent"')
    elif case == "other_type":
        header = header.replace('"one"', '"two"')
    elif case == "extra_field":
        raw[key]["token"] = "SYNTHETIC_SECRET_SENTINEL"
        header = FILING_CONTEXT_PREFIX + json.dumps(raw)
    elif case == "nested":
        raw[key]["update_flag"] = {"token": "SYNTHETIC_SECRET_SENTINEL"}
        header = FILING_CONTEXT_PREFIX + json.dumps(raw)
    elif case == "oversize":
        header += " " * 8193
    elif case == "too_many":
        raw = {"financial-provider-artifact:v1:" + format(i, "064x"): raw[key] for i in range(9)}
        header = FILING_CONTEXT_PREFIX + json.dumps(raw)
    elif case == "wrong_identity":
        header = header.replace("a" * 64, "c" * 64)
    elif case == "bad_json":
        header = FILING_CONTEXT_PREFIX + "{"
    old, _ = submitted(content, quote)
    evidence, update = submitted(with_header(content, header), quote)
    obs = evidence.source_facts[0].financial_observation
    assert obs is not None and obs.filing_context is None
    prior = old.source_facts[0].financial_observation
    for field in ("field", "value", "unit", "period_end", "period_basis", "decision_qualified"):
        assert getattr(obs, field) == getattr(prior, field)
    assert evidence.claim_validations[0].status == old.claim_validations[0].status
    assert "报表范围=未知" in update["fundamentals_report"]
    assert "SYNTHETIC_SECRET_SENTINEL" not in project(evidence).model_dump_json()


@pytest.mark.parametrize("code,scope,flag,expected", [
    (None, "unknown", None, {"report_type": None, "consolidation_scope": "unknown", "update_flag": None}),
    ("1", "consolidated", "0", {"report_type": "1", "consolidation_scope": "consolidated", "update_flag": "0"}),
    ("6", "parent", "SYNTHETIC_SECRET_SENTINEL", {"report_type": None, "consolidation_scope": "unknown", "update_flag": None}),
])
def test_encoder_retains_only_known_codes_without_inferring_scope(code, scope, flag, expected):
    from tradingagents.financial_observations import filing_context_header
    content, quote, _ = sample()
    header = filing_context_header([("financial-provider-artifact:v1:" + "a" * 64, code, scope, flag)])
    assert "SYNTHETIC_SECRET_SENTINEL" not in header
    evidence, _ = submitted(with_header(content, header), quote)
    assert evidence.source_facts[0].financial_observation.filing_context.model_dump() == expected


def test_conflicting_metadata_is_not_last_writer_wins():
    from tradingagents.financial_observations import filing_context_header
    key = "financial-provider-artifact:v1:" + "a" * 64
    assert filing_context_header([(key, "1", "consolidated", "0"), (key, "1", "consolidated", "1")]) is None


@pytest.mark.parametrize("number", ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "123.45", "999999.25"])
def test_tool_and_inline_numeric_decisions_unchanged(number):
    from tests.test_evidence_notes import SYMBOL
    from tradingagents.evidence import (
        MaterialClaim,
        build_inline_evidence_state,
        decision_ready_material_claims,
    )
    content, quote, header = sample()
    statement = f"{SYMBOL} net income was {number} CNY."
    before, _ = submitted(content, quote, statement)
    after, _ = submitted(with_header(content, header), quote, statement)
    assert [x.status for x in after.claim_validations] == [x.status for x in before.claim_validations]
    assert decision_ready_material_claims(after) == decision_ready_material_claims(before) == ()
    claim = MaterialClaim(claim_id="fundamentals.synthetic", analyst="fundamentals",
                          statement=statement, source_refs=("fundamentals.synthetic",), source_quote=quote)
    inline_before = build_inline_evidence_state({claim.source_refs[0]: content}, [claim])
    inline_after = build_inline_evidence_state({claim.source_refs[0]: with_header(content, header)}, [claim])
    assert [x.status for x in inline_before.claim_validations] == [x.status for x in inline_after.claim_validations]
    assert [x.detail for x in inline_before.claim_validations] == [x.detail for x in inline_after.claim_validations]


def test_tampered_filing_binding_does_not_reach_audit():
    from tradingagents.financial_observations import FinancialFilingContext
    content, quote, header = sample()
    evidence, _ = submitted(with_header(content, header), quote)
    fact = evidence.source_facts[0]
    forged = fact.financial_observation.model_copy(update={"filing_context": FinancialFilingContext()})
    changed = evidence.model_copy(update={"source_facts": (fact.model_copy(update={"financial_observation": forged}),)})
    assert project(changed).source_facts[0].financial_context is None


def test_old_audit_format_still_roundtrips_without_fabricating_metadata():
    from tradingagents.evidence_artifacts import AuditEvidenceProjection
    content, quote, _ = sample()
    evidence, _ = submitted(content, quote)
    payload = project(evidence).model_dump(mode="json")
    payload["source_facts"][0].pop("financial_context")
    decoded = AuditEvidenceProjection.model_validate_json(json.dumps(payload))
    assert decoded.source_facts[0].financial_context is None
    assert "financial_context" not in decoded.model_dump(mode="json")["source_facts"][0]


@pytest.mark.parametrize("tool", ["get_balance_sheet", "get_income_statement", "get_cashflow"])
def test_real_dispatch_submission_report_and_audit_keep_context(tmp_path, monkeypatch, tool):
    import tests.test_financial_observation_binding as binding_tests
    from tradingagents.evidence import EvidenceState, verified_financial_observations
    original = binding_tests.build_analyst_update
    observed = []

    def inspect(*args, **kwargs):
        update = original(*args, **kwargs)
        evidence = EvidenceState.model_validate(update["evidence_state"])
        observations = verified_financial_observations(evidence)
        assert len(observations) == 1
        assert observations[0].filing_context.report_type == "1"
        assert "报表范围=合并" in update["fundamentals_report"]
        assert project(evidence).source_facts[0].financial_context.filing_context == observations[0].filing_context
        observed.append(tool)
        return update

    monkeypatch.setattr(binding_tests, "build_analyst_update", inspect)
    binding_tests.test_production_normalizer_renderer_envelope_and_submission_bind_units(
        tmp_path, monkeypatch, tool, None)
    assert observed == [tool]


def test_changed_presentation_rejects_old_checkpoint_but_reuses_raw_cache(tmp_path, monkeypatch):
    from tradingagents.dataflows.financial_dispatch import FinancialDispatchCheckpointError
    config = runtime_config(tmp_path)
    config["tushare_financial_https_endpoint"] = "https://api.tushare.pro"
    calls = []

    class Transport:
        def query(self, endpoint, **params):
            calls.append(endpoint)
            return statement_rows(endpoint)

    monkeypatch.setattr(current_statements, "create_financial_statement_client", lambda _: Transport())
    actual = current_statements.CurrentTushareStatements

    class PreviousPresentation(actual):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.variant_id = "current-v1:synthetic-prior-presentation"

    clock = Clock()
    req = request("get_income_statement", frequency=FinancialReportingFrequency.ANNUAL)
    monkeypatch.setattr(current_statements, "CurrentTushareStatements", PreviousPresentation)
    previous = dispatcher(config, clock=clock, sleeper=clock.sleep)
    assert previous.dispatch(req).artifact is not None
    old_ledger = previous.checkpoint_ledger()
    monkeypatch.setattr(current_statements, "CurrentTushareStatements", actual)
    with pytest.raises(FinancialDispatchCheckpointError):
        dispatcher(config, clock=clock, sleeper=clock.sleep, checkpoint_ledger=old_ledger)
    current = dispatcher(config, clock=clock, sleeper=clock.sleep)
    result = current.dispatch(req)
    assert "# financial_filing_context=" in result.value
    assert len(calls) == 1
    restored = dispatcher(config, clock=clock, sleeper=clock.sleep,
                          checkpoint_ledger=current.checkpoint_ledger())
    assert restored.dispatch(req).artifact is not None
    assert len(calls) == 1
