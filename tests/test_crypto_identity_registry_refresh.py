from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.main import app
from tradingagents.dataflows import identity_registry_refresh as registry_refresh
from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    IdentityRegistryUnavailable,
    RegistryFailureReason,
)

EXPECTED_SUPPORTED_CRYPTO_UNIVERSE = (
    "ADA-USD",
    "AVAX-USD",
    "BCH-USD",
    "BTC-USD",
    "DOGE-USD",
    "DOT-USD",
    "ETH-USD",
    "LINK-USD",
    "LTC-USD",
    "SOL-USD",
    "XRP-USD",
)


@pytest.mark.unit
def test_crypto_registry_refresh_cli_accepts_an_offline_candidate() -> None:
    result = CliRunner().invoke(app, ["crypto-identity-registry-refresh", "--help"])

    assert result.exit_code == 0
    assert "offline" in result.stdout.lower()
    assert "candidate" in result.stdout.lower()


@pytest.mark.unit
def test_crypto_registry_refresh_focused_gate_includes_cli_symbol_boundary() -> None:
    assert "tests/test_cli_symbol_handling.py" in (
        registry_refresh.CRYPTO_FOCUSED_TEST_PATHS
    )


def _candidate_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "canonical_symbol": "BTC-USD",
        "aliases": ["BTCUSD"],
        "venue": "CCC",
        "instrument_kind": "crypto",
        "currency": "USD",
        "display_name": "Bitcoin USD",
        "provenance": {
            "provider": "Yahoo Finance",
            "source_ref": "https://finance.yahoo.com/quote/BTC-USD/",
            "retrieved_at": "2026-07-25T00:00:00+00:00",
        },
    }
    row.update(overrides)
    return row


def _candidate(
    path: Path,
    rows: list[dict[str, object]] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "registry_id": "crypto-ccc-identity-registry-v1",
                "rows": rows if rows is not None else [_candidate_row()],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_crypto_registry_refresh_publishes_registry_checksum_and_config_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "config" / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(candidate)
    monkeypatch.setattr(registry_refresh, "_run_crypto_registry_tests", lambda **_: None)

    result = registry_refresh.refresh_crypto_identity_registry(
        candidate_path=candidate,
        registry_path=registry,
        checksum_path=checksum,
        env_path=env_file,
    )

    assert result.registry_path == registry.resolve()
    assert checksum.read_text(encoding="utf-8") == (
        f"{result.digest}  crypto_identity_registry.json\n"
    )
    env_text = env_file.read_text(encoding="utf-8")
    assert f"TRADINGAGENTS_CRYPTO_IDENTITY_REGISTRY_PATH={registry.resolve()}" in env_text
    assert f"TRADINGAGENTS_CRYPTO_IDENTITY_REGISTRY_SHA256={result.digest}" in env_text
    resolved = registry_refresh.resolve_authoritative_crypto_identity(
        "BTCUSD",
        registry_path=registry,
        expected_sha256=result.digest,
    )
    assert isinstance(resolved, IdentityRegistryAvailable)


@pytest.mark.unit
def test_crypto_registry_refresh_accepts_every_supported_member(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    rows = [
        _candidate_row(
            canonical_symbol=symbol,
            aliases=[symbol.replace("-", "")],
            display_name=f"{symbol} identity",
        )
        for symbol in EXPECTED_SUPPORTED_CRYPTO_UNIVERSE
    ]
    _candidate(candidate, rows)
    monkeypatch.setattr(registry_refresh, "_run_crypto_registry_tests", lambda **_: None)

    result = registry_refresh.refresh_crypto_identity_registry(
        candidate_path=candidate,
        registry_path=registry,
        checksum_path=checksum,
        env_path=env_file,
    )

    assert result.added == EXPECTED_SUPPORTED_CRYPTO_UNIVERSE
    for symbol in EXPECTED_SUPPORTED_CRYPTO_UNIVERSE:
        resolved = registry_refresh.resolve_authoritative_crypto_identity(
            symbol,
            registry_path=registry,
            expected_sha256=result.digest,
        )
        assert isinstance(resolved, IdentityRegistryAvailable)
        assert resolved.identity.canonical_symbol == symbol


@pytest.mark.unit
def test_crypto_registry_refresh_rejects_unsupported_member_before_publication(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(
        candidate,
        [
            _candidate_row(
                canonical_symbol="UNI-USD",
                aliases=["UNIUSD"],
            )
        ],
    )
    original_registry = b'{"old":"registry"}\n'
    original_checksum = b"old-digest  crypto_identity_registry.json\n"
    original_env = b"KEEP_ME=yes\n"
    registry.write_bytes(original_registry)
    checksum.write_bytes(original_checksum)
    env_file.write_bytes(original_env)
    verification_calls = 0

    def record_verification(**_kwargs):
        nonlocal verification_calls
        verification_calls += 1

    monkeypatch.setattr(
        registry_refresh,
        "_run_crypto_registry_tests",
        record_verification,
    )

    with pytest.raises(
        registry_refresh.RegistryRefreshError,
        match="missing_runtime_capability",
    ):
        registry_refresh.refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=registry,
            checksum_path=checksum,
            env_path=env_file,
        )

    assert verification_calls == 0
    assert registry.read_bytes() == original_registry
    assert checksum.read_bytes() == original_checksum
    assert env_file.read_bytes() == original_env


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rows", "diagnostic_code"),
    [
        (
            [_candidate_row(canonical_symbol="BTC-USDT", currency="USDT")],
            "unsupported_base_quote",
        ),
        (
            [_candidate_row(aliases=["BTC-USDT"])],
            "alias_quote_substitution",
        ),
        (
            [_candidate_row(aliases=["BTC_USD"])],
            "invalid_alias",
        ),
        (
            [_candidate_row(aliases=[{"invalid": "alias"}])],
            "invalid_alias",
        ),
        (
            [_candidate_row(), _candidate_row(aliases=[])],
            "duplicate_canonical_identity",
        ),
        (
            [_candidate_row(venue="XNAS")],
            "reference_market_not_ccc",
        ),
        (
            [_candidate_row(instrument_kind="equity")],
            "missing_runtime_capability",
        ),
        (
            [_candidate_row(aliases=["ETHUSD"])],
            "alias_base_substitution",
        ),
    ],
)
def test_crypto_registry_refresh_rejects_policy_violations_before_publication(
    tmp_path,
    monkeypatch,
    rows,
    diagnostic_code,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(candidate, rows)
    prior = {
        registry: b'{"prior":"registry"}\n',
        checksum: b"prior-digest  crypto_identity_registry.json\n",
        env_file: b"PRESERVE=exactly\r\n",
    }
    for path, content in prior.items():
        path.write_bytes(content)
    monkeypatch.setattr(
        registry_refresh,
        "_run_crypto_registry_tests",
        lambda **_: pytest.fail("verification must follow complete validation"),
    )
    monkeypatch.setattr(
        registry_refresh,
        "_atomic_write",
        lambda *_args, **_kwargs: pytest.fail(
            "candidate publication must not start after validation failure"
        ),
    )

    with pytest.raises(
        registry_refresh.RegistryRefreshError,
        match=diagnostic_code,
    ):
        registry_refresh.refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=registry,
            checksum_path=checksum,
            env_path=env_file,
        )

    assert {path: path.read_bytes() for path in prior} == prior


@pytest.mark.unit
def test_crypto_registry_refresh_rolls_back_every_file_when_verification_fails(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(candidate)
    original_registry = b'{"old":"registry"}\n'
    original_checksum = b"old-digest  crypto_identity_registry.json\n"
    original_env = b"KEEP_ME=yes\n"
    registry.write_bytes(original_registry)
    checksum.write_bytes(original_checksum)
    env_file.write_bytes(original_env)

    verification_saw_published_artifacts = False

    def fail_verification(**kwargs):
        nonlocal verification_saw_published_artifacts
        verification_registry = Path(kwargs["registry_path"])
        assert verification_registry == registry.resolve()
        assert json.loads(verification_registry.read_text(encoding="utf-8"))["rows"]
        assert registry.read_bytes() != original_registry
        assert checksum.read_bytes() != original_checksum
        assert env_file.read_bytes() != original_env
        verification_saw_published_artifacts = True
        raise RuntimeError("verification failed")

    monkeypatch.setattr(
        registry_refresh,
        "_run_crypto_registry_tests",
        fail_verification,
    )

    with pytest.raises(
        registry_refresh.RegistryRefreshError,
        match="verification failed",
    ):
        registry_refresh.refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=registry,
            checksum_path=checksum,
            env_path=env_file,
        )

    assert verification_saw_published_artifacts is True
    assert registry.read_bytes() == original_registry
    assert checksum.read_bytes() == original_checksum
    assert env_file.read_bytes() == original_env


@pytest.mark.unit
def test_crypto_registry_refresh_preserves_files_on_runtime_compatibility_failure(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(candidate)
    prior = {
        registry: b'{"prior":"registry"}\n',
        checksum: b"prior-digest  crypto_identity_registry.json\n",
        env_file: b"PRESERVE=runtime-compatibility\r\n",
    }
    for path, content in prior.items():
        path.write_bytes(content)
    monkeypatch.setattr(
        registry_refresh,
        "resolve_authoritative_crypto_identity",
        lambda *_args, **_kwargs: IdentityRegistryUnavailable(
            reason=RegistryFailureReason.MALFORMED,
            source_ref="candidate:injected",
            diagnostic_code="injected_runtime_incompatibility",
        ),
    )
    monkeypatch.setattr(
        registry_refresh,
        "_run_crypto_registry_tests",
        lambda **_: pytest.fail("published tests must not run"),
    )
    monkeypatch.setattr(
        registry_refresh,
        "_atomic_write",
        lambda *_args, **_kwargs: pytest.fail(
            "candidate publication must not start after compatibility failure"
        ),
    )

    with pytest.raises(
        registry_refresh.RegistryRefreshError,
        match="injected_runtime_incompatibility",
    ):
        registry_refresh.refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=registry,
            checksum_path=checksum,
            env_path=env_file,
        )

    assert {path: path.read_bytes() for path in prior} == prior


@pytest.mark.unit
def test_crypto_registry_refresh_restores_every_file_on_publication_failure(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(candidate)
    prior = {
        registry: b'{"prior":"registry"}\n',
        checksum: b"prior-digest  crypto_identity_registry.json\n",
        env_file: b"PRESERVE=publication-failure\r\n",
    }
    for path, content in prior.items():
        path.write_bytes(content)
    monkeypatch.setattr(registry_refresh, "_run_crypto_registry_tests", lambda **_: None)
    atomic_write = registry_refresh._atomic_write
    publication_writes = 0

    def fail_second_publication_write(path, content, mode):
        nonlocal publication_writes
        publication_writes += 1
        if publication_writes == 2:
            raise OSError("injected publication failure")
        atomic_write(path, content, mode)

    monkeypatch.setattr(
        registry_refresh,
        "_atomic_write",
        fail_second_publication_write,
    )

    with pytest.raises(
        registry_refresh.RegistryRefreshError,
        match="Changes rolled back",
    ):
        registry_refresh.refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=registry,
            checksum_path=checksum,
            env_path=env_file,
        )

    assert {path: path.read_bytes() for path in prior} == prior


@pytest.mark.unit
def test_crypto_registry_refresh_restores_every_file_on_interruption(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate.json"
    registry = tmp_path / "crypto_identity_registry.json"
    checksum = registry.with_suffix(".sha256")
    env_file = tmp_path / ".env.enterprise"
    _candidate(candidate)
    prior = {
        registry: b'{"prior":"registry"}\n',
        checksum: b"prior-digest  crypto_identity_registry.json\n",
        env_file: b"PRESERVE=interruption\r\n",
    }
    for path, content in prior.items():
        path.write_bytes(content)
    monkeypatch.setattr(registry_refresh, "_run_crypto_registry_tests", lambda **_: None)
    atomic_write = registry_refresh._atomic_write
    publication_writes = 0

    def interrupt_second_publication_write(path, content, mode):
        nonlocal publication_writes
        publication_writes += 1
        if publication_writes == 2:
            raise KeyboardInterrupt
        atomic_write(path, content, mode)

    monkeypatch.setattr(
        registry_refresh,
        "_atomic_write",
        interrupt_second_publication_write,
    )

    with pytest.raises(KeyboardInterrupt):
        registry_refresh.refresh_crypto_identity_registry(
            candidate_path=candidate,
            registry_path=registry,
            checksum_path=checksum,
            env_path=env_file,
        )

    assert {path: path.read_bytes() for path in prior} == prior
