from __future__ import annotations

import base64
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from hashlib import sha256

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.coordinator import (
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.models import InstrumentSpec, ProviderDatasetSpec

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
        if not publication.provider.strict_history_qualified:
            return HistoryBundleWriteResult(True, False,
                diagnostic="provider_history_not_qualified")
        with MarketHistoryStore.open(config) as store:
            prior_bundle_revision_id = None
            try:
                prior_bundle = store.latest_qualified_history_bundle(
                    publication.instrument.canonical_symbol,
                    publication.requested_as_of.isoformat(),
                    provider=publication.provider,
                    instrument=publication.instrument,
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
            prior_calendar = None
            with suppress(KeyError):
                prior_calendar = store.read_current_session_calendar(
                    calendar.reference_market
                )
            # CAL-02 payloads bind exact provider-returned membership. Keep
            # these immutable; calendar_discovery composes verified views
            # when a requested window spans several stored revisions.
            if prior_calendar is not None and not calendar.payload_contract:
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
    cache_provider: ProviderDatasetSpec | None = None,
    cache_instrument: InstrumentSpec | None = None,
    snapshot_metadata: dict | None = None,
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
    if (cache_provider is None) != (cache_instrument is None):
        return ShadowWriteResult(
            True, False, diagnostic="cache_binding_incomplete"
        )
    if cache_provider is not None and (
        not isinstance(cache_provider, ProviderDatasetSpec)
        or not isinstance(cache_instrument, InstrumentSpec)
        or cache_provider.strict_history_qualified
        or cache_provider.provider_name != provider
        or cache_instrument.canonical_symbol != instrument.yahoo_symbol
        or cache_instrument.instrument_kind != instrument.instrument_kind
        or cache_instrument.currency != "CNY"
    ):
        return ShadowWriteResult(
            True, False, diagnostic="current_only_cache_binding_invalid"
        )

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
    provider_spec = cache_provider or ProviderDatasetSpec(
        upstream_service_id=upstream_service_id,
        upstream_service_name=service_name,
        provider_dataset_id=provider_dataset_id,
        provider_name=provider,
        dataset_name="mainland-current-adjusted-v1",
        adjustment_methodology=adjustment_basis,
        strict_history_qualified=False,
    )
    instrument_spec = cache_instrument or InstrumentSpec(
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        reference_market=instrument.exchange,
        instrument_kind=instrument.instrument_kind,
        currency="CNY",
        identity_revision="mainland-routing-v1",
    )
    from tradingagents.market_history.store import (
        MarketHistoryStore,
        ProviderFramePublication,
    )

    publication = ProviderFramePublication(
        upstream_service_id=provider_spec.upstream_service_id,
        upstream_service_name=provider_spec.upstream_service_name,
        provider_dataset_id=provider_spec.provider_dataset_id,
        provider_name=provider_spec.provider_name,
        dataset_name=provider_spec.dataset_name,
        adjustment_methodology=provider_spec.adjustment_methodology,
        strict_history_qualified=False,
        instrument_id=instrument_spec.instrument_id,
        canonical_symbol=instrument_spec.canonical_symbol,
        reference_market=instrument_spec.reference_market,
        instrument_kind=instrument_spec.instrument_kind,
        currency=instrument_spec.currency,
        identity_revision=instrument_spec.identity_revision,
        snapshot_id=snapshot_id,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_trading_date=effective_trading_date,
        adjustment_basis=adjustment_basis,
        frame_digest=frame_digest,
        history_rows=history_rows,
        retrieved_at=retrieved_at,
        normalized_frame=normalized_frame_text.encode("utf-8"),
        snapshot_metadata=snapshot_metadata,
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
