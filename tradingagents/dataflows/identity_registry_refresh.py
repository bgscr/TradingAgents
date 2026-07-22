"""Refresh the digest-pinned instrument identity registry from exchanges."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
from pathlib import Path
from typing import Any

import requests

from tradingagents.dataflows.instrument_identity import (
    IdentityRegistryAvailable,
    resolve_authoritative_instrument_identity,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "instrument_identity_registry.json"
DEFAULT_CHECKSUM_PATH = DEFAULT_REGISTRY_PATH.with_suffix(".sha256")
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env.enterprise"

REGISTRY_ID = "mainland-exchange-identity-registry-v1"
REGISTRY_PATH_ENV = "TRADINGAGENTS_IDENTITY_REGISTRY_PATH"
REGISTRY_SHA256_ENV = "TRADINGAGENTS_IDENTITY_REGISTRY_SHA256"
FOCUSED_TEST_PATHS = (
    "tests/test_production_instrument_identity_registry.py",
    "tests/test_authoritative_instrument_identity.py",
    "tests/test_env_overrides.py",
)

_SSE_ENDPOINT = "https://query.sse.com.cn/sseQuery/commonQuery.do"
_SSE_COMPANY_PAGE = (
    "https://www.sse.com.cn/assortment/stock/list/info/company/"
    "index.shtml?COMPANY_CODE={code}"
)
_SZSE_ENDPOINT = "https://www.szse.cn/api/report/ShowReport/data"
_SZSE_COMPANY_PAGE = (
    "https://www.szse.cn/certificate/individual/index.html?code={code}"
)
_EXPLICIT_MAINLAND_SYMBOL = re.compile(r"^(?P<code>\d{6})\.(?P<venue>SS|SH|SZ)$")
_HTML_TAG = re.compile(r"<[^>]+>")


class RegistryRefreshError(RuntimeError):
    """The registry refresh could not complete without violating its contract."""


@dataclass(frozen=True)
class RegistryRefreshResult:
    registry_path: Path
    checksum_path: Path
    env_path: Path
    digest: str
    added: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]


@dataclass(frozen=True)
class _FileBackup:
    content: bytes | None
    mode: int | None


def refresh_identity_registry(
    symbols: list[str],
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    checksum_path: Path = DEFAULT_CHECKSUM_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
    timeout_seconds: float = 20.0,
    full_tests: bool = False,
) -> RegistryRefreshResult:
    """Fetch, merge, pin, configure, and verify authoritative identities."""
    canonical_symbols = _canonical_symbols(symbols)
    _require_source_checkout()
    registry_path = registry_path.resolve()
    checksum_path = checksum_path.resolve()
    env_path = env_path.resolve()
    existing = _load_registry(registry_path)
    rows_by_symbol = {
        str(row.get("canonical_symbol", "")): row for row in existing["rows"]
    }

    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    session = requests.Session()
    session.trust_env = False
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []

    for symbol in canonical_symbols:
        fetched = _fetch_exchange_identity(
            session,
            symbol,
            retrieved_at=retrieved_at,
            timeout_seconds=timeout_seconds,
        )
        prior = rows_by_symbol.get(symbol)
        if prior is None:
            rows_by_symbol[symbol] = fetched
            added.append(symbol)
        elif _identity_without_retrieval_time(prior) == _identity_without_retrieval_time(
            fetched
        ):
            unchanged.append(symbol)
        else:
            rows_by_symbol[symbol] = fetched
            updated.append(symbol)

    artifact = {
        "schema_version": "1.0",
        "registry_id": REGISTRY_ID,
        "rows": [rows_by_symbol[key] for key in sorted(rows_by_symbol)],
    }
    registry_bytes = (
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    digest = sha256(registry_bytes).hexdigest()
    _validate_candidate(registry_bytes, digest, artifact["rows"])

    try:
        registry_reference = Path(
            os.path.relpath(registry_path, start=checksum_path.parent)
        ).as_posix()
    except ValueError:
        registry_reference = registry_path.as_posix()
    checksum_bytes = f"{digest}  {registry_reference}\n".encode()
    env_bytes = _updated_env_bytes(
        env_path.read_bytes() if env_path.exists() else b"",
        registry_path=registry_path,
        digest=digest,
    )
    targets = (registry_path, checksum_path, env_path)
    backups = {path: _backup(path) for path in targets}

    try:
        _atomic_write(registry_path, registry_bytes, backups[registry_path].mode)
        _atomic_write(checksum_path, checksum_bytes, backups[checksum_path].mode)
        _atomic_write(env_path, env_bytes, backups[env_path].mode)
        _run_registry_tests(
            registry_path=registry_path,
            digest=digest,
            full_tests=full_tests,
        )
    except BaseException as exc:
        rollback_errors: list[str] = []
        for path in reversed(targets):
            try:
                _restore(path, backups[path])
            except OSError as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RegistryRefreshError(
                f"{exc}\nRollback incomplete: {'; '.join(rollback_errors)}"
            ) from exc
        if isinstance(exc, Exception):
            raise RegistryRefreshError(f"{exc}\nChanges rolled back.") from exc
        raise

    return RegistryRefreshResult(
        registry_path=registry_path,
        checksum_path=checksum_path,
        env_path=env_path,
        digest=digest,
        added=tuple(added),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
    )


def _canonical_symbols(symbols: list[str]) -> tuple[str, ...]:
    canonical: set[str] = set()
    for raw_symbol in symbols:
        normalized = str(raw_symbol).strip().upper()
        match = _EXPLICIT_MAINLAND_SYMBOL.fullmatch(normalized)
        if match is None:
            raise RegistryRefreshError(
                f"unsupported symbol {raw_symbol!r}; use an explicit "
                "six-digit .SS, .SH, or .SZ symbol"
            )
        code = match.group("code")
        venue = match.group("venue")
        canonical.add(f"{code}.{'SS' if venue in {'SS', 'SH'} else 'SZ'}")
    if not canonical:
        raise RegistryRefreshError("at least one symbol is required")
    return tuple(sorted(canonical))


def _require_source_checkout() -> None:
    required_paths = ("pyproject.toml", *FOCUSED_TEST_PATHS)
    missing = [relative for relative in required_paths if not (PROJECT_ROOT / relative).is_file()]
    if missing:
        raise RegistryRefreshError(
            "identity-registry-refresh requires a TradingAgents source checkout "
            "containing its test suite; run it from an editable/source install "
            f"(missing: {', '.join(missing)})"
        )


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "registry_id": REGISTRY_ID, "rows": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryRefreshError(f"cannot read existing registry {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise RegistryRefreshError(f"existing registry {path} has no rows list")
    return payload


def _fetch_exchange_identity(
    session: requests.Session,
    symbol: str,
    *,
    retrieved_at: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if symbol.endswith(".SS"):
        return _fetch_sse_identity(
            session,
            symbol,
            retrieved_at=retrieved_at,
            timeout_seconds=timeout_seconds,
        )
    return _fetch_szse_identity(
        session,
        symbol,
        retrieved_at=retrieved_at,
        timeout_seconds=timeout_seconds,
    )


def _fetch_sse_identity(
    session: requests.Session,
    symbol: str,
    *,
    retrieved_at: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    code = symbol.removesuffix(".SS")
    params = {
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "STOCK_TYPE": "1",
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": code,
        "COMPANY_STATUS": "2,4,5,7,8",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "10",
        "pageHelp.pageNo": "1",
    }
    try:
        response = session.get(
            _SSE_ENDPOINT,
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": "https://www.sse.com.cn/assortment/stock/list/",
                "User-Agent": "TradingAgents identity registry refresh/1.0",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RegistryRefreshError(f"SSE lookup failed for {symbol}: {exc}") from exc
    rows = payload.get("result") if isinstance(payload, dict) else None
    matches = [
        row
        for row in rows or ()
        if isinstance(row, dict)
        and str(row.get("A_STOCK_CODE", "")).strip() == code
        and str(row.get("STOCK_TYPE", "")).strip() == "1"
    ]
    if len(matches) != 1:
        raise RegistryRefreshError(
            f"SSE returned {len(matches)} authoritative A-share rows for {symbol}"
        )
    row = matches[0]
    display_name = str(row.get("FULL_NAME") or row.get("SEC_NAME_CN") or "").strip()
    if not display_name:
        raise RegistryRefreshError(f"SSE returned no company name for {symbol}")
    return {
        "canonical_symbol": symbol,
        "aliases": [code, f"{code}.SH"],
        "venue": "XSHG",
        "instrument_kind": "equity",
        "currency": "CNY",
        "display_name": display_name,
        "provenance": {
            "provider": "Shanghai Stock Exchange",
            "source_ref": _SSE_COMPANY_PAGE.format(code=code),
            "retrieved_at": retrieved_at,
        },
    }


def _fetch_szse_identity(
    session: requests.Session,
    symbol: str,
    *,
    retrieved_at: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    code = symbol.removesuffix(".SZ")
    params = {
        "SHOWTYPE": "JSON",
        "CATALOGID": "1110",
        "TABKEY": "tab1",
        "PAGENO": "1",
        "txtDMorJC": code,
    }
    try:
        response = session.get(
            _SZSE_ENDPOINT,
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": "https://www.szse.cn/market/product/stock/list/index.html",
                "User-Agent": "TradingAgents identity registry refresh/1.0",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RegistryRefreshError(f"SZSE lookup failed for {symbol}: {exc}") from exc
    reports = payload if isinstance(payload, list) else ()
    rows = [
        row
        for report in reports
        if isinstance(report, dict) and isinstance(report.get("data"), list)
        for row in report["data"]
        if isinstance(row, dict)
    ]
    matches = [row for row in rows if str(row.get("agdm", "")).strip() == code]
    if len(matches) != 1:
        raise RegistryRefreshError(
            f"SZSE returned {len(matches)} authoritative A-share rows for {symbol}"
        )
    raw_name = str(matches[0].get("agjc") or "")
    display_name = unescape(_HTML_TAG.sub("", raw_name)).strip()
    if not display_name:
        raise RegistryRefreshError(f"SZSE returned no security name for {symbol}")
    return {
        "canonical_symbol": symbol,
        "aliases": [code],
        "venue": "XSHE",
        "instrument_kind": "equity",
        "currency": "CNY",
        "display_name": display_name,
        "provenance": {
            "provider": "Shenzhen Stock Exchange",
            "source_ref": _SZSE_COMPANY_PAGE.format(code=code),
            "retrieved_at": retrieved_at,
        },
    }


def _identity_without_retrieval_time(row: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(row, ensure_ascii=False))
    provenance = normalized.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("retrieved_at", None)
    return normalized


def _validate_candidate(
    registry_bytes: bytes,
    digest: str,
    rows: list[dict[str, Any]],
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as handle:
            handle.write(registry_bytes)
            temporary_path = Path(handle.name)
        for row in rows:
            symbol = str(row.get("canonical_symbol", ""))
            result = resolve_authoritative_instrument_identity(
                symbol,
                registry_path=temporary_path,
                expected_sha256=digest,
            )
            if not isinstance(result, IdentityRegistryAvailable):
                raise RegistryRefreshError(
                    f"generated registry does not resolve {symbol}: "
                    f"{result.diagnostic_code}"
                )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _updated_env_bytes(
    existing: bytes,
    *,
    registry_path: Path,
    digest: str,
) -> bytes:
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryRefreshError("environment file must be UTF-8") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    values = {
        REGISTRY_PATH_ENV: str(registry_path),
        REGISTRY_SHA256_ENV: digest,
    }
    found: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        replacement = None
        for key, value in values.items():
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                replacement = f"{key}={value}"
                found.add(key)
                break
        rendered.append(replacement if replacement is not None else line)
    missing = [key for key in values if key not in found]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Digest-pinned authoritative instrument identity registry.")
        rendered.extend(f"{key}={values[key]}" for key in missing)
    return (newline.join(rendered) + newline).encode("utf-8")


def _backup(path: Path) -> _FileBackup:
    if not path.exists():
        return _FileBackup(content=None, mode=None)
    return _FileBackup(content=path.read_bytes(), mode=path.stat().st_mode)


def _atomic_write(path: Path, content: bytes, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _restore(path: Path, backup: _FileBackup) -> None:
    if backup.content is None:
        path.unlink(missing_ok=True)
        return
    _atomic_write(path, backup.content, backup.mode)


def _run_registry_tests(
    *,
    registry_path: Path,
    digest: str,
    full_tests: bool,
) -> None:
    command = [sys.executable, "-m", "pytest", "-q"]
    if not full_tests:
        command.extend(FOCUSED_TEST_PATHS)
    environment = os.environ.copy()
    environment[REGISTRY_PATH_ENV] = str(registry_path)
    environment[REGISTRY_SHA256_ENV] = digest
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RegistryRefreshError(f"could not start registry tests: {exc}") from exc
    if completed.returncode != 0:
        details = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        if len(details) > 4000:
            details = details[-4000:]
        raise RegistryRefreshError(
            "registry tests failed" + (f":\n{details}" if details else "")
        )
