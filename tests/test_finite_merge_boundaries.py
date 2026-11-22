from pathlib import Path
from unittest.mock import patch
import pytest
from tradingagents import reporting
from tradingagents.market_history import current_cache as cache
from tradingagents.dataflows import finite_disclosure_cache as finite

def test_invalid_terminal_before_storage(tmp_path):
    target=tmp_path/'unpublished'
    with patch.object(reporting,'ReportStore',side_effect=AssertionError('unexpected storage')) as store:
        with pytest.raises(finite.a.Rejected):
            reporting.write_report_tree({},'601328.SH',target,config={'finite_disclosure_profile':finite.PROFILE})
    store.assert_not_called()
    assert not target.exists()

def test_ordinary_cannot_select_finite():
    with patch.object(cache,'get_config',return_value={}):
        assert 'ordinary_qualification_scope' in cache.compatibility_diagnostic(manifest_path=cache.MANIFEST_PATH.with_name('current_cache_compatibility_finite_v2.json'))

def test_finite_only_its_qualification():
    with patch.object(cache,'get_config',return_value={'finite_disclosure_profile':finite.PROFILE}),patch.object(cache._qualification,'compatibility_diagnostic',return_value='scoped') as q:
        assert cache.compatibility_diagnostic()=='scoped'
        assert q.call_args.kwargs['manifest_path'].name=='current_cache_compatibility_finite_v2.json'
        assert 'finite_qualification_scope' in cache.compatibility_diagnostic(manifest_path=cache.MANIFEST_PATH)

def test_archived_grant_unavailable():
    with pytest.raises(finite.a.Rejected,match='finite_release_authority_unconfigured'):finite.installed_profile()

def test_ordinary_fingerprint_validation(tmp_path,monkeypatch):
    import json
    from hashlib import sha256
    monkeypatch.setattr(cache,"get_config",lambda:{})
    monkeypatch.setattr(cache,"SOURCE_ROOT",tmp_path)
    monkeypatch.setattr(cache,"MANIFEST_PATH",tmp_path/"current.json")
    for name in cache.SOURCE_FILES+cache.VERIFICATION_FILES:
        p=tmp_path/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(b"# synthetic\n")
    sources=cache.source_fingerprints()
    details={name:dict(live="fixture",restored="fixture",lineage="fixture",rows=21) for name in cache.SCENARIOS}
    matrix={name:dict(passed=True,evidence_sha256=cache.digest(v)) for name,v in details.items()}
    scope="isolated-synthetic-current-cache-only"
    report=dict(matrix=matrix,details=details,verification_sources={name:sources[name] for name in cache.VERIFICATION_FILES},scope=scope,mechanism_phase="direct-codec-before-manifest")
    manifest=dict(contract=cache.MANIFEST_CONTRACT,mechanism=cache.CONTRACT,sources=sources,matrix=matrix,matrix_sha256=cache.digest(matrix),details=details,evidence=dict(path="sample",sha256=cache.digest(report),scope=scope))
    cache.MANIFEST_PATH.write_text(json.dumps(manifest))
    assert cache.compatibility_diagnostic() is None
    (tmp_path/cache.SOURCE_FILES[0]).write_text("changed")
    assert "source_mismatch" in cache.compatibility_diagnostic()
