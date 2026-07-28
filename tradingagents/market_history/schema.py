"""Explicit transactional migrations for the Market History Database."""

SCHEMA_VERSION = 7

CREATE_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
)
"""

MIGRATION_V1 = (
    """
    CREATE TABLE upstream_services (
        upstream_service_id TEXT PRIMARY KEY,
        service_name TEXT NOT NULL,
        account_scope TEXT NOT NULL DEFAULT '',
        operator_ceiling_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (service_name, account_scope)
    )
    """,
    """
    CREATE TABLE provider_datasets (
        provider_dataset_id TEXT PRIMARY KEY,
        provider_name TEXT NOT NULL,
        upstream_service_id TEXT NOT NULL REFERENCES upstream_services(upstream_service_id),
        dataset_name TEXT NOT NULL,
        adjustment_methodology TEXT NOT NULL,
        strict_history_qualified INTEGER NOT NULL DEFAULT 0
            CHECK (strict_history_qualified IN (0, 1)),
        created_at TEXT NOT NULL,
        UNIQUE (provider_name, upstream_service_id, dataset_name, adjustment_methodology)
    )
    """,
    """
    CREATE TABLE instruments (
        instrument_id TEXT PRIMARY KEY,
        canonical_symbol TEXT NOT NULL,
        reference_market TEXT NOT NULL,
        instrument_kind TEXT NOT NULL,
        currency TEXT NOT NULL,
        listed_on TEXT,
        delisted_on TEXT,
        identity_revision TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on),
        UNIQUE (canonical_symbol, reference_market, instrument_kind)
    )
    """,
    """
    CREATE TABLE payload_artifacts (
        digest TEXT PRIMARY KEY
            CHECK (length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'),
        relative_path TEXT NOT NULL UNIQUE,
        byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
        media_type TEXT NOT NULL,
        installed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE ingestion_runs (
        run_id TEXT PRIMARY KEY,
        provider_dataset_id TEXT REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT REFERENCES instruments(instrument_id),
        operation TEXT NOT NULL CHECK (
            operation IN ('seed', 'incremental_refresh', 'reconciliation', 'calendar_refresh')
        ),
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        requested_start TEXT,
        requested_end TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'published', 'failed')),
        payload_digest TEXT REFERENCES payload_artifacts(digest),
        error_code TEXT,
        CHECK (completed_at IS NULL OR completed_at >= started_at)
    )
    """,
    """
    CREATE TABLE raw_market_observation_revisions (
        revision_id TEXT PRIMARY KEY,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        session_date TEXT NOT NULL,
        open_value TEXT NOT NULL,
        high_value TEXT NOT NULL,
        low_value TEXT NOT NULL,
        close_value TEXT NOT NULL,
        volume_value TEXT NOT NULL,
        provider_effective_at TEXT,
        provider_available_at TEXT,
        observed_at TEXT NOT NULL,
        first_observed_at TEXT NOT NULL,
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        normalization_rule_version TEXT,
        original_values_json TEXT,
        payload_digest TEXT NOT NULL REFERENCES payload_artifacts(digest),
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
        CHECK (
            (normalization_rule_version IS NULL AND original_values_json IS NULL)
            OR (normalization_rule_version IS NOT NULL AND original_values_json IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX raw_observations_by_instrument_date
    ON raw_market_observation_revisions (
        instrument_id, provider_dataset_id, session_date, observed_at
    )
    """,
    """
    CREATE TABLE trading_status_revisions (
        revision_id TEXT PRIMARY KEY,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        session_date TEXT NOT NULL,
        trading_status TEXT NOT NULL CHECK (
            trading_status IN ('traded', 'suspended', 'unknown')
        ),
        official_carried_close_value TEXT,
        volume_value TEXT,
        provider_available_at TEXT,
        observed_at TEXT NOT NULL,
        first_observed_at TEXT NOT NULL,
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        payload_digest TEXT NOT NULL REFERENCES payload_artifacts(digest),
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
        CHECK (
            trading_status != 'suspended'
            OR (official_carried_close_value IS NOT NULL AND volume_value = '0')
        )
    )
    """,
    """
    CREATE INDEX trading_status_by_instrument_date
    ON trading_status_revisions (
        instrument_id, provider_dataset_id, session_date, observed_at
    )
    """,
    """
    CREATE TABLE adjustment_factor_revisions (
        revision_id TEXT PRIMARY KEY,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        effective_date TEXT NOT NULL,
        factor_value TEXT NOT NULL,
        provider_available_at TEXT,
        observed_at TEXT NOT NULL,
        first_observed_at TEXT NOT NULL,
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        payload_digest TEXT NOT NULL REFERENCES payload_artifacts(digest),
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id)
    )
    """,
    """
    CREATE INDEX adjustment_factors_by_instrument_date
    ON adjustment_factor_revisions (
        instrument_id, provider_dataset_id, effective_date, observed_at
    )
    """,
    """
    CREATE TABLE provider_frame_revisions (
        frame_revision_id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL UNIQUE,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        requested_start TEXT NOT NULL,
        requested_end TEXT NOT NULL,
        effective_trading_date TEXT NOT NULL,
        adjustment_basis TEXT NOT NULL,
        frame_digest TEXT NOT NULL
            CHECK (length(frame_digest) = 64 AND frame_digest NOT GLOB '*[^0-9a-f]*'),
        history_rows INTEGER NOT NULL CHECK (history_rows > 0),
        payload_digest TEXT NOT NULL REFERENCES payload_artifacts(digest),
        retrieved_at TEXT NOT NULL,
        current_only INTEGER NOT NULL DEFAULT 1 CHECK (current_only = 1)
    )
    """,
    """
    CREATE INDEX provider_frames_by_instrument_date
    ON provider_frame_revisions (
        instrument_id, provider_dataset_id, effective_trading_date, retrieved_at
    )
    """,
    """
    CREATE TABLE history_bundle_revisions (
        bundle_revision_id TEXT PRIMARY KEY,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        requested_as_of TEXT NOT NULL,
        retrieval_cutoff TEXT NOT NULL,
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        observed_at TEXT NOT NULL,
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
        published_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE history_bundle_observation_revisions (
        bundle_revision_id TEXT NOT NULL
            REFERENCES history_bundle_revisions(bundle_revision_id),
        revision_id TEXT NOT NULL
            REFERENCES raw_market_observation_revisions(revision_id),
        PRIMARY KEY (bundle_revision_id, revision_id)
    )
    """,
    """
    CREATE TABLE history_bundle_trading_status_revisions (
        bundle_revision_id TEXT NOT NULL
            REFERENCES history_bundle_revisions(bundle_revision_id),
        revision_id TEXT NOT NULL REFERENCES trading_status_revisions(revision_id),
        PRIMARY KEY (bundle_revision_id, revision_id)
    )
    """,
    """
    CREATE TABLE history_bundle_adjustment_factor_revisions (
        bundle_revision_id TEXT NOT NULL
            REFERENCES history_bundle_revisions(bundle_revision_id),
        revision_id TEXT NOT NULL REFERENCES adjustment_factor_revisions(revision_id),
        PRIMARY KEY (bundle_revision_id, revision_id)
    )
    """,
    """
    CREATE TABLE instrument_provider_state (
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        seeded_at TEXT,
        latest_completed_session TEXT,
        last_refresh_at TEXT,
        last_reconciled_at TEXT,
        next_reconcile_on TEXT,
        lifecycle_state TEXT NOT NULL CHECK (
            lifecycle_state IN ('unseeded', 'active', 'degraded', 'retired')
        ),
        last_ingestion_run_id TEXT REFERENCES ingestion_runs(run_id),
        PRIMARY KEY (instrument_id, provider_dataset_id)
    )
    """,
    """
    CREATE TABLE market_session_calendars (
        calendar_revision_id TEXT PRIMARY KEY,
        reference_market TEXT NOT NULL,
        timezone_name TEXT NOT NULL,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        observed_at TEXT NOT NULL,
        first_observed_at TEXT NOT NULL,
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        payload_digest TEXT NOT NULL REFERENCES payload_artifacts(digest),
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id)
    )
    """,
    """
    CREATE TABLE market_sessions (
        calendar_revision_id TEXT NOT NULL
            REFERENCES market_session_calendars(calendar_revision_id),
        session_date TEXT NOT NULL,
        session_status TEXT NOT NULL CHECK (session_status IN ('open', 'closed')),
        opens_at TEXT,
        closes_at TEXT,
        PRIMARY KEY (calendar_revision_id, session_date),
        CHECK (
            session_status = 'open'
            OR (opens_at IS NULL AND closes_at IS NULL)
        )
    )
    """,
    """
    CREATE TABLE publication_watermarks (
        watermark_id TEXT PRIMARY KEY,
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        expected_session_date TEXT NOT NULL,
        publication_state TEXT NOT NULL CHECK (
            publication_state IN ('not_yet_published', 'available')
        ),
        observed_at TEXT NOT NULL,
        cooldown_until TEXT,
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
        CHECK (
            publication_state != 'not_yet_published' OR cooldown_until IS NOT NULL
        )
    )
    """,
    """
    CREATE INDEX publication_watermarks_by_candidate
    ON publication_watermarks (
        instrument_id, provider_dataset_id, expected_session_date, observed_at
    )
    """,
    """
    CREATE TABLE request_cooldowns (
        upstream_service_id TEXT NOT NULL REFERENCES upstream_services(upstream_service_id),
        cooldown_scope TEXT NOT NULL,
        cooldown_until TEXT NOT NULL,
        reason TEXT NOT NULL,
        retry_after_seconds REAL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (upstream_service_id, cooldown_scope)
    )
    """,
    """
    CREATE TABLE request_leases (
        request_key TEXT PRIMARY KEY,
        upstream_service_id TEXT NOT NULL REFERENCES upstream_services(upstream_service_id),
        owner_id TEXT NOT NULL,
        priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 3),
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        CHECK (expires_at > acquired_at)
    )
    """,
    """
    CREATE INDEX request_leases_by_service_expiry
    ON request_leases (upstream_service_id, expires_at)
    """,
    """
    CREATE TABLE history_store_diagnostics (
        diagnostic_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        operation TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
        code TEXT NOT NULL,
        detail TEXT NOT NULL,
        instrument_id TEXT REFERENCES instruments(instrument_id),
        provider_dataset_id TEXT REFERENCES provider_datasets(provider_dataset_id),
        ingestion_run_id TEXT REFERENCES ingestion_runs(run_id)
    )
    """,
    """
    CREATE INDEX history_diagnostics_by_time
    ON history_store_diagnostics (occurred_at, severity, code)
    """,
    """
    CREATE TABLE shadow_equivalence_results (
        equivalence_result_id TEXT PRIMARY KEY,
        scenario TEXT NOT NULL,
        frame_revision_id TEXT
            REFERENCES provider_frame_revisions(frame_revision_id),
        reconstructed_snapshot_id TEXT,
        compared_at TEXT NOT NULL,
        identity_matches INTEGER NOT NULL CHECK (identity_matches IN (0, 1)),
        provider_matches INTEGER NOT NULL CHECK (provider_matches IN (0, 1)),
        adjustment_basis_matches INTEGER NOT NULL CHECK (adjustment_basis_matches IN (0, 1)),
        effective_range_matches INTEGER NOT NULL CHECK (effective_range_matches IN (0, 1)),
        eligible_observations_match INTEGER NOT NULL CHECK (
            eligible_observations_match IN (0, 1)
        ),
        frame_digest_matches INTEGER NOT NULL CHECK (frame_digest_matches IN (0, 1)),
        derived_facts_match INTEGER NOT NULL CHECK (derived_facts_match IN (0, 1)),
        lineage_matches INTEGER NOT NULL CHECK (lineage_matches IN (0, 1)),
        passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
        detail_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE snapshot_pins (
        snapshot_id TEXT PRIMARY KEY,
        bundle_revision_id TEXT NOT NULL
            REFERENCES history_bundle_revisions(bundle_revision_id),
        instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
        provider_dataset_id TEXT NOT NULL REFERENCES provider_datasets(provider_dataset_id),
        calendar_revision_id TEXT REFERENCES market_session_calendars(calendar_revision_id),
        requested_as_of TEXT NOT NULL,
        retrieval_cutoff TEXT NOT NULL,
        adjustment_basis TEXT NOT NULL,
        derivation_version TEXT NOT NULL,
        frame_digest TEXT NOT NULL
            CHECK (length(frame_digest) = 64 AND frame_digest NOT GLOB '*[^0-9a-f]*'),
        provenance_class TEXT NOT NULL CHECK (
            provenance_class IN ('retrospective_backfill', 'observed_point_in_time')
        ),
        history_store_degraded INTEGER NOT NULL DEFAULT 0
            CHECK (history_store_degraded IN (0, 1)),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE snapshot_observation_pins (
        snapshot_id TEXT NOT NULL REFERENCES snapshot_pins(snapshot_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        session_date TEXT NOT NULL,
        observation_revision_id TEXT NOT NULL
            REFERENCES raw_market_observation_revisions(revision_id),
        trading_status_revision_id TEXT
            REFERENCES trading_status_revisions(revision_id),
        PRIMARY KEY (snapshot_id, ordinal),
        UNIQUE (snapshot_id, session_date)
    )
    """,
    """
    CREATE TABLE snapshot_factor_pins (
        snapshot_id TEXT NOT NULL REFERENCES snapshot_pins(snapshot_id),
        factor_revision_id TEXT NOT NULL
            REFERENCES adjustment_factor_revisions(revision_id),
        PRIMARY KEY (snapshot_id, factor_revision_id)
    )
    """,
)


MIGRATION_V2 = (
    """
    CREATE TABLE IF NOT EXISTS request_queue (
        request_key TEXT PRIMARY KEY,
        upstream_service_id TEXT NOT NULL REFERENCES upstream_services(upstream_service_id),
        priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 3),
        first_enqueued_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        CHECK (expires_at > first_enqueued_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS request_queue_by_service_priority
    ON request_queue (upstream_service_id, priority, first_enqueued_at)
    """,
)


MIGRATION_V3 = (
    "ALTER TABLE snapshot_pins ADD COLUMN identity_version TEXT NOT NULL DEFAULT 'v1'",
    "ALTER TABLE snapshot_pins ADD COLUMN manifest_digest TEXT",
    "ALTER TABLE snapshot_pins ADD COLUMN manifest_json TEXT",
    "ALTER TABLE snapshot_pins ADD COLUMN normalization_version TEXT",
    "ALTER TABLE snapshot_pins ADD COLUMN effective_trading_date TEXT",
    "ALTER TABLE snapshot_pins ADD COLUMN history_rows INTEGER",
)


MIGRATION_V4 = (
    """
    CREATE TABLE IF NOT EXISTS history_bundle_publication_membership_audit (
        ingestion_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
        bundle_revision_id TEXT NOT NULL
            REFERENCES history_bundle_revisions(bundle_revision_id),
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN ('observation', 'trading_status', 'adjustment_factor')
        ),
        revision_id TEXT NOT NULL,
        membership_origin TEXT NOT NULL CHECK (
            membership_origin IN ('retained', 'refreshed')
        ),
        asserted_provenance_class TEXT NOT NULL CHECK (
            asserted_provenance_class IN (
                'retrospective_backfill',
                'observed_point_in_time'
            )
        ),
        provider_available_at TEXT,
        first_observed_at TEXT NOT NULL,
        PRIMARY KEY (ingestion_run_id, revision_kind, revision_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS history_bundle_publication_audit_by_revision
    ON history_bundle_publication_membership_audit (
        revision_kind, revision_id, membership_origin
    )
    """,
)


MIGRATION_V5 = (
    """
    CREATE TABLE IF NOT EXISTS provider_request_sequences (
        sequence_id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL,
        upstream_service_id TEXT NOT NULL
            REFERENCES upstream_services(upstream_service_id),
        owner_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL CHECK (
            status IN ('running', 'succeeded', 'failed')
        ),
        final_physical_attempt_count INTEGER CHECK (
            final_physical_attempt_count IS NULL
            OR final_physical_attempt_count >= 0
        ),
        result_payload BLOB,
        result_sha256 TEXT CHECK (
            result_sha256 IS NULL
            OR (length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*')
        ),
        failure_outcome TEXT CHECK (
            failure_outcome IS NULL OR failure_outcome IN (
                'rate_limited', 'timeout', 'disconnect', 'empty_frame',
                'authentication', 'malformed_response', 'provider_error',
                'upstream_busy'
            )
        ),
        failure_retryable INTEGER CHECK (
            failure_retryable IS NULL OR failure_retryable IN (0, 1)
        ),
        failure_status_code INTEGER,
        failure_error_code TEXT,
        failure_retry_after_seconds REAL CHECK (
            failure_retry_after_seconds IS NULL
            OR failure_retry_after_seconds >= 0
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS provider_request_sequences_by_request
    ON provider_request_sequences (
        upstream_service_id, request_key, started_at DESC
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_request_attempts (
        sequence_id TEXT NOT NULL,
        attempt_index INTEGER NOT NULL CHECK (attempt_index >= 1),
        request_key TEXT NOT NULL,
        upstream_service_id TEXT NOT NULL
            REFERENCES upstream_services(upstream_service_id),
        upstream_service_name TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 3),
        operation TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        pacing_event TEXT NOT NULL CHECK (
            pacing_event IN ('permit_acquired', 'paced_then_permit_acquired')
        ),
        pacing_wait_seconds REAL NOT NULL CHECK (pacing_wait_seconds >= 0),
        outcome TEXT NOT NULL CHECK (
            outcome IN (
                'started', 'available', 'rate_limited', 'timeout', 'disconnect',
                'empty_frame', 'authentication', 'malformed_response',
                'provider_error', 'upstream_busy'
            )
        ),
        retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
        status_code INTEGER,
        error_code TEXT,
        retry_after_seconds REAL CHECK (
            retry_after_seconds IS NULL OR retry_after_seconds >= 0
        ),
        cooldown_changed INTEGER NOT NULL DEFAULT 0
            CHECK (cooldown_changed IN (0, 1)),
        cooldown_until TEXT,
        final_physical_attempt_count INTEGER CHECK (
            final_physical_attempt_count IS NULL
            OR final_physical_attempt_count >= attempt_index
        ),
        PRIMARY KEY (sequence_id, attempt_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS provider_request_attempts_by_upstream_time
    ON provider_request_attempts (upstream_service_id, attempted_at)
    """,
)


# Re-assert the cross-process handoff table in a separate migration so databases
# created by the first Ticket 10 audit migration upgrade without losing state.
MIGRATION_V6 = MIGRATION_V5[:2]


MIGRATION_V7 = (
    """
    CREATE TABLE IF NOT EXISTS provider_request_state_imports (
        source_database_path TEXT PRIMARY KEY,
        imported_at TEXT NOT NULL
    )
    """,
)
