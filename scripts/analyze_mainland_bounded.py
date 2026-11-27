"""User-operated, single-stock bounded mainland analysis entry."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parents[1]
DESTINATION = 'https://api.waditu.com/dataapi'


class Stop(RuntimeError):
    """A fixed local reason, never an upstream exception body."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def regular_path(path, *, must_exist=True):
    for candidate in (path, *path.parents):
        if candidate.exists():
            info = candidate.lstat()
            require(not candidate.is_symlink()
                    and not getattr(info, 'st_file_attributes', 0) & 0x400,
                    'linked_path')
    require(not must_exist or path.is_file(), 'missing_bound_file')


def diagnostic_index(root, log_file):
    """Expose only this session's existing, conventional tool-message index."""
    if not log_file:
        return None
    try:
        path = Path(log_file)
        relative = path.relative_to(root).as_posix()
        if not re.fullmatch(r"analysis/[0-9]{6}\.(?:SS|SH|SZ)/[0-9]{4}-[0-9]{2}-[0-9]{2}/runs/[0-9]{8}_[0-9]{6}/message_tool\.log", relative):
            return None
        regular_path(path)
        return relative
    except (OSError, ValueError, Stop):
        return None


def verified_basic(root, results_root, expected=None):
    try:
        receipt = read_json(root / 'basic-publication.json')
        if expected is not None and receipt != expected:
            return None
        relative = receipt['report_relative']
        if not re.fullmatch(r'[0-9]{6}\.(?:SH|SS|SZ)/[0-9]{4}-[0-9]{2}-[0-9]{2}/runs/[0-9]{8}_[0-9]{6}/reports/report_exports/[0-9a-f]{32}/complete_report\.md', relative):
            return None
        report = results_root / relative
        regular_path(report)
        manifest = read_json(report.parent / 'manifest.json')
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        if (receipt['basic_publication_status'] == 'published'
                and receipt['basic_export_id'] == manifest['export_id'] == report.parent.name
                and digest == receipt['basic_sha256'] == manifest['files']['complete_report.md']):
            return receipt
    except (OSError, KeyError, ValueError, TypeError, Stop):
        pass
    return None


def read_json(path):
    regular_path(path)
    require(path.stat().st_size <= 2_000_000, 'oversize_receipt')
    return json.loads(path.read_text(encoding='utf-8'))


def write_once(root, name, value):
    path = root / name
    require(path.parent == root, 'receipt_path')
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def verify_release(manifest, *, source=SOURCE, python_root=None):
    """Exact reviewed files only; no recursive inventory or ignored read errors."""
    python_root = Path(sys.executable).resolve().parents[1] if python_root is None else python_root
    require(manifest.get('contract') == 'bounded-mainland-release-v1', 'release_contract')
    require(Path(manifest['deployment_root']).resolve() == source.resolve(),
            'installation_root_mismatch')
    roots = {'project': source, 'python': python_root, 'stdlib': Path(sys.base_prefix)}
    seen = set()
    require(bool(manifest.get('files')), 'empty_release')
    for item in manifest['files']:
        relative = Path(item['relative'])
        require(item['root'] in roots and not relative.is_absolute()
                and '..' not in relative.parts and relative.name not in {'.env', 'tradingagents.local.json'},
                'invalid_bound_path')
        key = (item['root'], str(relative).casefold())
        require(key not in seen, 'duplicate_bound_path')
        seen.add(key)
        path = roots[item['root']] / relative
        try:
            regular_path(path)
            require(hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'],
                    'bound_source_changed')
        except Stop as error:
            if str(error) in {'missing_bound_file', 'bound_source_changed'}:
                error.diagnostic = ('bound_runtime_mismatch' if item['root'] in {'python', 'stdlib'}
                                    else 'bound_project_mismatch')
            raise


class SharedSessionClock:
    """One wall/monotonic ceiling, including all financial preparation."""
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time, started=None):
        self.clock, self.wall_clock = clock, wall_clock
        self.started_monotonic, self.started_epoch = started or (clock(), wall_clock())
        self.deadline_epoch = self.started_epoch + 1200
        self.deadline_monotonic = self.started_monotonic + 1200

    def remaining(self):
        remaining = min(self.deadline_epoch - self.wall_clock(),
                        self.deadline_monotonic - self.clock())
        require(remaining > 0, 'session_deadline_exceeded')
        return remaining

    def effective_deadline(self):
        return min(self.deadline_epoch, self.wall_clock() + self.remaining())

    def shorten_to(self, expiry_epoch):
        remaining = expiry_epoch - self.wall_clock()
        require(remaining > 0, 'bank_snapshot_expired')
        self.deadline_epoch = min(self.deadline_epoch, expiry_epoch)
        self.deadline_monotonic = min(self.deadline_monotonic, self.clock() + remaining)
        self.remaining()


def validate_selection(selection):
    expected = {'asset_type': 'stock', 'research_depth': 1, 'llm_provider': 'deepseek',
                'backend_url': 'https://api.deepseek.com', 'shallow_thinker': 'deepseek-flash',
                'deep_thinker': 'deepseek-v4-pro', 'output_language': 'Chinese',
                'china_a_enhancement_preset': 'basic'}
    for key, value in expected.items():
        require(selection.get(key) == value, 'selection_mismatch_' + key)
    require(re.fullmatch(r'\d{6}\.(SS|SH|SZ)', selection.get('ticker', '')) is not None,
            'mainland_equity_required')
    try:
        as_of = date.fromisoformat(selection['analysis_date'])
    except (KeyError, TypeError, ValueError):
        raise Stop('invalid_analysis_date') from None
    require(as_of <= datetime.now(ZoneInfo('Asia/Shanghai')).date(), 'future_analysis_date')
    require([item.value for item in selection['analysts']] ==
            ['market', 'social', 'news', 'fundamentals'], 'analysts_mismatch')


def build_explicit_selection(ticker, analysis_date, config):
    """Build the menu-equivalent selection; identity remains a later trusted precheck."""
    from cli.models import AnalystType
    from cli.utils import normalize_ticker_symbol

    return {
        'ticker': normalize_ticker_symbol(ticker), 'asset_type': 'stock',
        'analysis_date': analysis_date,
        'analysts': [AnalystType(value) for value in ('market', 'social', 'news', 'fundamentals')],
        'research_depth': 1, 'output_language': 'Chinese',
        'china_a_enhancement_preset': 'basic', 'llm_provider': 'deepseek',
        'backend_url': 'https://api.deepseek.com',
        'shallow_thinker': 'deepseek-flash', 'deep_thinker': 'deepseek-v4-pro',
        'deepseek_reasoning_effort': 'high',
        'google_thinking_level': config.get('google_thinking_level'),
        'openai_reasoning_effort': config.get('openai_reasoning_effort'),
        'anthropic_effort': config.get('anthropic_effort'),
    }


def reserve_session(parent: Path, *, run_id=None) -> Path:
    """Create a new identity without reopening, clearing or retrying an old one."""
    parent = Path(parent).absolute()
    for path in (parent, *parent.parents):
        if path.exists():
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("linked_session_path")
    parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex if run_id is None else run_id
    require(re.fullmatch('[0-9a-f]{32}', run_id), 'invalid_session_identity')
    root = parent / run_id
    root.mkdir(exist_ok=False)
    write_once(root, 'session.json', {'contract': 'bounded-mainland-session-v1',
               'run_id': root.name, 'root': str(root), 'resume': False})
    return root


FIXED = {
    'analysis_skip_optional_fundamentals': True,
    'deepseek_reasoning_effort': 'high', 'llm_max_output_tokens': 65536,
    'llm_provider': 'deepseek', 'quick_think_llm': 'deepseek-flash',
    'deep_think_llm': 'deepseek-v4-pro', 'backend_url': 'https://api.deepseek.com',
    'output_language': 'Chinese', 'max_debate_rounds': 1, 'max_risk_discuss_rounds': 1,
    'llm_max_retries': 0, 'checkpoint_enabled': True, 'analysis_budget_enabled': True,
    'analysis_model_regeneration_enabled': True,
    'analysis_max_calls': 60, 'analysis_timeout_seconds': 1200,
    'financial_cache_only': True, 'market_history_mode': 'current',
    'data_usage_mode': 'personal_research', 'mainland_capability_routing_mode': 'legacy',
    'tushare_account_scope_label': 'personal-2000',
    'tushare_financial_https_endpoint': DESTINATION,
    'tushare_financial_cache_max_age_seconds': 86400,
    'tushare_financial_force_refresh': False, 'tushare_calls_per_minute': 40,
    'tushare_operator_safety_ceiling_calls_per_minute': 40,
    'analysis_disable_alpha_vantage': True, 'evidence_gate_mode': 'enforce',
    'cli_announcements_enabled': False,
}


def analysis_allowance(identity, calendar, financial, session_max_calls=60):
    require(type(session_max_calls) is int and 1 <= session_max_calls <= 60, 'invalid_session_max_calls')
    for used, maximum in ((identity, 1), (calendar, 1), (financial, 3)):
        require(type(used) is int and 0 <= used <= maximum, 'invalid_preparation_count')
    remaining = session_max_calls - identity - calendar - financial
    require(remaining >= 0, 'session_call_limit')
    return remaining


class Session:
    def __init__(self, root, *, source=SOURCE, session_max_calls=60, full_analysis=False, use_project_env=False, bank_snapshot=None,
                  calendar_authorized=False, supplemental_report_period=None, announcement_options=None,
                  moneyflow=False, margin_date=None, powershell_news_executable=None, news_source_isolation=False,
                  powershell_fundflow_executable=None, observation_cache_mode=None, fundflow_observation_sha256=None):
        require(not (full_analysis and observation_cache_mode == 'as_known_on_date'), 'strict_full_evidence_not_qualified')
        require(type(use_project_env) is bool, 'credential_mode')
        self.use_project_env = use_project_env
        self.bank_snapshot = bank_snapshot
        self.bank_identity = None
        self.equity_reference = None
        self.equity_identity = None
        self.root, self.source = Path(root), Path(source)
        self.paths = {
            'instrument_identity_registry_path': self.source / 'config/instrument_identity_registry.json',
            'data_cache_dir': self.source / 'data/cache',
            'market_history_database_path': self.source / 'data/market_history/market_history.sqlite3',
            'market_history_payload_root': self.source / 'data/market_history/payloads',
            'market_history_backup_root': self.source / 'data/market_history/backups',
            'checkpoint_data_dir': self.root / 'checkpoint-state',
            'results_dir': self.root / 'analysis',
            'memory_log_path': self.root / 'memory/trading_memory.md',
        }
        self.selection = None
        self.clock = None
        self.storage = None
        self.calendar_handoff = None
        self.calendar_identity = None
        self.calendar_authorized = calendar_authorized
        require(type(full_analysis) is bool and (not full_analysis or
                not (moneyflow or margin_date or powershell_fundflow_executable)), 'duplicate_full_enhancements')
        self.full_analysis = full_analysis
        self.session_max_calls = analysis_allowance(0, 0, 0, session_max_calls)
        self.identity_calls = 0
        self.financial_calls = 0
        self.phase = 'configuration'
        self.analysis_artifacts = {}
        self._supplemental_report_period = supplemental_report_period
        self._supplemental_verified_view = None
        self._supplemental_statement = None
        self._announcement_options = announcement_options
        require(type(moneyflow) is bool, 'invalid_moneyflow_option')
        self._moneyflow = moneyflow
        self._margin_date = margin_date
        self._powershell_news_executable = powershell_news_executable
        require(type(news_source_isolation) is bool and (not news_source_isolation or powershell_news_executable is not None),
                'invalid_news_source_isolation')
        self._news_source_isolation = news_source_isolation
        self._powershell_receipt_failed = False
        self._enhancement_status = {}
        self._published_basic = None
        self._postpublication_error = None
        self._margin_receipt_failed = False
        self._powershell_fundflow_executable = powershell_fundflow_executable
        self._observation_cache_mode = observation_cache_mode
        self._fundflow_observation_sha256 = fundflow_observation_sha256

    def configure(self, original, *, selection=None):
        value = copy.deepcopy(original)
        value.update(FIXED)
        value['session_max_calls'] = self.session_max_calls
        value['session_identity_calls'] = self.identity_calls
        value['analysis_max_calls'] = self.session_max_calls - self.identity_calls
        value['bounded_moneyflow_enabled'] = self._moneyflow
        value['bounded_margin_date'] = self._margin_date
        value['bounded_powershell_news_enabled'] = self._powershell_news_executable is not None
        value['bounded_powershell_fundflow_enabled'] = self._powershell_fundflow_executable is not None
        if self._observation_cache_mode is not None:
            value['bounded_observation_cache_mode'] = self._observation_cache_mode
        if self._margin_date is not None or self._powershell_fundflow_executable is not None:
            value.setdefault('bounded_observation_cache_mode', 'retrospective')
        require(value.get('bounded_observation_cache_mode', 'disabled') in (
            'disabled', 'retrospective', 'as_known_on_date'), 'invalid_observation_cache_mode')
        if self._fundflow_observation_sha256 is not None:
            value['bounded_fundflow_observation_sha256'] = self._fundflow_observation_sha256
        if self._news_source_isolation:
            value['bounded_news_source_isolation'] = True
        else:
            value.pop('bounded_news_source_isolation', None)
        value.pop('analysis_calendar_receipt_sha256', None)
        value.pop('analysis_calendar_calls', None)
        if self.calendar_handoff is not None:
            count = self.check_calendar_handoff()
            value['analysis_max_calls'] = analysis_allowance(self.identity_calls, count, self.financial_calls, self.session_max_calls)
            value['analysis_calendar_calls'] = count
            value['analysis_calendar_receipt_sha256'] = self.calendar_handoff['sha256']
        value.update({key: str(path) for key, path in self.paths.items()})
        value['instrument_identity_registry_sha256'] = (
            self.source / 'config/instrument_identity_registry.sha256').read_text(encoding='ascii').split()[0]
        if self.equity_identity is not None:
            value['instrument_identity_registry_sha256'] = self.equity_identity['registry_sha256']
        if self.bank_identity is not None:
            value['instrument_identity_registry_sha256'] = self.bank_identity['registry_sha256']
        value['mainland_capability_routing_plan_version'] = None
        if selection is not None:
            from tradingagents.budget_preflight import apply_mainland_budget_sources
            value = apply_mainland_budget_sources(value, ticker=selection['ticker'], include_macro_sources=False)
        if self.clock is not None:
            value['analysis_session_deadline_epoch'] = self.clock.effective_deadline()
            value['analysis_session_monotonic_deadline'] = self.clock.deadline_monotonic
        return value

    def check_calendar_handoff(self, config=None):
        from tradingagents.market_history.calendar_preparation_runtime import (
            expected_context,
            verify_handoff,
        )

        require(self.calendar_handoff is not None and self.calendar_identity is not None,
                'calendar_handoff_missing')
        current = {**FIXED, 'session_max_calls': self.session_max_calls, 'session_identity_calls': self.identity_calls, **{key: str(path) for key, path in self.paths.items()}}
        context = expected_context(current, root=self.root, source_root=self.source,
            symbol=self.selection['ticker'], identity_revision=self.calendar_identity['revision'],
            venue=self.calendar_identity['venue'], as_of=self.selection['analysis_date'],
            szse_refresh=self.full_analysis and self.calendar_identity['venue']=='XSHE'
                and self.calendar_handoff['calendar_calls']==1)
        count = verify_handoff(self.root, self.calendar_handoff, context)
        if config is not None:
            require(config.get('analysis_max_calls') == analysis_allowance(self.identity_calls, count, self.financial_calls, self.session_max_calls)
                    and config.get('analysis_calendar_calls') == count
                    and config.get('analysis_calendar_receipt_sha256') == self.calendar_handoff['sha256']
                    and all(config.get(key) == str(value) for key, value in self.paths.items()),
                    'calendar_budget_handoff_mismatch')
        return count

    def check_bank_identity(self):
        if self.equity_identity is not None:
            from tradingagents.dataflows.bank_identity_snapshot import validate_binding
            validate_binding(self.source, self.root, self.equity_identity, self.selection['ticker'],
                             self.selection['analysis_date'], equity=True)
            self.clock.remaining()
        if self.bank_identity is not None:
            from tradingagents.dataflows.bank_identity_snapshot import validate_binding
            validate_binding(self.source, self.root, self.bank_identity,
                             self.selection['ticker'], self.selection['analysis_date'])
            self.clock.remaining()

    def prepare_bank_identity(self):
        if self.equity_reference is not None:
            require(self.bank_snapshot is None, 'conflicting_identity_contracts')
            from tradingagents.dataflows.bank_identity_snapshot import instant, prepare_overlay
            self.equity_identity = prepare_overlay(self.source, self.root, self.selection['ticker'],
                self.selection['analysis_date'], self.equity_reference, equity=True)
            require(self.equity_identity is not None,'equity_overlay_missing')
            self.paths['instrument_identity_registry_path'] = self.root / 'equity-identity-registry.json'
            self.clock.shorten_to(instant(self.equity_identity['expires_at']).timestamp())
            write_once(self.root, 'equity-identity-binding.json', self.equity_identity)
        if self.bank_snapshot is not None:
            from tradingagents.dataflows.bank_identity_snapshot import instant, prepare_overlay
            self.bank_identity = prepare_overlay(self.source, self.root, self.selection['ticker'],
                                                self.selection['analysis_date'], self.bank_snapshot)
            if self.bank_identity is not None:
                self.paths['instrument_identity_registry_path'] = self.root / 'bank-identity-registry.json'
                self.clock.shorten_to(instant(self.bank_identity['expires_at']).timestamp())
                write_once(self.root, 'bank-identity-binding.json', self.bank_identity)

    def precheck(self, original):
        """Bind the selected identity and existing stores; never initialize a real store."""
        require(self.calendar_authorized is True, 'calendar_confirmation_required')
        from diagnostic_runtime import SharedStatementStorage

        from tradingagents.asset_configuration import resolve_run_asset_configuration
        from tradingagents.budget_readiness import inspect_budget_readiness
        from tradingagents.dataflows.current_tushare_statements import NORMALIZER_VERSION
        from tradingagents.dataflows.provider_subrequests import (
            ProviderSubrequestInstrumentIdentity,
        )
        from tradingagents.statement_acceptance import validate_request

        self.clock.remaining()
        self.prepare_bank_identity()
        config = self.configure(original, selection=self.selection)
        asset = resolve_run_asset_configuration(self.selection['ticker'], config=config)
        identity = ProviderSubrequestInstrumentIdentity.from_authoritative(asset.instrument_identity)
        self.calendar_identity = {'revision': identity.identity_revision, 'venue': asset.instrument_identity.venue}
        self.selection['ticker'] = asset.instrument_identity.symbol
        market = self.paths['market_history_database_path'].resolve()
        coordinator = market.with_name(market.name + '.provider-requests.sqlite3')
        cache = self.paths['data_cache_dir'].resolve() / 'current-financial-statements.sqlite3'
        for path in (market, coordinator):
            regular_path(path)
        regular_path(cache, must_exist=False)
        state = coordinator.stat()
        storage = {'market_database': str(market), 'coordinator_database': str(coordinator),
                   'cache_database': str(cache), 'coordinator_file_id': [state.st_dev, state.st_ino]}
        self.storage = SharedStatementStorage(storage)
        as_of = self.selection['analysis_date']
        binding = {'contract': 'shared-statement-acceptance-v1',
                   'account': FIXED['tushare_account_scope_label'], 'destination': DESTINATION,
                   'instrument_identity_revision': identity.identity_revision,
                   'as_of': as_of, 'announcement_start': date(date.fromisoformat(as_of).year - 6, 1, 1).isoformat(),
                   'announcement_end': as_of, 'normalizer_version': NORMALIZER_VERSION,
                   'storage': storage}
        if self.equity_identity is not None:
            binding['equity_identity'] = self.equity_identity
        if self.bank_identity is not None:
            binding['bank_identity'] = self.bank_identity
        validate_request(self.root / 'refresh', ticker=self.selection['ticker'], destination=DESTINATION,
                         account=binding['account'], as_of=as_of,
                         shared_binding=binding, shared_storage=self.storage)
        # Expired financial observations are deliberately left for cache-first acquisition.
        readiness = inspect_budget_readiness(config, ticker=self.selection['ticker'], as_of_date=as_of,
                                              selected_analysts=('market',),
                                              instrument_identity=asset.instrument_identity)
        from tradingagents.market_history.calendar_preparation_runtime import prepare_for_session
        self.phase = 'calendar_preparation'
        require(self.session_max_calls > self.identity_calls, 'session_call_limit')
        if self.full_analysis:
            from tradingagents.full_session_ledger import preparation_role
        with preparation_role("calendar") if self.full_analysis else contextlib.nullcontext():
            self.calendar_handoff = prepare_for_session(config, identity=asset.instrument_identity,
            identity_revision=identity.identity_revision, as_of=as_of, root=self.root,
            source_root=self.source, clock=self.clock, prior_readiness=readiness,
            confirmed=self.calendar_authorized)
        config = self.configure(original, selection=self.selection)
        self.phase = 'precheck'
        readiness = inspect_budget_readiness(config, ticker=self.selection['ticker'], as_of_date=as_of,
                                              selected_analysts=('market',),
                                              instrument_identity=asset.instrument_identity)
        write_once(self.root, 'precheck.json', {'issues': [vars(issue) for issue in readiness.issues],
                   'checked': [vars(check) for check in readiness.checked],
                   'financial_policy': 'cache_first_at_execution', 'sqlite_readonly_coordination': True})
        require(not readiness.issues, 'market_calendar_not_ready')
        self.selection['ticker'] = asset.instrument_identity.symbol
        write_once(self.root, 'shared-binding.json', binding)
        return config

    def refresh(self):
        self.check_bank_identity()
        self.clock.remaining()
        # The supervised financial child may issue up to three physical requests.
        # Reserve this capacity before spawning it; only actual sends are charged.
        calendar_calls = self.check_calendar_handoff()
        require(analysis_allowance(self.identity_calls, calendar_calls, 0, self.session_max_calls) >= 3,
                'insufficient_financial_preparation_allowance')
        command = [sys.executable, '-I', '-S', '-B', str(self.source / 'scripts/accept_financial_statements.py'),
                   '--output', str(self.root / 'refresh'), '--destination', DESTINATION,
                   '--account', FIXED['tushare_account_scope_label'], '--ticker', self.selection['ticker'],
                   '--as-of', self.selection['analysis_date'], '--shared-binding', str(self.root / 'shared-binding.json'),
                   '--authorize-live', '--authorize-initialization-probes',
                   '--session-deadline-epoch', str(self.clock.effective_deadline()),
                   '--session-monotonic-deadline', str(self.clock.deadline_monotonic)]
        command.append('--allow-optional-financial-gaps')
        if self.use_project_env:
            command.append('--use-project-env')
        # Keep both child environments credential-free. In hidden mode, pass
        # only this session's Tushare token over a one-use anonymous stdin pipe.
        from diagnostic_runtime import safe_environment
        if self.full_analysis:
            from tradingagents.full_session_ledger import owner
            from full_financial_channel import serve
            from session_token_pipe import encode_session_token
            owned=owner()
            require(owned is not None and owned.key==self.root.name,"verified_full_owner_required")
            command.append("--full-ticket-stdin")
            if not self.use_project_env:command.append("--session-token-stdin")
            with serve(owned,self.source,self.root) as key:
                frame=key.encode("ascii")+b"\n"
                if not self.use_project_env:
                    frame+=encode_session_token(os.environ.get("TUSHARE_TOKEN"),self.root.name)
                result=subprocess.run(command,input=frame,cwd=self.source,env=safe_environment(self.root),
                                      timeout=self.clock.remaining(),check=False)
        elif self.use_project_env:
            result = subprocess.run(command, cwd=self.source, env=safe_environment(self.root), check=False)
        else:
            from session_token_pipe import send_session_token
            command.append('--session-token-stdin')
            result = send_session_token(command, token=os.environ.get('TUSHARE_TOKEN'),
                run_id=self.root.name, cwd=self.source, env=safe_environment(self.root))
        require(result.returncode == 0, 'refresh_stopped')
        counts = self.verify_refresh()
        self.financial_calls = counts[0]
        return counts

    def verify_refresh(self):
        self.check_bank_identity()
        result = read_json(self.root / 'refresh/result.json')
        live = read_json(self.root / 'refresh/live.json')
        reuse = read_json(self.root / 'refresh/reuse.json')
        require(result.get('passed') is True and result.get('status') in {'passed', 'passed_degraded'}
                and (result.get('same_six_outcomes_and_timestamps') is True
                     or result.get('same_six_views_and_timestamps') is True), 'refresh_not_passed')
        require(live.get('canonical_symbol') == reuse.get('canonical_symbol') == self.selection['ticker'],
                'refresh_symbol_mismatch')
        require(live.get('status') in {'acquired', 'acquired_degraded'}
                and reuse.get('status') == ('reused_degraded' if live.get('status') == 'acquired_degraded' else 'reused')
                and live.get('pid') != reuse.get('pid'), 'reuse_not_independent')
        require(type(live.get('physical_requests')) is int and 0 <= live['physical_requests'] <= 3
                and type(reuse.get('physical_requests')) is int and reuse['physical_requests'] == 0
                and reuse.get('client_attempts') == 0, 'refresh_count_mismatch')
        require(len(live.get('views', {})) == 6 and live['views'] == reuse.get('views'), 'refresh_views_mismatch')
        contract = read_json(self.root / 'refresh/contract.json')
        require(contract['request']['shared_binding'] == read_json(self.root / 'shared-binding.json'),
                'refresh_binding_mismatch')
        from tradingagents.dataflows.current_statement_diagnostics import (
            scopes_from_binding,
            validate_prepared_views,
        )
        from tradingagents.evidence import (
            EvidenceCapability,
            InstrumentKind,
            capability_profile_for,
        )
        optional = EvidenceCapability.COMPANY_FINANCIALS in capability_profile_for(InstrumentKind.EQUITY).optional_capabilities
        gaps = validate_prepared_views(live['views'], ticker=self.selection['ticker'],
                                       as_of=self.selection['analysis_date'], allow_optional_gaps=optional,
                                       expected_scopes=scopes_from_binding(contract['request']['shared_binding']))
        require((gaps > 0) == (result.get('status') == 'passed_degraded')
                and live.get('status') == ('acquired_degraded' if gaps else 'acquired')
                and reuse.get('status') == ('reused_degraded' if gaps else 'reused')
                and (not gaps or contract.get('allow_optional_financial_gaps') is True),
                'financial_preparation_status_mismatch')
        if self._supplemental_report_period is not None:
            # Only a deep copy of this successful invocation's prepared view is a
            # source anchor. Never rescan historical receipts or arbitrary cache.
            self._supplemental_verified_view = copy.deepcopy(live['views']['get_balance_sheet:quarterly'])
            from retained_supplemental_statement import load_retained_supplemental_statement, SupplementalSourceRejected
            try:
                self._supplemental_statement = load_retained_supplemental_statement(
                    self.paths['data_cache_dir'], verified_view=self._supplemental_verified_view,
                    shared_binding=copy.deepcopy(contract['request']['shared_binding']),
                    ticker=self.selection['ticker'], as_of=self.selection['analysis_date'],
                    report_period=self._supplemental_report_period,
                )
            except SupplementalSourceRejected as error:
                raise Stop(str(error)) from None
        self.clock.remaining()
        return [live['physical_requests'], 0]

    @contextlib.contextmanager
    def analysis_hooks(self, cli, config, artifacts):
        """Reuse the reviewed CLI and gate, restoring process hooks on every exit."""
        from tradingagents import analysis_budget
        from tradingagents.evidence import evaluate_admission_gate
        from tradingagents.graph.conditional_logic import ConditionalLogic
        from tradingagents.graph.evidence_gate import _evidence_from_state

        with contextlib.ExitStack() as stack:
            basic_export = {}
            margin = fundflow = None
            if self._powershell_fundflow_executable is not None:
                from tradingagents.powershell_fundflow import PowerShellFundflow, powershell_fundflow_scope
                fundflow = PowerShellFundflow(self._powershell_fundflow_executable,
                    self.source / 'scripts/Get-PublicFundflow.ps1', self.selection['ticker'], self.selection['analysis_date'],
                    after_report=True)
                def save_fundflow_receipt(exc_type, error, traceback):
                    try:
                        write_once(self.root, 'powershell-fundflow.json', {**fundflow.summary(), **basic_export})
                    except Exception:
                        self._powershell_receipt_failed = True
                        if error is None:
                            raise Stop('powershell_fundflow_receipt_write_failed') from None
                    return False
                stack.push(save_fundflow_receipt)
                stack.enter_context(powershell_fundflow_scope(fundflow))
            if self._powershell_news_executable is not None:
                from tradingagents.powershell_news import PowerShellNews, powershell_news_scope

                news = PowerShellNews(self._powershell_news_executable, self.source / 'scripts/Get-PublicNews.ps1',
                                      self.selection['ticker'], self.selection['analysis_date'],
                                      isolate_invalid_sources=self._news_source_isolation,
                                      as_known_on_date=config.get('bounded_observation_cache_mode') == 'as_known_on_date')
                def save_news_receipt(exc_type, error, traceback):
                    try:
                        write_once(self.root, 'powershell-news.json', news.summary())
                    except Exception:
                        self._powershell_receipt_failed = True
                        if error is None:
                            raise Stop('powershell_receipt_write_failed') from None
                        # Keep the original budget/cancel/provider failure. The
                        # separate fixed flag survives in the terminal receipt.
                    return False
                stack.push(save_news_receipt)
                stack.enter_context(powershell_news_scope(news))
            if self._moneyflow:
                from tradingagents.moneyflow import moneyflow_scope

                stack.enter_context(moneyflow_scope(config, ticker=self.selection['ticker'],
                                                    as_of=self.selection['analysis_date']))
            if self._margin_date is not None:
                from tradingagents.margin_balance import _Run
                margin = _Run(config, self.selection['ticker'], self.selection['analysis_date'], self._margin_date)
                def save_margin_receipt(exc_type, error, traceback):
                    try:
                        write_once(self.root, 'margin-balance.json', {**basic_export,
                            'source_status': self._enhancement_status.get('margin', 'not_started'),
                            'observation': margin.result, 'transport': dict(margin.transport)})
                    except Exception:
                        self._margin_receipt_failed = True
                        if error is None:
                            raise Stop('margin_receipt_write_failed') from None
                    return False
                stack.push(save_margin_receipt)
                stack.callback(margin.cancel)
            def replace(owner, name, value):
                original = getattr(owner, name)
                stack.callback(setattr, owner, name, original)
                setattr(owner, name, value)
                return original

            if self.full_analysis:
                from tradingagents.full_factory import session_sources
                stack.enter_context(session_sources(self.selection, self._observation_cache_mode,
                                                    producer_config=config, pair_runtime=getattr(self,"pair_runtime",None), selected=getattr(self,"selected_enhancements",None), bulletin=getattr(self, "full_bulletin", None),local_sources=getattr(self,"full_local_sources",None)))
            replace(cli, 'DEFAULT_CONFIG', config)
            replace(cli, 'get_user_selections', lambda: dict(self.selection, china_a_enhancement_preset='all')
                    if self.full_analysis else self.selection)
            if fundflow is not None or margin is not None:
                original_write = cli._write_run_reports
                def report_digest(report):
                    regular_path(report)
                    return hashlib.sha256(report.read_bytes()).hexdigest()

                original_failure = cli._mark_run_failed
                def record_failure(artifacts, exc, current_phase):
                    publication = self._published_basic
                    if (publication is None or not isinstance(exc, analysis_budget.AnalysisBudgetStopped)
                            or exc.reason != 'deadline_exceeded'):
                        return original_failure(artifacts, exc, current_phase)
                    receipt = verified_basic(self.root, self.paths['results_dir'], publication)
                    if receipt is None:
                        return original_failure(artifacts, exc, current_phase)
                    details = dict(basic_status='completed', basic_export_id=receipt['basic_export_id'],
                                   basic_sha256=receipt['basic_sha256'], stop_reason=exc.reason,
                                   cleanup_status='failed', enhancement_sources=dict(self._enhancement_status))
                    original_failure(artifacts, exc, current_phase, verified_deadline=details)
                replace(cli, '_mark_run_failed', record_failure)
                def publish_with_fundflow(state, ticker, run_artifacts):
                    from tradingagents.terminal_contract import build_terminal_contract
                    from tradingagents.research_stream import full_graph_valid
                    if not full_graph_valid(state):
                        report = original_write(state, ticker, run_artifacts)
                        basic_export.update(basic_export_id=report.parent.name, basic_publication_status='published', basic_sha256=report_digest(report),
                                           report_relative=report.relative_to(self.paths['results_dir']).as_posix())
                        basic_export['enhancement_skipped'] = 'basic_outcome_without_decision'
                        for name, owner in [('margin', margin), ('fundflow', fundflow)]:
                            if owner is not None:
                                self._enhancement_status[name] = 'skipped_no_decision'
                        self._published_basic = dict(basic_export)
                        run_artifacts['published_basic'] = self._published_basic
                        write_once(self.root, 'basic-publication.json', self._published_basic)
                        return report
                    # The Basic graph has finished all mandatory data and model calls.
                    # The worker owns its acquisition context and uses the run's budget.
                    from concurrent.futures import ThreadPoolExecutor
                    collect_margin = margin.prepare(state) if margin is not None else None
                    if margin is not None and collect_margin is None:
                        self._enhancement_status['margin'] = 'binding_unavailable'

                    from tradingagents.powershell_fundflow import FundflowStop

                    def collect_selected():
                        if fundflow is not None:
                            self._enhancement_status['fundflow'] = 'interrupted'
                            try:
                                fundflow.collect_after_report()
                            except FundflowStop as error:
                                if error.args != ('fundflow_access_denied',):
                                    raise
                                status = fundflow.summary()['transport'].get('http_status')
                                self._enhancement_status['fundflow'] = {401: 'authentication', 403: 'permission_denied'}.get(status, 'access_denied')
                                if collect_margin is not None:
                                    self._enhancement_status['margin'] = 'skipped_after_access_denied'
                                return
                            observation = fundflow.summary()['observation'] or {}
                            transport = fundflow.summary()['transport']
                            self._enhancement_status['fundflow'] = ('timeout' if transport.get('status') == 'timeout'
                                else 'unavailable' if transport.get('status') == 'retrospective_cache_unavailable'
                                else observation.get('status', 'not_collected'))
                        if collect_margin is not None:
                            self._enhancement_status['margin'] = 'interrupted'
                            result = collect_margin()
                            self._enhancement_status['margin'] = result['status']

                    try:
                        report = original_write(state, ticker, run_artifacts)
                        basic_export.update(basic_export_id=report.parent.name, basic_publication_status='published',
                                            basic_sha256=report_digest(report),
                                            report_relative=report.relative_to(self.paths['results_dir']).as_posix())
                        publication = dict(basic_export)
                        write_once(self.root, 'basic-publication.json', publication)
                        self._published_basic = publication
                        run_artifacts['published_basic'] = publication
                    except BaseException:
                        if margin is not None:
                            margin.cancel()
                        if fundflow is not None:
                            fundflow.cancel_collection()
                        raise

                    # Collection runs in its own thread, after the Basic receipt has been written.
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        try:
                            worker.submit(collect_selected).result()
                        except (Exception, FundflowStop, analysis_budget.AnalysisBudgetStopped) as error:
                            self._postpublication_error = error
                            self._enhancement_status['worker'] = 'failed'
                    basic_export['enhancement_completion'] = dict(self._enhancement_status)
                    return report
                replace(cli, '_write_run_reports', publish_with_fundflow)
            if self._supplemental_report_period is not None or self._announcement_options is not None or self._news_source_isolation:
                if self._supplemental_report_period is not None:
                    require(self._supplemental_statement is not None, 'supplemental_source_not_prepared')
                announcement = None
                if self._announcement_options is not None:
                    from tradingagents.announcement_report import AnnouncementRequest

                    announcement = AnnouncementRequest(*self._announcement_options)
                original_save = cli.save_report_to_disk
                def save_with_supplement(*args, **kwargs):
                    if self._news_source_isolation:
                        from tradingagents.news_projection import unavailable
                        require('news_source_selection' not in kwargs, 'news_selection_override_forbidden')
                        kwargs['news_source_selection'] = news.value or unavailable(
                            news.request, 'not_attempted', isolate_invalid_sources=True)
                    if self._supplemental_report_period is not None:
                        require('supplemental_statement' not in kwargs
                                or kwargs['supplemental_statement'] is self._supplemental_statement,
                                'supplemental_request_override_forbidden')
                        kwargs['supplemental_statement'] = self._supplemental_statement
                    if announcement is not None:
                        require('company_announcement' not in kwargs
                                or kwargs['company_announcement'] is announcement,
                                'announcement_request_override_forbidden')
                        require('announcement_timeout_seconds' not in kwargs and 'announcement_remaining' not in kwargs,
                                'announcement_deadline_override_forbidden')
                        kwargs['company_announcement'] = announcement
                        kwargs['announcement_timeout_seconds'] = 25
                        kwargs['announcement_remaining'] = self.clock.remaining
                    return original_save(*args, **kwargs)
                # Both automated _write_run_reports and the explicit save prompt
                # resolve this same global; ExitStack restores it on every exit.
                replace(cli, 'save_report_to_disk', save_with_supplement)
            original_build = cli._build_run_config

            def build(*args, **kwargs):
                value = self.configure(original_build(*args, **kwargs), selection=self.selection)
                value['bounded_noninteractive'] = not sys.stdin.isatty()
                self.clock.remaining()
                return value

            replace(cli, '_build_run_config', build)
            original_artifacts = cli._prepare_run_artifacts

            def prepare(*args, **kwargs):
                value = original_artifacts(*args, **kwargs)
                artifacts.update(value)
                return value

            replace(cli, '_prepare_run_artifacts', prepare)
            original_bind = analysis_budget.AnalysisBudget.bind_checkpoint

            def bind(budget, connection, lock, thread, policy_binding, *, has_checkpoint):
                require(not has_checkpoint, 'unexpected_checkpoint')
                self.clock.remaining()
                count = self.check_calendar_handoff(config)
                require(budget.max_calls == analysis_allowance(self.identity_calls, count, self.financial_calls, self.session_max_calls),
                        'preparation_budget_handoff_mismatch')
                require(budget._session_deadline_epoch is not None
                        and budget._session_deadline_epoch <= self.clock.deadline_epoch
                        and budget._session_monotonic_deadline == self.clock.deadline_monotonic,
                        'shared_deadline_not_bound')
                original_bind(budget, connection, lock, thread, policy_binding, has_checkpoint=has_checkpoint)
                require(budget._deadline <= self.clock.deadline_epoch, 'deadline_reset_forbidden')
                write_once(self.root, 'budget-binding.json', {'thread': thread, 'policy_binding': policy_binding,
                           'deadline': budget._deadline, 'session_monotonic_deadline': budget._session_monotonic_deadline,
                           'checkpoint_directory': str(self.paths['checkpoint_data_dir']), 'resume': False})

            replace(analysis_budget.AnalysisBudget, 'bind_checkpoint', bind)
            for analyst, clear in (('market', 'Msg Clear Market'), ('social', 'Msg Clear Sentiment'),
                                   ('news', 'Msg Clear News'), ('fundamentals', 'Msg Clear Fundamentals')):
                name = 'should_continue_' + analyst
                original = getattr(ConditionalLogic, name)

                def route(owner, state, *, _original=original, _analyst=analyst, _clear=clear):
                    destination = _original(owner, state)
                    if destination == _clear:
                        gate = evaluate_admission_gate(_evidence_from_state(state.get('evidence_state')))
                        if not gate.admitted:
                            write_once(self.root, 'critical-stop.json', {'stage': 'analyst_terminal_route',
                                       'analyst': _analyst, 'reason': 'critical_evidence_failure',
                                       'readiness': gate.readiness.value,
                                       'diagnostic_codes': [code.value for code in gate.diagnostic_codes],
                                       'diagnostic_index': diagnostic_index(self.root, artifacts.get('log_file')),
                                       'subsequent_analysis_sends_allowed': False})
                            budget = analysis_budget.active_budget()
                            require(budget is not None, 'critical_stop_budget_missing')
                            budget.stop('critical_evidence_failure')
                            raise Stop('critical_evidence_failure')
                    return destination

                replace(ConditionalLogic, name, route)
            yield

    def analyze(self, cli, config):
        # Only the artifacts captured by this invocation may explain its stop.
        self.analysis_artifacts = {}
        from tradingagents.statement_acceptance import _retain_shared_ceiling
        calendar_calls = self.check_calendar_handoff(config)
        self.check_bank_identity()
        self.clock.remaining()
        checkpoint = self.paths['checkpoint_data_dir'] / 'checkpoints' / (self.selection['ticker'] + '.db')
        require(not any(Path(str(checkpoint) + suffix).exists() for suffix in ('', '-wal', '-shm', '-journal')),
                'preexisting_checkpoint')
        artifacts = self.analysis_artifacts
        with _retain_shared_ceiling(self.storage), self.analysis_hooks(cli, config, artifacts):
            exit_code = cli.app(args=['analyze', '--checkpoint', '--mainland-core-sources'], standalone_mode=False)
        require(exit_code in (None, 0, 130) or (self.full_analysis and exit_code == 2), 'cli_nonzero_exit')
        require('status_file' in artifacts, 'analysis_outcome_missing')
        status = read_json(Path(artifacts['status_file']))
        require(status.get('status') == 'completed', 'analysis_not_completed')
        if self._postpublication_error is not None:
            raise self._postpublication_error
        attempts = status.get('analysis_budget', {}).get('admitted_attempts')
        require(type(attempts) is int and 0 <= attempts <= config['analysis_max_calls'],
                'calendar_combined_budget_mismatch')
        unmet = any(value not in ('available', 'available_partial')
                    for value in self._enhancement_status.values())
        if self.full_analysis:
            full = read_json(Path(artifacts['report_dir']) / 'full-result.json')
            require(full['ticker'] == self.selection['ticker'] and full['cutoff'] == self.selection['analysis_date']
                    and full['requested_preset'] == 'all' and status.get('full_acceptance') == full['status'],
                    'full_receipt_mismatch')
            unmet = full['status'] not in ('completed','custom_complete') or exit_code not in (None, 0)
            self._enhancement_status = full['sources']
            if hasattr(self,"date_layer"):
                module,manifest=self.date_layer
                try:
                    module["publish"](SOURCE,manifest,Path(artifacts['report_dir']),self.selection['analysis_date'],datetime.now(ZoneInfo('Asia/Shanghai')),ticker=self.selection['ticker'])
                except (ValueError,KeyError,OSError,StopIteration,TypeError) as error:
                    write_once(self.root,'latest-dynamics-failure.json',{'status':'unavailable','error_type':type(error).__name__})
        result = {'status': 'enhancement_incomplete' if unmet else ('custom_complete' if self.full_analysis and full.get('scope')=='custom_full' else 'completed'),
                'basic_status': 'completed', 'enhancement_sources': dict(self._enhancement_status),
                'status_file': str(artifacts['status_file']),
                'terminal_outcome_kind': status.get('terminal_outcome_kind'),
                'evidence_integrity_status': status.get('evidence_integrity_status'),
                'analysis_budget': status.get('analysis_budget'),
                'calendar_requests': calendar_calls,
                'calendar_plus_analysis_requests': calendar_calls + attempts,
                'total_external_attempts': self.identity_calls + calendar_calls + self.financial_calls + attempts,
                'completion_is_not_decision_qualification': True}
        if self.full_analysis:
            add_full_total(result,self.root)
        return result


    def stopped_analysis_summary(self):
        """Read after CLI cleanup; never replace the failure or open another run."""
        status_file = self.analysis_artifacts.get('status_file')
        if status_file is None:
            return {'status': 'diagnostic_unavailable', 'reason': 'run_artifacts_unavailable'}
        try:
            path = Path(status_file).absolute()
            run_dir = path.parent
            if (path.name != 'run_status.json' or len(run_dir.parents) < 5
                    or run_dir.parents[4] != self.root.absolute()):
                return {'status': 'diagnostic_unavailable', 'reason': 'run_artifacts_mismatch'}
            from summarize_analysis_stop import summarize_stop

            return summarize_stop(run_dir, project_root=self.source)
        except Exception:
            # No arbitrary exception text, paths, configuration or credentials.
            return {'status': 'diagnostic_unavailable', 'reason': 'summary_unavailable'}

    def execute(self, cli, config, *, verify_sources, requested_selection=None):
        """One invocation. Any failure leaves its original identity and evidence."""
        from tradingagents.market_history.calendar_preparation import CalendarStopped

        from tradingagents import analysis_budget

        financial_counts = None
        try:
            self.phase = 'selection'
            with analysis_budget.analysis_preparation_scope(config):
                self.selection = (cli.get_user_selections() if requested_selection is None
                                  else build_explicit_selection(*requested_selection, config))
                validate_selection(self.selection)
                if self.selection['ticker'] in ('601328.SH', '601328.SS') and self.selection['analysis_date'] == '2026-11-13':
                    raise Stop('prior_dispatch_unreconciled')
                if self._supplemental_report_period is not None:
                    require(date.fromisoformat(self._supplemental_report_period)
                            <= date.fromisoformat(self.selection['analysis_date']),
                            'supplemental_period_after_analysis')
            self.phase = 'precheck'
            verify_sources()
            config = self.precheck(config)
            write_once(self.root, 'configuration.json', {**FIXED, 'session_max_calls': self.session_max_calls,
                       'bounded_moneyflow_enabled': self._moneyflow,
                       'bounded_powershell_news_enabled': self._powershell_news_executable is not None,
                       'bounded_powershell_fundflow_enabled': self._powershell_fundflow_executable is not None,
                       **({'bounded_news_source_isolation': True} if self._news_source_isolation else {}),
                        'bounded_margin_date': self._margin_date,
                       **({'bounded_observation_cache_mode': config['bounded_observation_cache_mode']}
                          if 'bounded_observation_cache_mode' in config else {}),
                       'analysis_max_calls': config['analysis_max_calls'],
                       'budget_stage': 'before_financial_refresh',
                       'analysis_calendar_calls': config['analysis_calendar_calls'],
                       'analysis_calendar_receipt_sha256': config['analysis_calendar_receipt_sha256'],
                       'ticker': self.selection['ticker'], 'analysis_date': self.selection['analysis_date'],
                       'analysts': ['market', 'social', 'news', 'fundamentals'],
                       'paths': {key: str(path) for key, path in self.paths.items()}, 'resume': False})
            verify_sources()
            self.phase = 'financial_preparation'
            financial_counts = self.refresh()
            self.financial_calls = financial_counts[0]
            config['analysis_max_calls'] = analysis_allowance(
                self.identity_calls, config['analysis_calendar_calls'], self.financial_calls, self.session_max_calls)
            write_once(self.root, 'preparation-counts.json', {
                'identity': self.identity_calls, 'calendar': config['analysis_calendar_calls'],
                'financial': self.financial_calls, 'remaining_for_analysis': config['analysis_max_calls']})
            verify_sources()
            self.phase = 'analysis'
            if self.full_analysis:
                from tradingagents.full_session_ledger import owner
                require(owner() is not None and owner().key==self.root.name,"verified_full_owner_required")
                owner().phase='analysis'
            require(config['analysis_max_calls'] > 0, 'session_call_limit')
            value = self.analyze(cli, config)
        except BaseException as error:
            # Production run_diagnostics retains the detailed redacted model/data failure.
            # This outer lifecycle receipt never includes arbitrary exception text or config.
            reason = (error.reason if isinstance(error, analysis_budget.AnalysisBudgetStopped) else
                      str(error) if type(error) is Stop else
                      error.code if type(error) is CalendarStopped else 'application_stopped')
            value = {'status': 'stopped', 'reason': reason,
                     'exception_type': type(error).__name__, 'primary_error': safe_error(error, self.source)}
            if type(error) is CalendarStopped and error.details is not None:
                value['calendar_failure_details'] = error.details
            if self.phase == 'analysis':
                value['stop_summary'] = self.stopped_analysis_summary()
                publication = verified_basic(self.root, self.paths['results_dir'], self._published_basic) if self._published_basic else None
                if publication is not None:
                    value['basic_status'] = 'completed'
                    value['basic_publication'] = publication
                    value['enhancement_sources'] = dict(self._enhancement_status)
        if self.full_analysis:
            add_full_total(value,self.root)
        value.update(phase=self.phase, financial_requests=financial_counts, resume=False,
                     automatic_resume=False, manual_resume_entry=False)
        value['calendar_handoff'] = self.calendar_handoff
        if self._margin_date is not None:
            value['margin_receipt_failed'] = self._margin_receipt_failed
        if self._powershell_news_executable is not None or self._powershell_fundflow_executable is not None:
            value['powershell_transport_receipt_failed'] = self._powershell_receipt_failed
            if self._powershell_news_executable is not None:
                value['powershell_news_receipt_failed'] = self._powershell_receipt_failed
        # A diagnostic write failure is propagated, never reported as successful completion.
        write_once(self.root, 'result.json', value)
        return value


def add_full_total(value,root):
    from tradingagents.full_session_ledger import owner
    current=owner()
    require(current is not None and current.key==root.name,"verified_full_owner_required")
    events=current.snapshot()["events"]
    value["total_external_attempts"]=len(events)
    value["total_financial_attempts"]=sum(row[2]=="financial" for row in events)
    value["physical_count_source"]="session_ledger"

def terminal_receipt(value, root):
    """Keep the terminal envelope small; only add the closed stop projection."""
    receipt = {'status': value['status'], 'phase': value['phase'],
               'result': str(root / 'result.json'), 'resume': False}
    if value.get('physical_count_source')=='session_ledger':
        receipt.update(total_external_attempts=value['total_external_attempts'],physical_count_source='session_ledger')
    if value['status'] == 'stopped' and 'stop_summary' in value:
        receipt['stop_summary'] = value['stop_summary']
        if value['stop_summary'].get('reason') == 'model_transport_failure' and not value.get('basic_publication'):
            receipt['user_message'] = (
                '模型连接中断，本次研究已停止，未产生最终评级。'
                '本次运行目录保留了已写入的草稿、数据覆盖记录和失败回执。'
            )
    return receipt


def safe_error(error, source=SOURCE):
    labels = {'tradingagents/analysis_budget.py': 'analysis_budget', 'cli/main.py': 'cli_main',
              'scripts/analyze_mainland_bounded.py': 'bounded_entry'}
    calls = []
    frame = error.__traceback__
    while frame is not None and len(calls) < 16:
        path = Path(frame.tb_frame.f_code.co_filename).absolute()
        relative = path.relative_to(source).as_posix() if path.is_relative_to(source) else ''
        label = labels.get(relative, 'other')
        calls.append({'source': label, 'line': frame.tb_lineno if label != 'other' else None})
        frame = frame.tb_next
    reason = getattr(error, 'reason', None)
    allowed = {'deadline_exceeded', 'call_limit_exceeded', 'critical_evidence_failure',
               'data_redirect_forbidden', 'model_redirect_forbidden', 'disabled_data_vendor',
               'uncovered_model_transport_settings', 'uncovered_httpx_transport',
               'uncovered_model_destination', 'uncovered_data_transport_settings',
               'uncovered_data_vendor', 'model_output_limit_unbound', 'preparation_network_forbidden'}
    return {'budget_reason': reason if type(reason) is str and reason in allowed else None,
            'call_locations': calls}


def read_user_credentials(*, use_project_env=False):
    if use_project_env:
        from project_credentials import read_project_credentials
        credentials = read_project_credentials(SOURCE)
        try:
            os.environ.update(credentials)
        finally:
            credentials.clear()
        return
    import getpass
    import warnings
    require(sys.stdin.isatty(), 'interactive_hidden_input_required')
    for name, label in (('TUSHARE_TOKEN', 'Tushare Token for market analysis'),
                        ('DEEPSEEK_API_KEY', 'DeepSeek API key for model analysis')):
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass(label + ' (hidden, not saved): ')
        require(type(value) is str and 0 < len(value) <= 512 and value == value.strip()
                and all(32 < ord(c) < 127 for c in value) and not value.startswith(name + '='),
                'hidden_credential_input_invalid')
        os.environ[name] = value
        del value


def parse_arguments(argv=None):
    """Validate explicit selection syntax before credentials, state or imports."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--session-max-calls', type=int, default=60, help='Whole-session physical request ceiling (1 to 60).')
    parser.add_argument('--enhancement', action='append', choices=('stock_individual_fund_flow', 'stock_lhb_detail_em', 'stock_margin_detail_sse', 'stock_margin_detail_szse', 'stock_hot_rank_latest_em', 'stock_hot_keyword_em', 'stock_individual_notice_report', 'stock_zh_a_disclosure_report_cninfo', 'stock_individual_info_em', 'stock_sector_fund_flow_rank', 'stock_info_global_em', 'bank_lpr'), default=None)
    parser.add_argument('--full', action='store_true', help='Select bounded full analysis; requires bound full providers.')
    parser.add_argument('--check-only', action='store_true', help='Only verify reviewed files; no imports, prompts or state.')
    parser.add_argument('--guided', action='store_true', help='Interactive selection; prepare identity in the confirmed session.')
    parser.add_argument('--guided-powershell-executable', help=argparse.SUPPRESS)
    parser.add_argument('--use-project-env', action='store_true',
                        help='Read only TUSHARE_TOKEN and DEEPSEEK_API_KEY from this project .env; never save them.')
    parser.add_argument('--bank-snapshot', help='Explicit completed stock_basic UUID:SHA256; no automatic refresh.')
    parser.add_argument('--ticker', help='Explicit six-digit mainland ticker with .SH/.SS/.SZ; case-insensitive, '
                        'surrounding whitespace allowed; requires --analysis-date.')
    parser.add_argument('--analysis-date', help='Explicit YYYY-MM-DD; requires --ticker. No implicit current date.')
    parser.add_argument('--supplemental-report-period',
                        help='Opt in to display one exact period from this run\'s verified retained bank balance-sheet gap; no extra acquisition.')
    parser.add_argument('--announcement-file', help='Opt-in local PDF basename from the fixed inbox; report only, no model upload.')
    parser.add_argument('--announcement-date', help='Declared publication date for the local PDF, YYYY-MM-DD.')
    parser.add_argument('--announcement-source-url', help='Declared PDF URL; never fetched by this option.')
    parser.add_argument('--moneyflow', action='store_true',
                        help='Opt in to one Tushare net_mf_amount request for 30 calendar days; selected session budget, advisory only.')
    parser.add_argument('--powershell-news', action='store_true',
                        help='One fixed no-profile PowerShell news GET per run; explicit ticker/date required; failure degrades without stdin/retry.')
    parser.add_argument('--powershell-news-executable', help=argparse.SUPPRESS)
    parser.add_argument('--powershell-fundflow', action='store_true',
                        help='Read verified retrospective individual fund-flow ratios after Basic publication. A cache miss sends no request.')
    parser.add_argument('--powershell-fundflow-executable', help=argparse.SUPPRESS)
    parser.add_argument('--observation-cache-mode', choices=('retrospective', 'as_known_on_date', 'disabled'),
                        help='Cache policy for selected margin, PowerShell fundflow and news observations only.')
    parser.add_argument('--fundflow-observation-sha256',
                        help='Normalized observation SHA-256 from an independently verified PowerShell fundflow receipt.')
    parser.add_argument('--news-source-isolation', action='store_true',
                        help='Explicit PowerShell-only partial page mode: discard source-invalid articles, never relax source or sensitive-content checks.')
    parser.add_argument('--margin-date', help='Opt in to one financing-balance observation for an explicit YYYY-MM-DD trading date; must match the verified snapshot, no date fallback.')
    args = parser.parse_args(argv)
    analysis_allowance(0, 0, 0, args.session_max_calls)
    if args.full:
        require(args.ticker and args.analysis_date and not args.guided, 'full_requires_explicit_selection')
        require(not (args.moneyflow or args.margin_date or args.powershell_fundflow), 'duplicate_full_enhancements')
    if args.fundflow_observation_sha256 is not None:
        require(args.powershell_fundflow and re.fullmatch('[0-9a-f]{64}', args.fundflow_observation_sha256),
                'invalid_fundflow_observation_pin')
    if args.enhancement is not None:
        require(args.full and len(set(args.enhancement)) == len(args.enhancement), 'invalid_custom_full_scope')
        require(not args.moneyflow and not args.announcement_file, 'undeclared_custom_enhancement')
        require(not args.powershell_fundflow or 'stock_individual_fund_flow' in args.enhancement, 'unselected_fundflow')
        margin_name='stock_margin_detail_szse' if (args.ticker or '').upper().endswith('.SZ') else 'stock_margin_detail_sse'
        require(not args.margin_date or margin_name in args.enhancement, 'unselected_margin')
    require((args.ticker is None) == (args.analysis_date is None), 'selection_arguments_must_be_paired')
    require(args.powershell_news == (args.powershell_news_executable is not None), 'powershell_news_executable_required')
    require(args.powershell_fundflow == (args.powershell_fundflow_executable is not None), 'powershell_fundflow_executable_required')
    require(not args.powershell_fundflow or args.ticker is not None or args.guided, 'powershell_fundflow_requires_explicit_selection')
    if args.powershell_fundflow:
        executable = Path(args.powershell_fundflow_executable)
        require(executable.is_absolute() and executable.name.lower() == 'pwsh.exe', 'invalid_powershell_executable')
    require(not args.news_source_isolation or args.powershell_news or args.guided, 'news_source_isolation_requires_powershell')
    require(not args.powershell_news or args.ticker is not None or args.guided, 'powershell_news_requires_explicit_selection')
    if args.powershell_news:
        executable = Path(args.powershell_news_executable)
        require(executable.is_absolute() and executable.name.lower() == 'pwsh.exe', 'invalid_powershell_executable')
    if args.supplemental_report_period is not None:
        require(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', args.supplemental_report_period) is not None,
                'invalid_supplemental_report_period')
        try:
            date.fromisoformat(args.supplemental_report_period)
        except ValueError:
            raise Stop('invalid_supplemental_report_period') from None
    if args.ticker is not None:
        token = args.ticker.strip()
        require(token.isascii(), 'invalid_explicit_ticker')
        args.ticker = token.upper()
        require(re.fullmatch(r'[0-9]{6}\.(?:SH|SS|SZ)', args.ticker) is not None,
                'invalid_explicit_ticker')
        require(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', args.analysis_date) is not None,
                'invalid_explicit_analysis_date')
        try:
            requested_date = date.fromisoformat(args.analysis_date)
        except ValueError:
            raise Stop('invalid_explicit_analysis_date') from None
        require(requested_date <= datetime.now(ZoneInfo('Asia/Shanghai')).date(), 'future_analysis_date')
        if args.supplemental_report_period is not None:
            require(date.fromisoformat(args.supplemental_report_period) <= requested_date,
                    'supplemental_period_after_analysis')
    if args.margin_date is not None:
        require(args.ticker is not None, 'margin_requires_explicit_selection')
        require(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', args.margin_date) is not None, 'invalid_margin_date')
        try:
            margin_date = date.fromisoformat(args.margin_date)
        except ValueError:
            raise Stop('invalid_margin_date') from None
        require(date(2010, 1, 1) <= margin_date <= requested_date, 'margin_date_out_of_range')
    announcement_values = (args.announcement_file, args.announcement_date, args.announcement_source_url)
    require(all(value is None for value in announcement_values)
            or all(value is not None for value in announcement_values), 'announcement_arguments_incomplete')
    args.announcement_options = None
    if args.announcement_file is not None:
        require(args.ticker is not None, 'announcement_requires_explicit_selection')
        # This module is stdlib-only at import. Do not import the package/.env
        # before consent; these checks read no PDF and perform no subprocess work.
        import importlib.util

        spec = importlib.util.spec_from_file_location('local_announcement_input', SOURCE / 'tradingagents/local_announcement.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.input_name(args.announcement_file)
            module.declaration(args.ticker[:-3] + '.SH' if args.ticker.endswith('.SS') else args.ticker,
                               args.announcement_date, args.analysis_date, args.announcement_source_url)
        except ValueError:
            raise Stop('invalid_announcement_input') from None
        args.announcement_options = announcement_values
    return args


def guided_input(prompt, *, reader=None):
    try:
        value = (input if reader is None else reader)(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Stop('guided_cancelled') from None
    require(type(value) is str and len(value) <= 80, 'guided_input_invalid')
    value = value.strip()
    require(value != '/cancel', 'guided_cancelled')
    return value


def guided_yes(prompt, *, reader=None):
    value = guided_input(prompt + ' [Y/n，/cancel 取消]: ', reader=reader).lower()
    require(value in {'', 'y', 'n'}, 'guided_choice_invalid')
    return value != 'n'


def collect_guided_selection(args, *, reader=None, today=None):
    """Short console prompts only; not a second analysis or evidence mechanism."""
    require(sys.stdin.isatty(), 'guided_terminal_required')
    today = today or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    print('简洁入口：中文 Basic 四分析员及研究/交易/风控/最终决策；输入 /cancel 取消。')
    if args.ticker is None:
        ticker = guided_input('股票（必须选择，六位代码加 .SH/.SS/.SZ）: ', reader=reader)
        proposed = (today - timedelta(days=1)).isoformat()
        print('默认日期是上海时区昨日的日历截止，不代表交易日或已发布完整行情。'
              '实际有效交易日仍由生产日历与行情校验决定。')
        cutoff = guided_input(f'分析截止日期 YYYY-MM-DD [{proposed}]: ', reader=reader) or proposed
        selected = parse_arguments(['--ticker', ticker, '--analysis-date', cutoff])
        args.ticker, args.analysis_date = selected.ticker, selected.analysis_date
    if not args.moneyflow:
        args.moneyflow = guided_yes('启用资金流（最多一次 POST）？', reader=reader)
    if args.margin_date is None and guided_yes('启用指定交易日融资余额（最多一次 POST）？', reader=reader):
        print('融资日期必须与最终行情快照交易日完全相同；非交易日请填写实际交易日，'
              '不确定可输入 /skip。不自动改期或增加请求。')
        value = guided_input(f'融资交易日 [{args.analysis_date}]，/skip 不启用: ', reader=reader)
        if value != '/skip':
            value = value or args.analysis_date
            checked = parse_arguments(['--ticker', args.ticker, '--analysis-date', args.analysis_date,
                                       '--margin-date', value])
            args.margin_date = checked.margin_date
    if not args.powershell_news:
        executable = Path(args.guided_powershell_executable or '')
        require(executable.is_absolute() and executable.name.lower() == 'pwsh.exe', 'invalid_powershell_executable')
        if executable.is_file():
            regular_path(executable)
        else:
            print('PowerShell 不可用，新闻将标为不可用；不回退 Requests 或浏览器。')
        args.powershell_news = True
        args.powershell_news_executable = str(executable)
    print('新闻固定选择 PowerShell：最多一次公开 GET；失败按原规则降级，不切换供应商。')
    if not args.use_project_env:
        args.use_project_env = guided_yes('使用项目既有 .env 凭据（不显示、不复制）？', reader=reader)
    print(f'已选择：{args.ticker} / {args.analysis_date}；中文 Basic；PowerShell 新闻；'
          f'资金流：{"启用" if args.moneyflow else "关闭"}；融资日期：{args.margin_date or "不启用"}。')
    return args


def prepare_guided_identity(args, *, source=SOURCE, discover=None, refresh=None, clock=None):
    """Reuse validated CURRENT metadata; refresh requires the updater's own consent.

    Return the number of physical sends to deduct before the graph starts.
    """
    from tradingagents.dataflows import bank_identity_snapshot as bank
    known, _, _ = bank._base(source, args.ticker)
    if known:
        print('身份：使用正式注册表；不发送身份请求。')
        return 0
    if args.bank_snapshot is not None:
        bank._overlay_inputs(source, args.ticker, args.analysis_date, args.bank_snapshot, bank.current_time())
        print('身份：显式快照已校验，正式会话仍会重新绑定并检查有效期。')
        return 0
    reference, state = (discover or bank.find_current_reference)(source, args.ticker, args.analysis_date)
    if state == 'refresh_required':
        print('身份：没有有效同股快照；接下来是独立单股更新，最多一次 stock_basic POST，'
              '单股查询计入本次所选总次数和 1200 秒上限，沿用原限流和零重试。'
              '身份查询需要单独确认；取消或失败即停止本次会话。')
        if refresh is None:
            from update_bank_identity import main as refresh
        arguments = ['--refresh-security', args.ticker]
        if args.use_project_env:
            arguments.append('--use-project-env')
        require(clock is not None, 'session_supervisor_required')
        clock.remaining()
        result = refresh(arguments, return_result=True, session_clock=clock)
        require(type(result) is dict and result.get('status') == 'exact_positive'
                and type(result.get('physical_attempts')) is int and result['physical_attempts'] == 1,
                'bank_refresh_failed')
        reference = result.get('snapshot_ref')
    require(reference is not None, 'bank_identity_reference_missing')
    bank._overlay_inputs(source, args.ticker, args.analysis_date, reference, bank.current_time())
    args.bank_snapshot = reference
    print('身份：同股快照已校验，正式会话重新绑定；CURRENT 身份不证明历史时点可得。')
    return int(state == 'refresh_required')


def check_required_local_stores(source):
    """Presence only, before consent; the later storage/identity checks still bind."""
    market = Path(source) / 'data/market_history/market_history.sqlite3'
    coordinator = market.with_name(market.name + '.provider-requests.sqlite3')
    for path, reason in ((market, 'local_market_store_unavailable'),
                         (coordinator, 'local_coordinator_store_unavailable')):
        try:
            regular_path(path)
        except Stop as error:
            if str(error) == 'missing_bound_file':
                raise Stop(reason) from None
            raise


def selection_error_hint(error):
    """Fixed input guidance only; never echo arbitrary exception or input text."""
    if type(error) is not Stop:
        return None
    return {
        'selection_arguments_must_be_paired': 'Provide both --ticker and --analysis-date, or omit both for the menu.',
        'invalid_explicit_ticker': 'Use six ASCII digits with an explicit .SH, .SS or .SZ suffix, '
                                   'for example 600919.SH or 000001.SZ; bare codes are ambiguous here.',
        'invalid_explicit_analysis_date': 'Use a real calendar date in YYYY-MM-DD format for --analysis-date.',
        'future_analysis_date': '--analysis-date must not be later than today in Asia/Shanghai.',
        'invalid_supplemental_report_period': 'Use a real calendar date in YYYY-MM-DD format for --supplemental-report-period.',
        'supplemental_period_after_analysis': '--supplemental-report-period must be on or before --analysis-date; '
                                             'omit it if no exact supplemental period is intended.',
        'local_market_store_unavailable': 'Required data/market_history/market_history.sqlite3 is missing or not a regular file. '
                                          'Restore the verified project store or arrange reviewed initialization; do not create an empty file. '
                                          'See docs/bounded-mainland-entry.md.',
        'local_coordinator_store_unavailable': 'Required data/market_history/market_history.sqlite3.provider-requests.sqlite3 '
                                               'is missing or not a regular file. Restore the verified matching coordinator or arrange '
                                               'reviewed initialization; do not create an empty file. See docs/bounded-mainland-entry.md.',
    }.get(str(error))


def resolve_full_arguments(argv,source,manifest,now):
    argv = list(argv)
    date_module = None
    if '--full' in argv and '--check-only' not in argv:
        import runpy
        module_key = "scripts/full_date_evidence.py"
        require(any(x["root"]=="project" and x["relative"].replace(chr(92),"/")==module_key for x in manifest["files"]), "full_date_module_unbound")
        date_module=runpy.run_path(str(source/module_key))
        # Accept one literal value from the supported PowerShell/Python switch.
        require(argv.count("--analysis-date")==1,"explicit_date_or_latest_required")
        idx=argv.index("--analysis-date")+1
        require(idx<len(argv) and not any(a.startswith("--analysis-date=") for a in argv),"ambiguous_date")
        load=date_module["bound"]
        request=argv[idx]
        require(argv.count('--ticker')==1 and argv.index('--ticker')+1<len(argv) and not any(a.startswith('--ticker=') for a in argv),'explicit_ticker_required')
        wire=argv[argv.index('--ticker')+1]
        require(re.fullmatch(r'[0-9]{6}\.(SH|SS|SZ)',wire) is not None,'unsupported_or_ambiguous_ticker')
        ticker=wire.replace('.SH','.SS')
        calendar_key='config/full_evidence/shenzhen-calendar.json' if ticker.endswith('.SZ') else 'config/full_evidence/shanghai-calendar.json'
        market_key='config/full_evidence/market-releases.json' if ticker=='601328.SS' else f'config/full_evidence/{ticker}/market-releases.json'
        record_key=f"config/full_evidence/XSHG/{request}/completed-day.json"
        reviewed={row["relative"].replace(chr(92),"/") for row in manifest["files"] if row["root"]=="project"}
        if request!="latest" and ticker.endswith(".SS") and calendar_key not in reviewed and record_key in reviewed:
            selected=date_module["completed_historical_day"](source,manifest,record_key,request,"XSHG",now)
            args=parse_arguments(argv)
            require(args.ticker.replace(".SH",".SS")==ticker and args.analysis_date==selected,"unreviewed_full_selection")
            require(args.observation_cache_mode!="as_known_on_date","strict_full_evidence_not_qualified")
            return args,date_module
        calendar=load(source,manifest,calendar_key)
        market=load(source,manifest,market_key) if request=="latest" else {"ticker":ticker,"source":"explicit_selection","rows":[]}
        require(date_module["calendar_matches"](source,manifest,calendar),"unbound_calendar_original")
        require(market.get("ticker")==ticker,"wrong_market_release_security")
        qualified = []
        for row in market["rows"]:
            observed = date_module["observed_close"](source,manifest,market,row)
            if observed:
                if observed["published_at"] is not None:
                    observed["basis"] = "published"
                qualified.append(observed)
        market=dict(market,rows=qualified)
        selected=date_module["resolve"](request,now,calendar,market)
        argv[idx]=selected
        if request == "latest":
            eligible = [r for r in qualified if r["trading_date"] == selected and
                        date_module["instant"](r["first_observed_at"]) <= now and
                        date_module["eligible_release"](r, calendar, now,ticker)]
            date_module["selection_receipt"] = min(eligible, key=lambda r: date_module["instant"](r["first_observed_at"]))
    args=parse_arguments(argv)
    if args.full and not args.check_only:
        require(date_module is not None and args.analysis_date==selected and args.ticker.replace('.SH','.SS')==ticker,'unreviewed_full_selection')
        require(args.observation_cache_mode != 'as_known_on_date','strict_full_evidence_not_qualified')
    return args,date_module

FULL_BOUND_FILES = ('tradingagents/powershell_fundflow.py', 'scripts/accept_financial_statements.py', 'scripts/analyze_mainland_bounded.py', 'scripts/daily_registration.py', 'scripts/daily_retention.py', 'scripts/full_date_evidence.py', 'scripts/full_financial_channel.py', 'tradingagents/analysis_budget.py', 'tradingagents/dataflows/bank_identity_acquisition.py', 'tradingagents/full_dispatch.py', 'tradingagents/full_factory.py', 'tradingagents/full_matrix.py', 'tradingagents/full_session_ledger.py', 'tradingagents/tushare_financial_https.py', 'tradingagents/tushare_https.py')

def research_policy(manifest):
    bound={row["relative"]:row["sha256"] for row in manifest["files"] if row["root"]=="project"}
    path="config/portfolio_research_activation.json"
    require(path in bound,"research_publication_unavailable")
    raw=(SOURCE/path).read_bytes()
    require(hashlib.sha256(raw).hexdigest()==bound[path],"research_activation_changed")
    policy=json.loads(raw)
    require(policy["mode"]=="portfolio-research-v3" and policy["budget"]==[60,3,1200]
            and policy["search_enabled"] is False and policy["sources"].get("scripts/research_launch.py")
            and all(bound.get(k)==v for k,v in policy["sources"].items()),"research_activation_unbound")
    return policy


def main(clock=None, run_id=None):
    require(sys.flags.isolated and sys.dont_write_bytecode, 'python_flags')
    try:
        manifest = read_json(SOURCE / 'config/bounded_mainland_release.json')
    except Stop as error:
        if str(error) == 'missing_bound_file':
            error.diagnostic = 'reviewed_install_manifest_missing'
        raise
    verify_release(manifest)
    require(Path(sys.executable).resolve() == SOURCE / '.venv/Scripts/python.exe', 'python_identity')
    if '--auto-latest' in sys.argv[1:]:
        require(sys.argv.count('--auto-latest')==1,'ambiguous_intake')
        require(any(row['root']=='project' and row['relative']=='scripts/full_latest_intake.py'
                    for row in manifest['files']),'full_intake_unbound')
        import runpy
        from types import SimpleNamespace
        run=runpy.run_path(str(SOURCE/'scripts/full_latest_intake.py'))['run']
        return run(SimpleNamespace(**globals()),[v for v in sys.argv[1:] if v!='--auto-latest'],clock,run_id,manifest)
    if '--full' in sys.argv:
        approved={row['relative'].replace(chr(92),'/') for row in manifest['files'] if row['root']=='project'}
        require(set(FULL_BOUND_FILES)<=approved,'full_owner_sources_unbound')
    args,date_module=resolve_full_arguments(sys.argv[1:],SOURCE,manifest,datetime.now(ZoneInfo("Asia/Shanghai")))
    if args.full:
        # Full acquisition has no approved announcement/policy destination binding yet.
        required = {'tradingagents/' + name + '.py' for name in ('full_matrix','full_factory','full_notice','notice_pages','full_lpr','full_publication','scoped_bulletin','analysis_budget','budget_data')}
        required.update(FULL_BOUND_FILES)
        required.add("tradingagents/full_pair_producer.py")
        require(required <= {row['relative'].replace(chr(92),'/') for row in manifest['files'] if row['root']=='project'}, 'full_provider_binding_unavailable')
    if args.check_only:
        if args.ticker in ('601328.SH', '601328.SS') and args.analysis_date == '2026-11-13':
            print(json.dumps(dict(status='full_preflight_blocked' if args.full else 'source_preflight_blocked',
                                  source_integrity='passed', selection_errors=['prior_dispatch_unreconciled'],
                                  identity='not_checked', provider_inputs='not_checked',
                                  runtime_metadata_checked=False, ready_for_live=False)))
            return 2
        if args.full:
            selection = [v for v in sys.argv[1:] if v != '--check-only']
            problems = []
            if not re.fullmatch(r'[0-9]{6}\.(SH|SS|SZ)',args.ticker or ''):
                problems.append('unreviewed_full_selection')
            try:
                resolve_full_arguments(selection, SOURCE, manifest, datetime.now(ZoneInfo("Asia/Shanghai")))
            except (ValueError, RuntimeError, OSError, KeyError) as error:
                problems.append(str(error))
            print(json.dumps(dict(status='full_preflight_blocked' if problems else 'full_selection_verified',
                                  source_integrity='passed', selection_errors=problems,
                                  identity='not_checked', provider_inputs='not_checked',
                                  runtime_metadata_checked=False, ready_for_live=None if not problems else False)))
            return 2 if problems else 0
        print(json.dumps({'status': 'source_preflight_passed', 'runtime_metadata_checked': False}))
        return 0
    import ctypes
    require(not ctypes.windll.shell32.IsUserAnAdmin(), 'non_admin_required')
    if args.guided:
        args = collect_guided_selection(args)
    if args.ticker in ('601328.SH', '601328.SS') and args.analysis_date == '2026-11-13':
        raise Stop('prior_dispatch_unreconciled')
    # Only explicit selection with local file credentials can use a pipe.
    # Hidden credential input and the screen-buffer menu still require a TTY.
    require(sys.stdin.isatty() or (args.use_project_env and args.ticker is not None),
            'interactive_terminal_required')
    require(not os.environ.get('TRADINGAGENTS_PROTECTION_CONTEXT'), 'bound_protection_context')
    require(os.environ.get('TRADINGAGENTS_OFFLINE_TEST_DISABLE_LOCAL_PROFILE', '0') == '0', 'offline_test_mode')
    check_required_local_stores(SOURCE)
    if args.guided:
        # Prevent package import from reading dotenv before the updater's explicit consent.
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        sys.path.insert(0, str(SOURCE))
        sys.path.insert(0, str(SOURCE / 'scripts'))
        print('START 建立本次会话；如所选股票需要更新身份，随后单独确认至多一次身份查询。')
    if args.full:
        print('Full public sources: np-anotice-stock.eastmoney.com (up to 2 pages), www.chinamoney.com.cn (history and dated article); all within the same limit. Retrospective results are labeled.')
    print('Local prerequisites: required files present; schema, identity and coverage are not yet verified.')
    print('One selected mainland stock/date; four analysts, Chinese shallow 1+1.\n'
          f'All external attempts <={args.session_max_calls} including identity, calendar, data and models; financial preparation <=3 within that total.\n'
          'Failed sends and format repairs count; unused calendar allowance stays with analysis.\n'
          'Calendar preparation: reuse verified coverage, or <=1 full-window SSE trade_cal send.\n'
          'Each preparation send reduces the remaining graph allowance.\n'
          'START includes calendar payload/revision and shared coordinator/WAL/SHM writes; no retries.\n'
          'DeepSeek Flash/Pro high, max output 65536, SDK retries 0; shared 1200s including calendar+finance.\n'
          'At most TWO controlled model regenerations per session, at most ONE per logical call, after a confirmed HTTP200 incomplete-chunked read; all sends count, same deadline.\n'
          'No resume, no money/input-token cap. The owned Windows job ends at the session deadline.\n'
          'Secrets go only to api.waditu.com / api.deepseek.com; public Eastmoney routes remain limited.\n'
          'New budget/checkpoint/report and necessary shared cache writes; old runs preserved.')
    print(f'Financial statements: {DESTINATION}; cache first, maximum age '
          f'{FIXED["tushare_financial_cache_max_age_seconds"]} seconds.\n'
          'Missing or expired coverage may require financial requests within the stated limit; '
          'cache availability is not evidence of missing response fields.')
    if args.supplemental_report_period is not None:
        print(f'Supplemental report period: {args.supplemental_report_period}; display only, no extra acquisition.\n'
              'It must bind to this session\'s verified response for the selected stock and exact period. '
              'If that binding is unavailable, this session stops after preparation; omit the option if not intended.\n'
              'Supplemental amounts do not fill missing financial fields or enter the investment decision.')
    if args.moneyflow:
        print('Moneyflow enhancement: one optional Tushare moneyflow POST for this stock and 30 calendar days; '
              'fields ts_code,trade_date,net_mf_amount only; uses the selected session budget and deadline. '
              'Validated numeric observations go to the sentiment analyst and report as auxiliary trading-flow data, '
              'not news, holdings, predictions or decision facts. No retry/pagination or other enhancements. '
              'Account access and full date coverage are not guaranteed; historical retrieval is retrospective.')
    if args.full and any(x["relative"].replace(chr(92),"/")=="config/ordinary-full-pair-policy.json" for x in manifest["files"] if x["root"]=="project"):
        print("Full retrospective: financing may use one bounded Tushare POST. The individual fund-flow ratio uses verified cached evidence only; missing evidence is reported as unavailable.")
    if args.margin_date is not None:
        print(f'Financing balance: one optional Tushare margin_detail POST for {args.margin_date}; '
              'fields trade_date,ts_code,rzye only, unit CNY yuan. This explicitly selected date must equal '
              'the verified snapshot trading date or no query is sent. No latest-date fallback. '
              'Uses the existing selected session budget/deadline; numeric advisory context and report only. '
              'Not net flow, a trend or decision evidence. Account access/real response compatibility unverified; '
              'retrospective data is not proof of availability by the cutoff. No retry or pagination.')
    if args.announcement_options is not None:
        print('Local company-announcement appendix: ONE original inbox PDF re-read only at report export, '
              'bounded local process, no download/model upload. Source and historical availability remain unverified.\n'
              'An invalid appendix is excluded with a fixed reason; no old-JSON fallback, news-count or rating change. '
              'The existing shared deadline is not extended.')
    if args.ticker is not None:
        display_symbol = args.ticker[:-3] + '.SS' if args.ticker.endswith('.SH') else args.ticker
        print(f'Explicit selection: {display_symbol} (input: {args.ticker}; identity not yet verified) / '
              f'{args.analysis_date}; {"Full" if args.full else "Basic"}; '
              'market, social, news, fundamentals; Chinese; 1 debate / 1 risk.')
        if date.fromisoformat(args.analysis_date) == datetime.now(ZoneInfo('Asia/Shanghai')).date():
            print('Mainland same-day data may be incomplete until markets close and vendors finish publishing.')
    print('Credential input: fixed project .env (two keys only, not saved elsewhere).'
          if args.use_project_env else 'Credential input: hidden once per key; Tushare reused in this session only (not saved).')
    if args.powershell_news:
        print('新闻：本次最多一次 PowerShell Invoke-WebRequest GET，计入所选共享预算。'
              '固定脚本、无 profile、无重试或重定向；普通新闻失败标为不可用，不回退 Requests 或浏览器。'
              '预算、取消、权限或完整性失败仍停止；股票/日期/来源校验和历史回补限制保持。')
        print('来源策略：逐条排除来源无效文章；全局/敏感校验失败仍整批拒绝；报告注明部分覆盖。'
              if args.news_source_isolation else '来源策略：严格模式，任一来源异常整批拒绝。')
    if args.powershell_fundflow:
        print('个股资金流：仅复用与本次证券、身份和日期匹配的合格回顾性缓存，零资金流外部调用。'
              '缺少合格缓存时标记不可用，保留已发布的 Basic；不启动请求子进程，不重试。'
              '窗口为截至日及之前 7 个日历日。只采用已核实单位的净占比和涨跌幅（%）；金额和收盘价单位未核实，排除。'
              '不是财报指标，不与 Tushare 全单净额合并，不改变评级；事后回补不证明历史时点可得。')
    require(input('Start ONE new billable session with these limits? Type START: ') == 'START', 'not_confirmed')
    require(clock is not None, 'session_supervisor_required')
    clock.remaining()
    root = reserve_session(SOURCE / 'reports/bounded-runs', run_id=run_id)
    if not args.full:
        return run_confirmed(args,root,clock,manifest,date_module)
    os.environ['PYTHON_DOTENV_DISABLED']='1'
    sys.path.insert(0,str(SOURCE));sys.path.insert(0,str(SOURCE/'scripts'))
    from tradingagents.full_session_ledger import Ledger,bind
    from full_financial_channel import digest,binding
    params=json.loads(json.dumps(vars(args),sort_keys=True,default=os.fspath,allow_nan=False))
    params['ticker']=params['ticker'].replace('.SS','.SH')
    params['requested_date']=sys.argv[sys.argv.index('--analysis-date')+1]
    if 'selection_receipt' in date_module:
        params['date_selection_sha256']=digest(date_module['selection_receipt'])
    check=dict(source=str(SOURCE),run_id=root.name,params=params,params_sha256=digest(params),
               account=FIXED['tushare_account_scope_label'])
    write_once(root,'full-owner.json',check)
    require(binding(SOURCE,root)==check,'full_run_binding')
    owned=Ledger(root/'physical-ledger',root.name,params)
    with bind(owned):
        outcome='failed'
        try:
            code=run_confirmed(args,root,clock,manifest,date_module)
            outcome='completed' if code==0 else 'failed'
            return code
        finally:
            final_hash=owned.finish(outcome)
            write_once(root,'physical-total.json',dict(total_external_attempts=len(owned.snapshot()['events']),
                ledger_sha256=final_hash,run_id=root.name,params_sha256=check['params_sha256']))

def run_confirmed(args,root,clock,manifest,date_module):
    ledger_module = sys.modules.get("tradingagents.full_session_ledger")
    current = ledger_module.owner() if ledger_module is not None else None
    if getattr(args, "research_mode", None) is not None or (current is not None and "decision_mode" in current.params):
        require(args.full, "research_requires_latest_full")
        from tradingagents.portfolio_research_run import confirmed_research_scope
        with confirmed_research_scope(args, root, date_module.get("selection_receipt")):
            return _run_confirmed(args,root,clock,manifest,date_module)
    return _run_confirmed(args,root,clock,manifest,date_module)


def _run_confirmed(args,root,clock,manifest,date_module):
    identity_calls = 0
    if args.guided:
        try:
            if args.full:
                from tradingagents.full_session_ledger import preparation_role
            with preparation_role("identity") if args.full else contextlib.nullcontext():
                identity_calls = prepare_guided_identity(args, clock=clock)
        except BaseException:
            stopped={'status':'stopped','phase':'identity','resume':False}
            if args.full:add_full_total(stopped,root)
            write_once(root,'result.json',stopped)
            raise
    session = Session(root, full_analysis=args.full, session_max_calls=args.session_max_calls, use_project_env=args.use_project_env, bank_snapshot=args.bank_snapshot,
                      calendar_authorized=True, supplemental_report_period=args.supplemental_report_period,
                      announcement_options=args.announcement_options, moneyflow=args.moneyflow,
                      margin_date=args.margin_date,
                      powershell_news_executable=args.powershell_news_executable,
                      news_source_isolation=args.news_source_isolation,
                      powershell_fundflow_executable=args.powershell_fundflow_executable,
                      observation_cache_mode=args.observation_cache_mode,
                      fundflow_observation_sha256=args.fundflow_observation_sha256)
    session.equity_reference = getattr(args,"equity_reference",None)
    if args.full:
        session.date_layer = (date_module, manifest)
        if "selection_receipt" in date_module:
            write_once(session.root, "date-selection.json", date_module["selection_receipt"])
    session.clock = clock
    session.identity_calls = identity_calls
    write_once(root, 'credential-mode.json', {'mode': 'project_env' if args.use_project_env else 'hidden',
                                          'values_retained': False})
    os.environ.update(PYTHON_DOTENV_DISABLED='1', LANGCHAIN_TRACING_V2='false', LANGCHAIN_TRACING='false',
                      LANGSMITH_TRACING='false', OTEL_SDK_DISABLED='true', TRADINGAGENTS_LLM_PROVIDER='deepseek',
                      TRADINGAGENTS_QUICK_THINK_LLM='deepseek-flash', TRADINGAGENTS_DEEP_THINK_LLM='deepseek-v4-pro',
                      TRADINGAGENTS_LLM_BACKEND_URL='https://api.deepseek.com', TRADINGAGENTS_OUTPUT_LANGUAGE='Chinese',
                      TRADINGAGENTS_MAX_DEBATE_ROUNDS='1', TRADINGAGENTS_MAX_RISK_ROUNDS='1')
    for name in ('TUSHARE_TOKEN', 'DEEPSEEK_API_KEY'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(SOURCE))
    sys.path.insert(0, str(SOURCE / 'scripts'))
    os.chdir(SOURCE)
    try:
        from tradingagents.default_config import DEFAULT_CONFIG, require_runtime_profile_loading
        require_runtime_profile_loading()
        if args.full:
            prefix="config/full_evidence/" if args.analysis_date=="2026-11-20" else "config/full_evidence/"+args.analysis_date+"/"
            code=args.ticker[:6]
            if code!="601328":prefix=f"config/full_evidence/{code}/{args.analysis_date}/"
            docs = (prefix+code+"-"+args.analysis_date.replace("-","")+"-bulletin.json",
                    prefix+code+"-"+args.analysis_date.replace("-","")+"-bulletin-review.json")
            bound = {row["relative"].replace(chr(92), "/"):row["sha256"] for row in manifest["files"] if row["root"]=="project"}
            from tradingagents.full_matrix import FIELDS,FLOW,margin_source
            from tradingagents.full_pair_producer import load_policy
            session.pair_runtime=load_policy(SOURCE,bound)
            session.selected_enhancements=args.enhancement
            session.full_local_sources={}
            for name in (*FIELDS,FLOW,margin_source(args.ticker.upper())):
                path=prefix+name+".json"
                raw=path[:-5]+".bin"
                if path in bound or raw in bound:
                    require(path in bound and raw in bound,"source_projection_binding_incomplete")
                    session.full_local_sources[name]=(SOURCE/path,bound[path])
            if any(name in bound for name in docs):
                require(all(name in bound for name in docs), "incomplete_bulletin_binding")
                from tradingagents.scoped_bulletin import ReviewedBulletin
                contents=[]
                for name in docs:
                    body=(SOURCE/name).read_bytes()
                    require(hashlib.sha256(body).hexdigest()==bound[name], "bulletin_binding_changed")
                    contents.append(body)
                session.full_bulletin = ReviewedBulletin(contents[0],json.loads(contents[1]))
        config = session.configure(DEFAULT_CONFIG)
        read_user_credentials(use_project_env=args.use_project_env)
        session.clock.remaining()
        write_once(root, 'session-deadline.json', {'started_epoch': session.clock.started_epoch,
                   'deadline_epoch': session.clock.deadline_epoch,
                   'deadline_monotonic': session.clock.deadline_monotonic, 'maximum_seconds': 1200,
                   'start_boundary': 'supervisor_before_worker_launch',
                   'finance_included': True, 'cooperative_not_hard_kill': False})
        import cli.main as cli
        original_config = cli.DEFAULT_CONFIG
        cli.DEFAULT_CONFIG = config
        try:
            requested = None if args.ticker is None else (args.ticker, args.analysis_date)
            value = session.execute(cli, config, verify_sources=lambda: verify_release(manifest),
                                    requested_selection=requested)
        finally:
            cli.DEFAULT_CONFIG = original_config
        print(json.dumps(terminal_receipt(value, root), ensure_ascii=False))
        return 0 if value['status'] in ('completed','custom_complete') else 1
    except BaseException as error:
        if not (root / 'result.json').exists():
            from project_credentials import credential_failure_code
            stopped={'status':'stopped','phase':'configuration_or_hidden_input',
                     'exception_type':type(error).__name__,'resume':False,
                     'credential_failure':credential_failure_code(error),'primary_error':safe_error(error)}
            if args.full:add_full_total(stopped,root)
            write_once(root,'result.json',stopped)
        raise
    finally:
        for name in ('TUSHARE_TOKEN', 'DEEPSEEK_API_KEY'):
            os.environ.pop(name, None)


def supervise(command, deadline, *, session_root=None, research_ticket=None):
    """Assign a suspended child before its first instruction; retain the only job handle."""
    import _winapi
    import ctypes
    import msvcrt
    from ctypes import wintypes as wt

    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wt.DWORD), ('min_working_set', ctypes.c_size_t),
                    ('max_working_set', ctypes.c_size_t), ('active_limit', wt.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wt.DWORD), ('scheduling', wt.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io_counters', ctypes.c_uint64 * 6),
                    ('memory_limits_and_peaks', ctypes.c_size_t * 4)]

    class Accounting(ctypes.Structure):
        _fields_ = [('times', ctypes.c_int64 * 4), ('page_faults', wt.DWORD), ('total', wt.DWORD),
                    ('active', wt.DWORD), ('terminated', wt.DWORD)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    api.CreateJobObjectW.restype = wt.HANDLE
    api.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    api.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    api.ResumeThread.argtypes = [wt.HANDLE]
    api.ResumeThread.restype = wt.DWORD
    api.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    api.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                             ctypes.POINTER(wt.DWORD)]
    api.CloseHandle.argtypes = [wt.HANDLE]
    job = api.CreateJobObjectW(None, None)  # unnamed, non-inheritable handle
    require(job, 'session_job_create_failed')
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway
    with contextlib.ExitStack() as handles:
        handles.callback(_winapi.CloseHandle, job)
        require(api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
                'session_job_setup_failed')
        current = _winapi.GetCurrentProcess()
        with contextlib.ExitStack() as streams:
            standard = []
            for stream in (sys.stdin, sys.stdout, sys.stderr):
                handle = _winapi.DuplicateHandle(current, msvcrt.get_osfhandle(stream.fileno()), current,
                                                 0, True, _winapi.DUPLICATE_SAME_ACCESS)
                streams.callback(_winapi.CloseHandle, handle)
                standard.append(handle)
            # Legacy console handles cannot appear in the inherited handle list.
            inherited = [h for h in standard if h & 3 != 3 or _winapi.GetFileType(h) != _winapi.FILE_TYPE_CHAR]
            if research_ticket is not None:
                from multiprocessing.connection import Pipe
                reader,writer=Pipe(duplex=False)
                streams.enter_context(reader)
                streams.enter_context(writer)
                os.set_handle_inheritable(reader.fileno(),True)
                inherited.append(reader.fileno())
                command[command.index("SUPERVISOR_PIPE_HANDLE")]=str(reader.fileno())
                writer.send_bytes(research_ticket)
            startup = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESTDHANDLES,
                hStdInput=standard[0], hStdOutput=standard[1], hStdError=standard[2],
                lpAttributeList={'handle_list': inherited} if inherited else None)
            require(time.monotonic() < deadline, 'session_deadline_exceeded')
            process, thread, _, _ = _winapi.CreateProcess(command[0], subprocess.list2cmdline(command),
                None, None, bool(inherited), 0x4, None, None, startup)  # CREATE_SUSPENDED
        handles.callback(_winapi.CloseHandle, process)
        handles.callback(_winapi.CloseHandle, thread)
        if not api.AssignProcessToJobObject(job, process):
            _winapi.TerminateProcess(process, 1)  # still suspended; no descendants
            raise Stop('session_job_assignment_failed')
        require(time.monotonic() < deadline, 'session_deadline_exceeded')
        require(api.ResumeThread(thread) == 1, 'session_child_resume_failed')
        wait_ms = max(0, int((deadline - time.monotonic()) * 1000))
        status = _winapi.WaitForSingleObject(process, wait_ms)
        if status == _winapi.WAIT_OBJECT_0 and time.monotonic() < deadline:
            return _winapi.GetExitCodeProcess(process)
        require(status in (_winapi.WAIT_TIMEOUT, _winapi.WAIT_OBJECT_0), 'session_wait_failed')
        require(api.TerminateJobObject(job, 124), 'session_job_termination_failed')
        cleanup_until = time.monotonic() + 5
        accounting = Accounting()
        job_empty = False
        while api.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            job_empty = accounting.active == 0
            if job_empty or time.monotonic() >= cleanup_until:
                break
            time.sleep(.01)
        # Job accounting can reach zero before the launcher handle signals.
        wait_ms = max(0, int((cleanup_until - time.monotonic()) * 1000))
        exited = _winapi.WaitForSingleObject(process, wait_ms) == _winapi.WAIT_OBJECT_0
        receipt = {'status': 'deadline_exceeded', 'job_termination_requested': True, 'resume': False,
                   'owned_process_exit_confirmed': exited, 'job_empty_confirmed': job_empty}
        if session_root is not None and (session_root / 'session.json').is_file():
            record = read_json(session_root / 'session.json')
            require(record == {'contract': 'bounded-mainland-session-v1', 'run_id': session_root.name,
                    'root': str(session_root), 'resume': False}, 'supervisor_session_mismatch')
            if exited and job_empty and (session_root/'full-owner.json').exists():
                # Read only after Job termination. Never manufacture zero from a missing ledger.
                try:
                    import sqlite3
                    spec=read_json(session_root/'full-owner.json')
                    path=session_root/'physical-ledger/ledger.sqlite3'
                    require(path.resolve()==path and path.is_file() and path.stat().st_nlink==1
                            and spec['run_id']==session_root.name and spec['source']==str(SOURCE),
                            'supervisor_ledger_binding')
                    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                        bound=db.execute('select params from requests where id=?',(session_root.name,)).fetchone()
                        canonical=json.dumps(spec['params'],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
                        require(bound==(hashlib.sha256(canonical.encode()).hexdigest(),),'supervisor_ledger_binding')
                        receipt.update(total_external_attempts=db.execute('select count(*) from sends').fetchone()[0],
                                       physical_count_source='session_ledger')
                except (OSError,ValueError,KeyError,sqlite3.Error,Stop):
                    receipt['physical_count_source']='unverified'
            if exited and job_empty:
                publication = verified_basic(session_root, session_root / 'analysis')
                if publication is not None:
                    receipt.update(basic_status='completed', basic_publication=publication,
                                   cleanup_status='interrupted', enhancement_completion='unverified')
            write_once(session_root, 'supervisor-result.json', {'run_id': session_root.name, **receipt})
        if session_root is not None:
            print(json.dumps({'run_id': session_root.name, **receipt}), flush=True)
        return 124


def cli_exit(clock=None, run_id=None):
    try:
        return main(clock, run_id)
    except Exception as error:
        # Do not print an arbitrary upstream exception body or a traceback containing inputs.
        hint = selection_error_hint(error)
        if hint is not None:
            print(hint, file=sys.stderr)
        reason = str(error) if type(error) is Stop else 'entry_error'
        details = {'status': 'stopped', 'exception_type': type(error).__name__, 'reason': reason}
        diagnostic = getattr(error, 'diagnostic', None) if type(error) is Stop else None
        if diagnostic in {'reviewed_install_manifest_missing', 'bound_runtime_mismatch', 'bound_project_mismatch'}:
            details['diagnostic'] = diagnostic
        elif reason in {'installation_root_mismatch', 'python_identity'}:
            details['diagnostic'] = 'installation_identity_mismatch'
        elif reason in {'local_market_store_unavailable', 'calendar_coverage_missing', 'security_close_missing',
                        'no_completed_session', 'verified_close_time_missing', 'calendar_not_observed',
                        'prepared SSE calendar range incomplete', 'prepared SZSE calendar range incomplete'}:
            details['diagnostic'] = 'local_market_evidence_missing'
        print(json.dumps(details))
        return 1


if __name__ == '__main__':
    if '--check-only' in sys.argv[1:]:
        raise SystemExit(cli_exit())
    started = (time.monotonic(), time.time())
    run_id = uuid.uuid4().hex
    if '--five-snapshots-once' in sys.argv:
        raise Stop("historical_snapshot_launch_unsupported")
    session_root = SOURCE / 'reports/bounded-runs' / run_id
    # Pass only the shared clock origin and original options. Credentials and
    # interactive input remain on the worker's existing standard handles.
    bootstrap = ('import runpy,sys; path=sys.argv.pop(1); '
                 'start=(float(sys.argv.pop(1)),float(sys.argv.pop(1))); '
                 'run_id=sys.argv.pop(1); m=runpy.run_path(path); '
                 'sys.exit(m["cli_exit"](m["SharedSessionClock"](started=start),run_id))')
    command = [sys.executable, '-I', '-B', '-c', bootstrap, str(Path(__file__).resolve()),
               *(str(value) for value in started), run_id, *sys.argv[1:]]
    ticket=None
    if "--decision-mode" in sys.argv[1:]:
        release=read_json(SOURCE/"config/bounded_mainland_release.json")
        verify_release(release)
        policy=research_policy(release)
        from runpy import run_path
        launch=run_path(str(SOURCE/"scripts/research_launch.py"))
        receipt=launch["envelope"](SOURCE,release,sys.argv[1:],run_id,started[0]+1200)
        ticket=launch["canonical"](receipt)
        # Keep the handle separate from user arguments and out of the environment.
        command[4]=bootstrap.replace('run_id=sys.argv.pop(1);','run_id=sys.argv.pop(1); pipe=int(sys.argv.pop(1)); ').replace(
            'sys.exit(m["cli_exit"](m["SharedSessionClock"](started=start),run_id))',
            'clock=m["SharedSessionClock"](started=start); clock.research_pipe=pipe; sys.exit(m["cli_exit"](clock,run_id))')
        command.insert(9,"SUPERVISOR_PIPE_HANDLE")
    try:
        require(not session_root.exists(), 'session_identity_collision')
        raise SystemExit(supervise(command, started[0] + 1200, session_root=session_root,research_ticket=ticket))
    except Exception:
        print('Bounded session supervisor failed.', file=sys.stderr)
        raise SystemExit(1) from None




