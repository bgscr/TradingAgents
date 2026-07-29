import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")
_PROJECT_RUNTIME_ROOT = os.getenv("TRADINGAGENTS_PROJECT_ROOT")
_DEFAULT_RESULTS_DIR = (
    os.path.join(_PROJECT_RUNTIME_ROOT, "logs")
    if _PROJECT_RUNTIME_ROOT
    else os.path.join(_TRADINGAGENTS_HOME, "logs")
)
_DEFAULT_CACHE_DIR = (
    os.path.join(_PROJECT_RUNTIME_ROOT, "data", "cache")
    if _PROJECT_RUNTIME_ROOT
    else os.path.join(_TRADINGAGENTS_HOME, "cache")
)
_DEFAULT_MEMORY_LOG_PATH = (
    os.path.join(
        _PROJECT_RUNTIME_ROOT,
        "data",
        "memory",
        "trading_memory.md",
    )
    if _PROJECT_RUNTIME_ROOT
    else os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")
)
_DEFAULT_MARKET_HISTORY_ROOT = (
    os.path.join(_PROJECT_RUNTIME_ROOT, "data", "market_history")
    if _PROJECT_RUNTIME_ROOT
    else os.path.join(_TRADINGAGENTS_HOME, "market_history")
)
_PACKAGE_CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "config"))
_DEFAULT_CRYPTO_REGISTRY_PATH = os.path.join(
    _PACKAGE_CONFIG_DIR,
    "crypto_identity_registry.json",
)
_DEFAULT_CRYPTO_CHECKSUM_PATH = os.path.join(
    _PACKAGE_CONFIG_DIR,
    "crypto_identity_registry.sha256",
)
_DEFAULT_CRYPTO_REGISTRY_ID = "crypto-ccc-identity-registry-v1"


def _read_default_crypto_registry_sha256() -> str | None:
    try:
        with open(_DEFAULT_CRYPTO_CHECKSUM_PATH, encoding="utf-8") as checksum:
            fields = checksum.read().split()
    except OSError:
        return None
    return fields[0] if fields else None


_DEFAULT_CRYPTO_REGISTRY_SHA256 = _read_default_crypto_registry_sha256()

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_EVIDENCE_GATE_MODE":   "evidence_gate_mode",
    "TRADINGAGENTS_IDENTITY_REGISTRY_PATH": "instrument_identity_registry_path",
    "TRADINGAGENTS_IDENTITY_REGISTRY_SHA256": "instrument_identity_registry_sha256",
    "TRADINGAGENTS_CRYPTO_IDENTITY_REGISTRY_PATH": "crypto_identity_registry_path",
    "TRADINGAGENTS_CRYPTO_IDENTITY_REGISTRY_SHA256": "crypto_identity_registry_sha256",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    "TRADINGAGENTS_MARKET_HISTORY_MODE":  "market_history_mode",
    "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH": "market_history_database_path",
    "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT": "market_history_payload_root",
    "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT": "market_history_backup_root",
    "TRADINGAGENTS_DATA_USAGE_MODE":      "data_usage_mode",
    "TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_MODE": "mainland_capability_routing_mode",
    "TRADINGAGENTS_TUSHARE_ENABLED_CAPABILITIES": "tushare_enabled_capabilities",
    "TRADINGAGENTS_TUSHARE_QUALIFICATION_PROFILE": "tushare_qualification_profile",
    "TRADINGAGENTS_TUSHARE_ACCOUNT_SCOPE_LABEL": "tushare_account_scope_label",
    "TRADINGAGENTS_TUSHARE_CALLS_PER_MINUTE": "tushare_calls_per_minute",
    "TRADINGAGENTS_TUSHARE_OPERATOR_SAFETY_CEILING_CALLS_PER_MINUTE": (
        "tushare_operator_safety_ceiling_calls_per_minute"
    ),
    "TRADINGAGENTS_YAHOO_MAX_PHYSICAL_ATTEMPTS": "yahoo_max_physical_attempts",
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")
_CRYPTO_REGISTRY_PATH_ENV = "TRADINGAGENTS_CRYPTO_IDENTITY_REGISTRY_PATH"
_CRYPTO_REGISTRY_DIGEST_ENV = "TRADINGAGENTS_CRYPTO_IDENTITY_REGISTRY_SHA256"


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
    loudly at startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    applied: set[str] = set()
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
            applied.add(env_var)
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    path_overridden = _CRYPTO_REGISTRY_PATH_ENV in applied
    digest_overridden = _CRYPTO_REGISTRY_DIGEST_ENV in applied
    if path_overridden != digest_overridden:
        missing_key = (
            "crypto_identity_registry_sha256"
            if path_overridden
            else "crypto_identity_registry_path"
        )
        config[missing_key] = None
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", _DEFAULT_RESULTS_DIR),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", _DEFAULT_CACHE_DIR),
    "memory_log_path": os.getenv(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        _DEFAULT_MEMORY_LOG_PATH,
    ),
    # The durable market-history store starts in shadow mode. Current analysis
    # continues to read the existing live provider chain until the mainland
    # compatibility gate passes. Switching back to "shadow" is the rollback.
    "market_history_mode": "shadow",
    "market_history_database_path": os.path.join(
        _DEFAULT_MARKET_HISTORY_ROOT,
        "market_history.sqlite3",
    ),
    "market_history_payload_root": os.path.join(
        _DEFAULT_MARKET_HISTORY_ROOT,
        "payloads",
    ),
    "market_history_backup_root": os.path.join(
        _DEFAULT_MARKET_HISTORY_ROOT,
        "backups",
    ),
    "data_usage_mode": "personal_research",
    # Capability-specific mainland provider routing is additive and opt-in.
    # The Tushare token remains exclusively in the existing TUSHARE_TOKEN
    # environment boundary and is never copied into runtime configuration.
    "mainland_capability_routing_mode": "legacy",
    "tushare_enabled_capabilities": (),
    "tushare_qualification_profile": "cn-a-2000-20260729-v1",
    "tushare_account_scope_label": "personal-research-default",
    "tushare_calls_per_minute": 40,
    "tushare_operator_safety_ceiling_calls_per_minute": 40,
    # Total Yahoo physical calls per coordinated sequence, including the first.
    "yahoo_max_physical_attempts": 4,
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.5",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Sampling temperature, forwarded to every provider when set. None leaves
    # each provider at its own default. Lower values reduce run-to-run
    # variation on models that honor it; reasoning models largely ignore it
    # and no setting makes LLM output bit-identical across runs (see README).
    "temperature": None,
    # SDK retry budget forwarded to every provider chat client. None leaves each
    # provider/SDK at its own default (usually 2). Raise it to ride out bursty
    # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
    "llm_max_retries": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Fail closed by default. "shadow" is an explicit temporary legacy override
    # whose directional output is visibly labeled as unenforced.
    "evidence_gate_mode": "enforce",
    # A registry is authoritative only when both its path and expected digest
    # are configured. Provider metadata may still enrich prompts but cannot
    # establish identity when this network-free trust anchor is absent.
    "instrument_identity_registry_path": None,
    "instrument_identity_registry_sha256": None,
    "crypto_identity_registry_path": _DEFAULT_CRYPTO_REGISTRY_PATH,
    "crypto_identity_registry_checksum_path": _DEFAULT_CRYPTO_CHECKSUM_PATH,
    "crypto_identity_registry_id": _DEFAULT_CRYPTO_REGISTRY_ID,
    "crypto_identity_registry_sha256": _DEFAULT_CRYPTO_REGISTRY_SHA256,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # China mainland A-share enhancement preset. The CLI asks for this only
    # when the ticker resolves to a mainland A-share. "basic" preserves current
    # behavior.
    "china_a_enhancement_preset": "basic",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category).
    # The configured value is the exact vendor chain — requests are NOT silently
    # routed to vendors you didn't choose. For ordered fallback, list several,
    # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
        "macro_data": "fred",                # Options: fred (needs FRED_API_KEY)
        "prediction_markets": "polymarket",  # Options: polymarket (keyless)
    },
    "market_data_vendors": {
        "cn_a": {
            "core_stock_apis": "akshare,baostock,yfinance",
            "technical_indicators": "akshare,baostock,yfinance",
            "fundamental_data": "akshare,yfinance,baostock",
            "news_data": "akshare,yfinance",
        },
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",       # NSE India (Nifty 50)
        ".BO":  "^BSESN",      # BSE India (Sensex)
        ".T":   "^N225",       # Tokyo (Nikkei 225)
        ".HK":  "^HSI",        # Hong Kong (Hang Seng)
        ".L":   "^FTSE",       # London (FTSE 100)
        ".TO":  "^GSPTSE",     # Toronto (TSX Composite)
        ".AX":  "^AXJO",       # Australia (ASX 200)
        ".SS":  "000001.SS",   # Shanghai (SSE Composite)
        ".SZ":  "399001.SZ",   # Shenzhen (SZSE Component)
        "":     "SPY",         # default for US-listed tickers (no suffix)
    },
})
