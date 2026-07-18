from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from tradingagents.picker.cache import PITCache
from tradingagents.picker.errors import PITError
from tradingagents.picker.ingestion import PITIngestor
from tradingagents.picker.pit_config import PITConfig
from tradingagents.picker.pit_dates import parse_yyyymmdd, validate_date_range
from tradingagents.picker.rate_limit import RetryPolicy, TokenBucketLimiter
from tradingagents.picker.snapshot import build_snapshot
from tradingagents.picker.tushare_provider import TushareProvider

app = typer.Typer(name="tradingagents-pit", no_args_is_help=True)


def _safe_error(exc: PITError) -> str:
    message = str(exc)
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        message = message.replace(token, "<redacted>")
    return message


def run_backfill(
    *,
    start_date: str,
    end_date: str,
    cache_dir: Path | None,
    calls_per_minute: int | None,
    refresh: bool,
) -> tuple[int, int, int, str]:
    validate_date_range(start_date, end_date)
    config = PITConfig.from_env(cache_dir, calls_per_minute)
    provider = TushareProvider.create(config)
    cache = PITCache(config.cache_dir)
    limiters: dict[str, TokenBucketLimiter] = {}

    def limiter_for(endpoint: str) -> TokenBucketLimiter:
        limiter = limiters.get(endpoint)
        if limiter is None:
            limiter = TokenBucketLimiter(config.rate_for(endpoint))
            limiters[endpoint] = limiter
        return limiter

    ingestor = PITIngestor(provider, cache, limiter_for, RetryPolicy())
    ingestor.probe()
    summary = ingestor.ingest(start_date, end_date, refresh=refresh)
    return summary.completed, summary.skipped, summary.failed, summary.run_id or ""


def snapshot_summary(*, date: str, cache_dir: Path | None) -> dict[str, object]:
    date = parse_yyyymmdd(date, "date").strftime("%Y%m%d")
    config = PITConfig.from_env(cache_dir)
    result = build_snapshot(PITCache(config.cache_dir), date)
    return {
        "as_of": result.as_of,
        "active": len(result.universe),
        "eligible": int(result.universe["eligible"].sum()),
        "coverage": result.coverage,
        "warnings": list(result.warnings),
    }


def integrity_summary(*, cache_dir: Path | None) -> dict[str, object]:
    """Run the explicit full-checksum audit for every cached partition."""
    config = PITConfig.from_env(cache_dir)
    cache = PITCache(config.cache_dir)
    try:
        failures = cache.verify_integrity()
        return {
            "partitions": len(cache.records()),
            "failed": len(failures),
            "failures": failures,
        }
    finally:
        cache.close()


@app.command()
def backfill(
    start_date: Annotated[str, typer.Option("--start-date")],
    end_date: Annotated[str, typer.Option("--end-date")],
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir")] = None,
    calls_per_minute: Annotated[
        int | None, typer.Option("--calls-per-minute")
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
) -> None:
    error: str | None = None
    try:
        completed, skipped, failed, run_id = run_backfill(
            start_date=start_date,
            end_date=end_date,
            cache_dir=cache_dir,
            calls_per_minute=calls_per_minute,
            refresh=refresh,
        )
    except PITError as exc:
        error = _safe_error(exc)
    if error is not None:
        typer.echo(f"PIT backfill failed: {error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"completed={completed} skipped={skipped} failed={failed} run_id={run_id}"
    )


@app.command()
def snapshot(
    date: Annotated[str, typer.Option("--date")],
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir")] = None,
) -> None:
    error: str | None = None
    try:
        summary = snapshot_summary(date=date, cache_dir=cache_dir)
    except PITError as exc:
        error = _safe_error(exc)
    if error is not None:
        typer.echo(f"PIT snapshot failed: {error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(summary, sort_keys=True))


@app.command()
def verify(
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir")] = None,
) -> None:
    """Hash all PIT payloads and report integrity failures."""
    error: str | None = None
    try:
        summary = integrity_summary(cache_dir=cache_dir)
    except PITError as exc:
        error = _safe_error(exc)
    if error is not None:
        typer.echo(f"PIT integrity audit failed: {error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(summary, sort_keys=True))
    if summary["failed"]:
        raise typer.Exit(code=1)
