from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256

import pytest
import requests
from typer.testing import CliRunner

from cli.main import app
from tradingagents.dataflows import identity_registry_refresh as registry_refresh


class _Response:
    def __init__(self, payload: object):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


@pytest.mark.unit
def test_identity_registry_refresh_is_one_complete_command(tmp_path, monkeypatch):
    def exchange_get(_session, url, **kwargs):
        assert url == "https://query.sse.com.cn/sseQuery/commonQuery.do"
        assert kwargs["params"]["STOCK_CODE"] == "600000"
        return _Response(
            {
                "result": [
                    {
                        "A_STOCK_CODE": "600000",
                        "STOCK_TYPE": "1",
                        "FULL_NAME": "上海浦东发展银行股份有限公司",
                        "SEC_NAME_CN": "浦发银行",
                    }
                ]
            }
        )

    test_runs: list[list[str]] = []

    def run_tests(command, **_kwargs):
        test_runs.append(command)
        return subprocess.CompletedProcess(command, 0, "3 passed", "")

    monkeypatch.setattr(requests.Session, "get", exchange_get)
    monkeypatch.setattr(subprocess, "run", run_tests)
    registry_path = tmp_path / "config" / "instrument_identity_registry.json"
    checksum_path = registry_path.with_suffix(".sha256")
    monkeypatch.setattr(
        registry_refresh,
        "DEFAULT_CHECKSUM_PATH",
        tmp_path / "must-not-use-production-default.sha256",
    )
    env_path = tmp_path / ".env.enterprise"
    env_path.write_text("EXISTING_SECRET=keep-me\n", encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_IDENTITY_REGISTRY_PATH", "stale-path")
    monkeypatch.setenv("TRADINGAGENTS_IDENTITY_REGISTRY_SHA256", "stale-digest")

    result = CliRunner().invoke(
        app,
        [
            "identity-registry-refresh",
            "600000.SS",
            "--registry-path",
            str(registry_path),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 0, result.output
    registry_bytes = registry_path.read_bytes()
    registry = json.loads(registry_bytes)
    assert registry == {
        "schema_version": "1.0",
        "registry_id": "mainland-exchange-identity-registry-v1",
        "rows": [
            {
                "canonical_symbol": "600000.SS",
                "aliases": ["600000", "600000.SH"],
                "venue": "XSHG",
                "instrument_kind": "equity",
                "currency": "CNY",
                "display_name": "上海浦东发展银行股份有限公司",
                "provenance": {
                    "provider": "Shanghai Stock Exchange",
                    "source_ref": (
                        "https://www.sse.com.cn/assortment/stock/list/info/"
                        "company/index.shtml?COMPANY_CODE=600000"
                    ),
                    "retrieved_at": registry["rows"][0]["provenance"][
                        "retrieved_at"
                    ],
                },
            }
        ],
    }
    digest = sha256(registry_bytes).hexdigest()
    assert checksum_path.read_text(encoding="utf-8") == (
        f"{digest}  instrument_identity_registry.json\n"
    )
    env_text = env_path.read_text(encoding="utf-8")
    assert "EXISTING_SECRET=keep-me" in env_text
    assert f"TRADINGAGENTS_IDENTITY_REGISTRY_PATH={registry_path.resolve()}" in env_text
    assert f"TRADINGAGENTS_IDENTITY_REGISTRY_SHA256={digest}" in env_text
    assert len(test_runs) == 1
    assert test_runs[0][1:4] == ["-m", "pytest", "-q"]
    assert "Registry refreshed" in result.output
    assert digest in result.output
    assert "Tests passed" in result.output
    assert "stale process environment overrides" in result.output
    assert "TRADINGAGENTS_IDENTITY_REGISTRY_PATH" in result.output
    assert "TRADINGAGENTS_IDENTITY_REGISTRY_SHA256" in result.output
    assert os.environ["TRADINGAGENTS_IDENTITY_REGISTRY_PATH"] == "stale-path"
    assert os.environ["TRADINGAGENTS_IDENTITY_REGISTRY_SHA256"] == "stale-digest"


@pytest.mark.unit
def test_identity_registry_refresh_merges_shenzhen_without_rewriting_prior_row(
    tmp_path,
    monkeypatch,
):
    def exchange_get(_session, url, **kwargs):
        assert url == "https://www.szse.cn/api/report/ShowReport/data"
        assert kwargs["params"]["txtDMorJC"] == "000001"
        return _Response(
            [
                {
                    "metadata": {"catalogid": "1110", "tabkey": "tab1"},
                    "data": [
                        {
                            "agdm": "000001",
                            "agjc": (
                                "<a href='/certificate/individual/index.html?code=000001'>"
                                "<u>平安银行</u></a>"
                            ),
                        }
                    ],
                    "error": None,
                }
            ]
        )

    monkeypatch.setattr(requests.Session, "get", exchange_get)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    registry_path = tmp_path / "instrument_identity_registry.json"
    prior_retrieved_at = "2026-07-22T06:49:29Z"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "registry_id": "mainland-exchange-identity-registry-v1",
                "rows": [
                    {
                        "canonical_symbol": "600895.SS",
                        "aliases": ["600895", "600895.SH"],
                        "venue": "XSHG",
                        "instrument_kind": "equity",
                        "currency": "CNY",
                        "display_name": "上海张江高科技园区开发股份有限公司",
                        "provenance": {
                            "provider": "Shanghai Stock Exchange",
                            "source_ref": (
                                "https://www.sse.com.cn/assortment/stock/list/info/"
                                "company/index.shtml?COMPANY_CODE=600895"
                            ),
                            "retrieved_at": prior_retrieved_at,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env_path = tmp_path / ".env.enterprise"

    result = CliRunner().invoke(
        app,
        [
            "identity-registry-refresh",
            "000001.SZ",
            "--registry-path",
            str(registry_path),
            "--checksum-path",
            str(registry_path.with_suffix(".sha256")),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(registry_path.read_text(encoding="utf-8"))["rows"]
    assert [row["canonical_symbol"] for row in rows] == ["000001.SZ", "600895.SS"]
    assert rows[0]["aliases"] == ["000001"]
    assert rows[0]["venue"] == "XSHE"
    assert rows[0]["instrument_kind"] == "equity"
    assert rows[0]["currency"] == "CNY"
    assert rows[0]["display_name"] == "平安银行"
    assert rows[0]["provenance"]["provider"] == "Shenzhen Stock Exchange"
    assert rows[0]["provenance"]["source_ref"] == (
        "https://www.szse.cn/certificate/individual/index.html?code=000001"
    )
    assert rows[1]["provenance"]["retrieved_at"] == prior_retrieved_at


@pytest.mark.unit
def test_identity_registry_refresh_rolls_back_every_file_when_tests_fail(
    tmp_path,
    monkeypatch,
):
    def exchange_get(_session, _url, **_kwargs):
        return _Response(
            {
                "result": [
                    {
                        "A_STOCK_CODE": "600895",
                        "STOCK_TYPE": "1",
                        "FULL_NAME": "变更后名称",
                        "SEC_NAME_CN": "变更简称",
                    }
                ]
            }
        )

    monkeypatch.setattr(requests.Session, "get", exchange_get)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "one registry test failed",
            "",
        ),
    )
    registry_path = tmp_path / "instrument_identity_registry.json"
    checksum_path = registry_path.with_suffix(".sha256")
    env_path = tmp_path / ".env.enterprise"
    original_registry = (
        '{"schema_version":"1.0","registry_id":"old","rows":['
        '{"canonical_symbol":"600895.SS","aliases":["600895","600895.SH"],'
        '"venue":"XSHG","instrument_kind":"equity","currency":"CNY",'
        '"display_name":"原名称","provenance":{"provider":"Shanghai Stock '
        'Exchange","source_ref":"https://example.test/600895",'
        '"retrieved_at":"2026-01-01T00:00:00Z"}}]}\n'
    ).encode()
    original_checksum = b"old-digest  instrument_identity_registry.json\n"
    original_env = (
        b"EXISTING_SECRET=keep-me\n"
        b"TRADINGAGENTS_IDENTITY_REGISTRY_PATH=old-path\n"
        b"TRADINGAGENTS_IDENTITY_REGISTRY_SHA256=old-digest\n"
    )
    registry_path.write_bytes(original_registry)
    checksum_path.write_bytes(original_checksum)
    env_path.write_bytes(original_env)
    monkeypatch.setenv("TRADINGAGENTS_IDENTITY_REGISTRY_PATH", "old-path")
    monkeypatch.setenv("TRADINGAGENTS_IDENTITY_REGISTRY_SHA256", "old-digest")

    result = CliRunner().invoke(
        app,
        [
            "identity-registry-refresh",
            "600895.SS",
            "--registry-path",
            str(registry_path),
            "--checksum-path",
            str(checksum_path),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 1
    assert "registry tests failed" in result.output
    assert "Changes rolled back" in result.output
    assert registry_path.read_bytes() == original_registry
    assert checksum_path.read_bytes() == original_checksum
    assert env_path.read_bytes() == original_env
    assert os.environ["TRADINGAGENTS_IDENTITY_REGISTRY_PATH"] == "old-path"
    assert os.environ["TRADINGAGENTS_IDENTITY_REGISTRY_SHA256"] == "old-digest"


@pytest.mark.unit
def test_identity_registry_refresh_rolls_back_every_file_when_interrupted(
    tmp_path,
    monkeypatch,
):
    def exchange_get(_session, _url, **_kwargs):
        return _Response(
            {
                "result": [
                    {
                        "A_STOCK_CODE": "600895",
                        "STOCK_TYPE": "1",
                        "FULL_NAME": "变更后名称",
                        "SEC_NAME_CN": "变更简称",
                    }
                ]
            }
        )

    def interrupt_tests(_command, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(requests.Session, "get", exchange_get)
    monkeypatch.setattr(subprocess, "run", interrupt_tests)
    registry_path = tmp_path / "instrument_identity_registry.json"
    checksum_path = registry_path.with_suffix(".sha256")
    env_path = tmp_path / ".env.enterprise"
    original_registry = (
        '{"schema_version":"1.0","registry_id":"old","rows":['
        '{"canonical_symbol":"600895.SS","aliases":["600895","600895.SH"],'
        '"venue":"XSHG","instrument_kind":"equity","currency":"CNY",'
        '"display_name":"原名称","provenance":{"provider":"Shanghai Stock '
        'Exchange","source_ref":"https://example.test/600895",'
        '"retrieved_at":"2026-01-01T00:00:00Z"}}]}\n'
    ).encode()
    original_checksum = b"old-digest  instrument_identity_registry.json\n"
    original_env = b"EXISTING_SECRET=keep-me\n"
    registry_path.write_bytes(original_registry)
    checksum_path.write_bytes(original_checksum)
    env_path.write_bytes(original_env)

    result = CliRunner().invoke(
        app,
        [
            "identity-registry-refresh",
            "600895.SS",
            "--registry-path",
            str(registry_path),
            "--checksum-path",
            str(checksum_path),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 130
    assert registry_path.read_bytes() == original_registry
    assert checksum_path.read_bytes() == original_checksum
    assert env_path.read_bytes() == original_env


@pytest.mark.unit
def test_identity_registry_refresh_requires_source_checkout_before_fetching(
    tmp_path,
    monkeypatch,
):
    fetch_attempts: list[str] = []

    def exchange_get(_session, url, **_kwargs):
        fetch_attempts.append(url)
        raise AssertionError("exchange fetch must not start without repository tests")

    monkeypatch.setattr(requests.Session, "get", exchange_get)
    monkeypatch.setattr(registry_refresh, "PROJECT_ROOT", tmp_path)
    registry_path = tmp_path / "output" / "instrument_identity_registry.json"
    env_path = tmp_path / "output" / ".env.enterprise"

    result = CliRunner().invoke(
        app,
        [
            "identity-registry-refresh",
            "600895.SS",
            "--registry-path",
            str(registry_path),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 1
    assert "requires a TradingAgents source checkout" in result.output
    assert fetch_attempts == []
    assert not registry_path.exists()
    assert not registry_path.with_suffix(".sha256").exists()
    assert not env_path.exists()


@pytest.mark.unit
def test_identity_registry_refresh_manifest_locates_custom_registry(
    tmp_path,
    monkeypatch,
):
    def exchange_get(_session, _url, **_kwargs):
        return _Response(
            {
                "result": [
                    {
                        "A_STOCK_CODE": "600000",
                        "STOCK_TYPE": "1",
                        "FULL_NAME": "上海浦东发展银行股份有限公司",
                        "SEC_NAME_CN": "浦发银行",
                    }
                ]
            }
        )

    monkeypatch.setattr(requests.Session, "get", exchange_get)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    registry_path = tmp_path / "config" / "instrument_identity_registry.json"
    checksum_path = tmp_path / "manifests" / "identity.sha256"
    env_path = tmp_path / ".env.enterprise"

    result = CliRunner().invoke(
        app,
        [
            "identity-registry-refresh",
            "600000.SS",
            "--registry-path",
            str(registry_path),
            "--checksum-path",
            str(checksum_path),
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 0, result.output
    digest = sha256(registry_path.read_bytes()).hexdigest()
    assert checksum_path.read_text(encoding="utf-8") == (
        f"{digest}  ../config/instrument_identity_registry.json\n"
    )
