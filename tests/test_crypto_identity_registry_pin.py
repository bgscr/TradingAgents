"""Ticket 08 regressions for the shipped Crypto Instrument Registry pin."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.identity_registry_refresh as registry_refresh_module
import tradingagents.dataflows.market_snapshot as market_snapshot_module
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.crypto_universe import (
    CRYPTO_REFERENCE_MARKET,
    CRYPTO_REGISTRY_ID,
    SUPPORTED_CRYPTO_UNIVERSE,
)
from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    IdentityRegistryUnavailable,
    RegistryFailureReason,
    resolve_authoritative_crypto_identity,
    resolve_authoritative_instrument_identity,
)
from tradingagents.evidence import AcquisitionUnavailableReason, acquire_run_evidence

PROJECT_ROOT = Path(__file__).parents[1]
EXPECTED_REGISTRY_DIGEST = (
    "d8bd9fa6491af365384015b1e34b452d0df2a8ca5c5b0d9cb0a78908361de5d1"
)


def _standard_pin() -> tuple[Path, Path, str, str]:
    config = get_config()
    return (
        Path(config["crypto_identity_registry_path"]),
        Path(config["crypto_identity_registry_checksum_path"]),
        config["crypto_identity_registry_id"],
        config["crypto_identity_registry_sha256"],
    )


@pytest.mark.unit
def test_standard_crypto_registry_pin_is_packaged_and_coherent() -> None:
    registry_path, checksum_path, registry_id, expected_digest = _standard_pin()

    registry_bytes = registry_path.read_bytes()
    checksum_digest, checksum_name = checksum_path.read_text(encoding="utf-8").split()
    artifact = json.loads(registry_bytes)
    packaging_configuration = (PROJECT_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert registry_path.is_absolute()
    assert checksum_path.is_absolute()
    assert registry_path.parent == checksum_path.parent
    assert registry_path.name == "crypto_identity_registry.json"
    assert checksum_name == registry_path.name
    assert registry_id == artifact["registry_id"] == CRYPTO_REGISTRY_ID
    assert expected_digest == checksum_digest == EXPECTED_REGISTRY_DIGEST
    assert sha256(registry_bytes).hexdigest() == expected_digest
    assert {row["canonical_symbol"] for row in artifact["rows"]} == set(
        SUPPORTED_CRYPTO_UNIVERSE
    )
    assert '"config/crypto_identity_registry.json"' in packaging_configuration
    assert '"config/crypto_identity_registry.sha256"' in packaging_configuration


@pytest.mark.unit
def test_standard_package_pin_is_the_default_refresh_target() -> None:
    registry_path, checksum_path, _registry_id, _expected_digest = _standard_pin()

    assert registry_path == registry_refresh_module.DEFAULT_CRYPTO_REGISTRY_PATH
    assert checksum_path == registry_refresh_module.DEFAULT_CRYPTO_CHECKSUM_PATH


@pytest.mark.unit
def test_built_wheel_contains_standard_crypto_registry_pin(tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheel"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(PROJECT_ROOT),
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheel_path = next(wheel_directory.glob("tradingagents-*.whl"))

    with zipfile.ZipFile(wheel_path) as wheel:
        packaged_files = set(wheel.namelist())

    assert {
        "tradingagents/config/crypto_identity_registry.json",
        "tradingagents/config/crypto_identity_registry.sha256",
    } <= packaged_files


@pytest.mark.unit
@pytest.mark.parametrize("symbol", sorted(SUPPORTED_CRYPTO_UNIVERSE))
def test_standard_pin_resolves_every_supported_crypto_identity(symbol: str) -> None:
    _registry_path, _checksum_path, registry_id, expected_digest = _standard_pin()

    result = resolve_authoritative_crypto_identity(symbol)

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.registry_id == registry_id
    assert result.registry_sha256 == expected_digest
    assert result.identity.canonical_symbol == symbol
    assert result.identity.instrument_kind == "crypto"
    assert result.identity.venue == CRYPTO_REFERENCE_MARKET
    assert result.identity.artifact_sha256 == expected_digest


@pytest.mark.unit
def test_standard_crypto_pin_is_independent_of_process_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path, _checksum_path, _registry_id, _expected_digest = _standard_pin()
    unrelated_directory = tmp_path / "unrelated" / "working-directory"
    unrelated_directory.mkdir(parents=True)

    results = []
    for working_directory in (PROJECT_ROOT, unrelated_directory):
        monkeypatch.chdir(working_directory)
        results.append(resolve_authoritative_crypto_identity("SOL-USD"))

    assert all(isinstance(result, IdentityRegistryAvailable) for result in results)
    assert {result.registry_source_ref for result in results} == {
        str(registry_path.resolve())
    }
    assert {result.identity.canonical_symbol for result in results} == {"SOL-USD"}


@pytest.mark.unit
def test_missing_crypto_registry_fails_typed_before_market_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_path, _checksum_path, registry_id, expected_digest = _standard_pin()
    config = get_config()
    config.update(
        {
            "crypto_identity_registry_path": str(tmp_path / "missing-registry.json"),
            "crypto_identity_registry_id": registry_id,
            "crypto_identity_registry_sha256": expected_digest,
        }
    )
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    monkeypatch.setattr(
        market_snapshot_module,
        "get_authoritative_market_snapshot",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid registry pin must stop before market or model work"
        ),
    )

    evidence = acquire_run_evidence("SOL-USD", "2026-07-25")

    assert evidence.instrument_identity is None
    assert evidence.acquisition_outcomes[0].reason is (
        AcquisitionUnavailableReason.REGISTRY_UNAVAILABLE
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("registry_path", "expected_digest"),
    (
        ("standard", None),
        (None, EXPECTED_REGISTRY_DIGEST),
    ),
)
def test_partial_explicit_crypto_pin_override_fails_typed(
    registry_path: str | None,
    expected_digest: str | None,
) -> None:
    standard_registry, _checksum_path, _registry_id, _digest = _standard_pin()

    result = resolve_authoritative_crypto_identity(
        "SOL-USD",
        registry_path=standard_registry if registry_path else None,
        expected_sha256=expected_digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_registry_pin_partial_override"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("override", "missing_key", "diagnostic_code"),
    (
        (
            {"crypto_identity_registry_path": "custom-registry.json"},
            "crypto_identity_registry_sha256",
            "crypto_registry_expected_digest_missing",
        ),
        (
            {"crypto_identity_registry_sha256": "b" * 64},
            "crypto_identity_registry_path",
            "crypto_registry_path_missing",
        ),
    ),
)
def test_partial_programmatic_crypto_pin_override_does_not_mix_with_default(
    override: dict[str, str],
    missing_key: str,
    diagnostic_code: str,
) -> None:
    config_module.set_config(override)

    config = get_config()
    result = resolve_authoritative_crypto_identity("SOL-USD")

    assert config[missing_key] is None
    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == diagnostic_code


@pytest.mark.unit
def test_missing_configured_crypto_digest_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path, _checksum_path, registry_id, _expected_digest = _standard_pin()
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "crypto_identity_registry_path": str(registry_path),
            "crypto_identity_registry_id": registry_id,
            "crypto_identity_registry_sha256": None,
        },
    )

    result = resolve_authoritative_crypto_identity("SOL-USD")

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_registry_expected_digest_missing"


@pytest.mark.unit
def test_crypto_registry_digest_mismatch_fails_typed() -> None:
    registry_path, _checksum_path, _registry_id, _expected_digest = _standard_pin()

    result = resolve_authoritative_crypto_identity(
        "SOL-USD",
        registry_path=registry_path,
        expected_sha256="0" * 64,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.INTEGRITY_FAILURE
    assert result.diagnostic_code == "registry_digest_mismatch"


@pytest.mark.unit
def test_tampered_crypto_registry_bytes_fail_typed(tmp_path: Path) -> None:
    registry_path, _checksum_path, _registry_id, expected_digest = _standard_pin()
    tampered_path = tmp_path / "tampered-registry.json"
    tampered_path.write_bytes(registry_path.read_bytes() + b" ")

    result = resolve_authoritative_crypto_identity(
        "SOL-USD",
        registry_path=tampered_path,
        expected_sha256=expected_digest,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.INTEGRITY_FAILURE
    assert result.diagnostic_code == "registry_digest_mismatch"


@pytest.mark.unit
def test_crypto_registry_id_mismatch_fails_typed(tmp_path: Path) -> None:
    registry_path, _checksum_path, _registry_id, _expected_digest = _standard_pin()
    artifact = json.loads(registry_path.read_bytes())
    artifact["registry_id"] = "crypto-ccc-identity-registry-v2"
    mismatched_path = tmp_path / "wrong-registry-id.json"
    mismatched_bytes = json.dumps(
        artifact,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    mismatched_path.write_bytes(mismatched_bytes)

    result = resolve_authoritative_crypto_identity(
        "SOL-USD",
        registry_path=mismatched_path,
        expected_sha256=sha256(mismatched_bytes).hexdigest(),
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.INTEGRITY_FAILURE
    assert result.diagnostic_code == "crypto_registry_id_mismatch"


@pytest.mark.unit
def test_coherent_custom_crypto_pin_retains_precedence(tmp_path: Path) -> None:
    registry_path, _checksum_path, _registry_id, _expected_digest = _standard_pin()
    artifact = json.loads(registry_path.read_bytes())
    solana_row = next(
        row for row in artifact["rows"] if row["canonical_symbol"] == "SOL-USD"
    )
    solana_row["display_name"] = "Custom Solana USD"
    solana_row["provenance"]["source_ref"] = "registry:custom-solana"
    custom_path = tmp_path / "custom-registry.json"
    custom_bytes = json.dumps(
        artifact,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    custom_path.write_bytes(custom_bytes)
    custom_digest = sha256(custom_bytes).hexdigest()

    result = resolve_authoritative_crypto_identity(
        "SOL-USD",
        registry_path=custom_path,
        expected_sha256=custom_digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.registry_source_ref == str(custom_path.resolve())
    assert result.registry_sha256 == custom_digest
    assert result.identity.display_name == "Custom Solana USD"
    assert result.identity.provenance_source_ref == "registry:custom-solana"


@pytest.mark.unit
def test_invalid_custom_crypto_pin_is_not_rewritten(tmp_path: Path) -> None:
    registry_path, _checksum_path, _registry_id, _expected_digest = _standard_pin()
    invalid_path = tmp_path / "invalid-custom-registry.json"
    invalid_bytes = registry_path.read_bytes() + b"tampered"
    invalid_path.write_bytes(invalid_bytes)

    result = resolve_authoritative_crypto_identity(
        "SOL-USD",
        registry_path=invalid_path,
        expected_sha256=EXPECTED_REGISTRY_DIGEST,
    )

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.INTEGRITY_FAILURE
    assert invalid_path.read_bytes() == invalid_bytes


@pytest.mark.unit
def test_standard_pin_does_not_expand_support_to_uni() -> None:
    result = resolve_authoritative_crypto_identity("UNI-USD")

    assert isinstance(result, IdentityRegistryUnavailable)
    assert result.reason is RegistryFailureReason.NOT_CONFIGURED
    assert result.diagnostic_code == "crypto_symbol_not_supported"


@pytest.mark.unit
def test_standard_crypto_pin_does_not_change_mainland_registry_resolution() -> None:
    mainland_registry = PROJECT_ROOT / "config" / "instrument_identity_registry.json"
    mainland_digest = mainland_registry.with_suffix(".sha256").read_text(
        encoding="utf-8"
    ).split()[0]

    result = resolve_authoritative_instrument_identity(
        "600895.SS",
        registry_path=mainland_registry,
        expected_sha256=mainland_digest,
    )

    assert isinstance(result, IdentityRegistryAvailable)
    assert result.identity.canonical_symbol == "600895.SS"
    assert result.identity.venue == "XSHG"
    assert result.identity.instrument_kind == "equity"
