from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, replace
from hashlib import sha256

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.coordinator import (
    upstream_service_identity_for_provider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowWriteResult:
    attempted: bool
    persisted: bool
    frame_revision_id: str | None = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class HistoryBundleWriteResult:
    attempted: bool
    persisted: bool
    bundle_revision_id: str | None = None
    diagnostic: str | None = None


def persist_mainland_history_candidate(candidate: object) -> HistoryBundleWriteResult:
    """Publish a selected provider's complete strict-history workflow."""
    runtime = get_config()
    try:
        config = MarketHistoryConfig.from_mapping(runtime)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Market history bundle configuration is invalid: %s", exc)
        return HistoryBundleWriteResult(
            True,
            False,
            diagnostic=f"configuration_invalid:{exc}",
        )
    if config.mode is MarketHistoryMode.DISABLED:
        return HistoryBundleWriteResult(False, False)

    from tradingagents.market_history.models import SnapshotPurpose
    from tradingagents.market_history.store import MarketHistoryStore

    try:
        publication = candidate.history_bundle
        calendar = candidate.calendar
        with MarketHistoryStore.open(config) as store:
            prior_bundle_revision_id = None
            try:
                prior_bundle = store.latest_qualified_history_bundle(
                    publication.instrument.canonical_symbol,
                    publication.requested_as_of.isoformat(),
                )
            except KeyError:
                pass
            else:
                prior_bundle_revision_id = prior_bundle.bundle_revision_id
                store.reconstruct_snapshot(
                    prior_bundle.bundle_revision_id,
                    requested_date=publication.requested_as_of,
                    purpose=SnapshotPurpose.CURRENT_ANALYSIS,
                )
            try:
                prior_calendar = store.read_current_session_calendar(
                    calendar.reference_market
                )
            except KeyError:
                pass
            else:
                sessions_by_date = {
                    item.session_date: item for item in prior_calendar.sessions
                }
                sessions_by_date.update(
                    (item.session_date, item) for item in calendar.sessions
                )
                composite_payload = json.dumps(
                    {
                        "composition": "market-session-calendar-union-v1",
                        "prior_calendar_revision_id": (
                            prior_calendar.calendar_revision_id
                        ),
                        "refresh_payload_base64": base64.b64encode(
                            calendar.raw_payload
                        ).decode("ascii"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                calendar = replace(
                    calendar,
                    raw_payload=composite_payload,
                    sessions=tuple(
                        sessions_by_date[day] for day in sorted(sessions_by_date)
                    ),
                )
            store.publish_session_calendar(calendar)
            published = store.publish_history_bundle(
                publication,
                prior_bundle_revision_id=prior_bundle_revision_id,
            )
    except Exception as exc:
        logger.warning("Market history bundle write failed: %s", exc)
        return HistoryBundleWriteResult(
            True,
            False,
            diagnostic=f"history_bundle_write_failed:{exc}",
        )
    return HistoryBundleWriteResult(
        True,
        True,
        bundle_revision_id=published.bundle_revision_id,
    )


def persist_mainland_provider_frame(
    *,
    symbol: str,
    provider: str,
    adjustment_basis: str,
    requested_start: str,
    requested_end: str,
    effective_trading_date: str,
    retrieved_at: str,
    snapshot_id: str,
    frame_digest: str,
    history_rows: int,
    normalized_frame_text: str,
) -> ShadowWriteResult:
    """Fail open after persisting one accepted live frame; never reacquire data."""
    runtime = get_config()
    try:
        config = MarketHistoryConfig.from_mapping(runtime)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Market history shadow configuration is invalid: %s", exc)
        return ShadowWriteResult(True, False, diagnostic=f"configuration_invalid:{exc}")
    if config.mode is MarketHistoryMode.DISABLED:
        return ShadowWriteResult(False, False)
    instrument = resolve_mainland_instrument(symbol)
    if instrument is None:
        return ShadowWriteResult(False, False)

    upstream_service_id, service_name = upstream_service_identity_for_provider(
        provider
    )
    canonical_symbol = instrument.yahoo_symbol
    provider_dataset_id = _stable_id(
        "provider-dataset",
        provider,
        upstream_service_id,
        "mainland-current-adjusted-v1",
    )
    instrument_id = _stable_id(
        "instrument",
        canonical_symbol,
        instrument.exchange,
        instrument.instrument_kind,
        "CNY",
    )
    from tradingagents.market_history.store import (
        MarketHistoryStore,
        ProviderFramePublication,
    )

    publication = ProviderFramePublication(
        upstream_service_id=upstream_service_id,
        upstream_service_name=service_name,
        provider_dataset_id=provider_dataset_id,
        provider_name=provider,
        dataset_name="mainland-current-adjusted-v1",
        adjustment_methodology=adjustment_basis,
        strict_history_qualified=False,
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        reference_market=instrument.exchange,
        instrument_kind=instrument.instrument_kind,
        currency="CNY",
        identity_revision="mainland-routing-v1",
        snapshot_id=snapshot_id,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_trading_date=effective_trading_date,
        adjustment_basis=adjustment_basis,
        frame_digest=frame_digest,
        history_rows=history_rows,
        retrieved_at=retrieved_at,
        normalized_frame=normalized_frame_text.encode("utf-8"),
    )
    try:
        with MarketHistoryStore.open(config) as store:
            stored = store.publish_provider_frame(publication)
    except Exception as exc:
        logger.warning(
            "Market history shadow write failed for %s via %s: %s",
            canonical_symbol,
            provider,
            exc,
        )
        return ShadowWriteResult(True, False, diagnostic=f"shadow_write_failed:{exc}")
    return ShadowWriteResult(True, True, frame_revision_id=stored.frame_revision_id)


def _stable_id(namespace: str, *components: object) -> str:
    digest = sha256()
    for component in components:
        encoded = str(component).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{namespace}=sha256:{digest.hexdigest()}"
