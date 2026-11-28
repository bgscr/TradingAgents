"""Opt-in local diagnostic observations, separate from evidence/admission.

The CLI supplies its existing content-addressed writer. No requests, credentials,
configuration reads, global handlers, or qualification decisions occur here.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from hashlib import sha256
from threading import Lock

_SINK = ContextVar('run_diagnostic_sink', default=None)
_PERSONAL_OUTPUT_SINK = ContextVar('personal_analyst_output_sink', default=None)
_OUTPUT_ANALYSTS = frozenset({'market', 'fundamentals', 'news', 'social', 'sentiment'})
_OUTPUT_STAGES = frozenset({'initial_response', 'finalizer'})
_OUTPUT_OUTCOMES = frozenset({'response', 'success', 'output_limit', 'none_parsed',
                              'validation_error', 'transport_error'})
_LENGTH_OBSERVATIONS = frozenset({'raw_finish_reason_length', 'sdk_length_finish_reason_error'})
_PERSONAL_OUTPUT_LIMIT = 60
_KINDS = frozenset({'claim_validation', 'analyst_output', 'news_exception',
                    'evidence_note_binding', 'admission_gate', 'news_business_status',
                    'news_response_shape', 'news_selection'})
_NEWS_ERROR_CODES = frozenset('news_' + reason for reason in (
    'http_status', 'duplicate_json_key', 'nonfinite_json_number', 'body_type', 'body_limit',
    'expected_callback_invalid', 'encoding_invalid', 'callback_invalid', 'wrapper_invalid',
    'json_invalid', 'trailing_syntax', 'root_not_object', 'business_failure',
    'business_status_unverified', 'request_invalid', 'schema_invalid', 'no_usable_articles',
))
_SENSITIVE = re.compile(
    r'(?i)(?:https?://|\b(?:authorization|proxy-authorization|cookie|set-cookie|'
    r'api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|secret)\b'
    r'[\"\x27]?\s*[=:]|\bbearer\s+|\bsk-[A-Za-z0-9_-]+|-----BEGIN .*PRIVATE KEY)')


def diagnostic_text(value, limit=8192):
    """Keep bounded relevant prose, or redact the whole field on secret/URL markers.

    Never reads secrets to compare them. Unknown unlabelled secret strings cannot
    be identified generically; only structured claims and cited evidence enter
    this function, never exception messages, headers, request bodies or env.
    """
    if not isinstance(value, str):
        return {'text': None, 'status': 'not_text'}
    if _SENSITIVE.search(value):
        return {'text': '[REDACTED]', 'status': 'sensitive_or_url_marker'}
    return {'text': value[:limit], 'status': 'complete' if len(value) <= limit else 'truncated',
            'original_chars': len(value), 'sha256': sha256(value.encode()).hexdigest()}


@contextmanager
def diagnostic_scope(sink):
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


@contextmanager
def personal_analyst_output_scope():
    """Capture only projected analyst observations without enabling the general sink."""
    result = {'events': [], 'dropped_events': 0}
    lock = Lock()
    closed = False

    def retain(event):
        with lock:
            if closed:
                return
            if len(result['events']) < _PERSONAL_OUTPUT_LIMIT:
                result['events'].append(event)
            else:
                result['dropped_events'] += 1

    token = _PERSONAL_OUTPUT_SINK.set(retain)
    try:
        yield result
    finally:
        with lock:
            closed = True
        _PERSONAL_OUTPUT_SINK.reset(token)


class DiagnosticPersistenceError(BaseException):
    """Terminal local retention failure; never a provider/format retry."""


class NewsResponseRejected(ValueError):
    """Local response rejection carried through existing ValueError handling.

    Only closed diagnostic labels travel with it, never the response or the
    original exception. Acquisition policy remains the existing ValueError path.
    """

    STAGES = frozenset({'body_read', 'decode', 'retention'})
    ERROR_TYPES = frozenset({
        'ValueError', 'TypeError', 'UnicodeError', 'UnicodeDecodeError', 'OSError',
        'PermissionError', 'FileExistsError', 'FileNotFoundError', 'TimeoutError',
        'ConnectionError', 'Timeout', 'ReadTimeout', 'ConnectTimeout', 'HTTPError',
        'RuntimeError', 'SSLError', 'ProxyError', 'OtherError',
    })

    def __init__(self, *, stage, error_type='ValueError', http_status=None):
        if type(stage) is not str or stage not in self.STAGES:
            raise ValueError('invalid_news_rejection_stage')
        super().__init__('news_response_rejected')
        self.stage = stage
        self.error_type = (error_type if type(error_type) is str
                           and error_type in self.ERROR_TYPES else 'OtherError')
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


def emit(kind, payload):
    sink = _SINK.get()
    if sink is not None and kind in _KINDS:
        # Synchronous durability: persistence failure is visible to the caller,
        # never reported as successful retention or used to admit evidence.
        try:
            sink({'version': 1, 'kind': kind, 'qualification_authority': False, 'payload': payload})
        except Exception:
            raise DiagnosticPersistenceError('diagnostic_persistence_failed') from None


def record_claim(*, claim, validation, facts, sources, numeric_statement,
                 indicator, bound_symbol, unsupported, indicator_error, snapshot_row=None):
    if _SINK.get() is None:
        return
    # Same production parser/comparator as the actual validator, not a second verdict.
    from tradingagents.evidence import (
        _numeric_claim_tokens,
        _numeric_token_parts,
        _numeric_token_supported,
    )
    tokens = _numeric_claim_tokens(numeric_statement)
    quote = claim.source_quote.strip() or claim.statement.strip()
    source_tokens = _numeric_claim_tokens(quote)
    sensitive = any(_SENSITIVE.search(text) for text in
                    (claim.statement, claim.source_quote, *claim.source_refs))
    if sensitive:
        # Derived token lists/differences must not reconstruct redacted values.
        # A model can copy the value into a different field without its label.
        # Redact both related prose fields, never just the matching one.
        redacted = {'text': '[REDACTED]', 'status': 'associated_sensitive_text_redacted'}
        emit('claim_validation', {'claim': {'statement': redacted, 'source_quote': redacted},
              'validation': {'status': validation.status.value},
              'stage': 'claim_validation', 'detail_status': 'sensitive_text_redacted',
              'numeric': None, 'trusted_facts': []})
        return
    comparisons = []
    numeric_complete = len(tokens) <= 32 and len(source_tokens) <= 32 and all(
        len(token) <= 128 for token in (*tokens, *source_tokens))
    for token in tokens[:32]:
        if len(token) > 128:
            continue
        parts = _numeric_token_parts(token)
        if parts is None:
            continue
        value, decimals, percent = parts
        tolerance = Decimal(5).scaleb(-(decimals + 1)) if decimals else Decimal(0)
        candidates = []
        for source in source_tokens[:32]:
            if len(source) > 128:
                continue
            source_parts = _numeric_token_parts(source)
            if source_parts is not None:
                candidates.append({'source_token': source, 'difference': str(value-source_parts[0]),
                    'percent_marker_matches': percent == source_parts[2],
                    'within_numeric_tolerance': _numeric_token_supported(token, (source,))})
        comparisons.append({'claim_token': token, 'absolute_tolerance': str(tolerance),
            'candidates': candidates, 'any_numeric_match': _numeric_token_supported(token, source_tokens),
            'validator_reported_mismatch': token in unsupported})
    emit('claim_validation', {
        'claim': {'claim_id': diagnostic_text(claim.claim_id), 'analyst': claim.analyst,
                  'statement': diagnostic_text(claim.statement), 'source_quote': diagnostic_text(claim.source_quote),
            'source_refs': [diagnostic_text(ref) for ref in claim.source_refs[:32]],
            'source_refs_complete': len(claim.source_refs) <= 32},
        'validation': {'status': validation.status.value, 'detail': diagnostic_text(validation.detail or ''),
                       'fact_ids': list(validation.fact_ids)},
        'sources': [{'ref': diagnostic_text(s.source_id), 'status': s.status.value,
                     'detail': diagnostic_text(s.detail or '')} for s in sources if s.source_id in claim.source_refs],
        'trusted_facts': [{'fact_id': f.fact_id, 'artifact_sha256': f.artifact_sha256,
            'tool_name': diagnostic_text(f.tool_name), 'tool_call_id': diagnostic_text(f.tool_call_id),
            'source_ref': diagnostic_text(f.source_ref), 'source_quote': diagnostic_text(f.raw_text),
            'span_start': f.source_span_start, 'span_end': f.source_span_end,
            'calculation_ids': [diagnostic_text(i) for i in f.calculation_ids],
            'fact_kind': f.fact_kind, 'canonical_field': diagnostic_text(f.canonical_field),
            'unit': diagnostic_text(f.unit), 'effective_date': diagnostic_text(f.effective_date),
            'semantic_status': 'source fact metadata only; excerpt does not qualify a decision'} for f in facts],
        'stage': 'claim_validation',
        'numeric': {'statement_after_bound_metadata': diagnostic_text(numeric_statement),
            'unsupported_tokens': [t if len(t) <= 128 else '[OVERSIZE_NUMBER]' for t in unsupported[:32]],
            'unsupported_tokens_complete': len(unsupported) <= 32 and all(len(t) <= 128 for t in unsupported),
            'comparisons': comparisons, 'comparisons_complete': numeric_complete,
            'comparison_semantics': 'excerpt token candidates; not field, unit, date or formula qualification',
            'bound_instrument': bound_symbol,
            'indicator_observation': (None if indicator is None else {
                'indicator': indicator.indicator, 'date': indicator.effective_date,
                'value': indicator.value if len(indicator.value) <= 128 else '[OVERSIZE_NUMBER]'}),
            'indicator_metadata_error': indicator_error,
            'snapshot_row_context': (None if snapshot_row is None else {
                'field': snapshot_row.field, 'effective_date': snapshot_row.effective_date,
                'value': snapshot_row.value if len(snapshot_row.value) <= 128 else '[OVERSIZE_NUMBER]',
                'qualification': 'excerpt_context_only'})},
    })


def record_note_binding(note, *, validated_message_count, match_count):
    """Record quote binding, never endorse a model paraphrase or create a fact."""
    if _SINK.get() is None:
        return
    values = (note.note_id, note.statement, note.source_ref, note.source_quote,
              note.tool_call_id or '')
    sensitive = any(_SENSITIVE.search(value) for value in values)
    redacted = {'text': '[REDACTED]', 'status': 'associated_sensitive_text_redacted'}
    reason = ('source_bound_quote' if note.binding_status == 'source_bound_quote'
              else 'source_not_validated' if not validated_message_count
              else 'quote_not_found' if not match_count else 'quote_ambiguous')
    emit('evidence_note_binding', {
        'stage': 'tool_quote_binding', 'reason_class': reason,
        'note': {name: redacted if sensitive else diagnostic_text(getattr(note, name))
                 for name in ('note_id', 'statement', 'source_ref', 'source_quote')},
        'kind': note.kind,
        'binding_status': note.binding_status,
        'validated_message_count': validated_message_count, 'quote_match_count': match_count,
        'source_status': note.source_status.value if note.source_status is not None else None,
        # Necessity is finalized by submission/policy merge, not this catalog.
        'necessity': 'see_admission_gate_source_state',
        'detail_status': 'sensitive_text_redacted' if sensitive else 'captured',
        'binding': None if sensitive else {
            'tool_call_id': diagnostic_text(note.tool_call_id),
            'source_content_sha256': note.source_content_sha256,
            'quote_start': note.quote_start, 'quote_end': note.quote_end},
        'financial_fact_created': False, 'decision_qualified': False,
        'semantic_status': 'paraphrase_not_established',
    })


def record_admission_gate(evidence, result):
    """Fixed gate stage/reasons plus sanitized source IDs; no raw state/body."""
    if _SINK.get() is None:
        return
    # Earlier identity/snapshot/history branches did not stop on these sources.
    # Only attribute source blockers to the branch actually returned by the gate.
    status_by_code = {
        'decision_configuration_invalid': 'not_applicable',
        'required_evidence_conflicted': 'conflicted',
        'required_evidence_unavailable': 'unavailable',
    }
    statuses = {status_by_code[code.value] for code in result.diagnostic_codes
                if code.value in status_by_code} if not result.admitted else set()
    sources = [s for s in evidence.sources if s.required and s.status.value in statuses]
    reasons = {
        'evidence note quote is not uniquely source-bound': 'limitation_quote_unbound',
        'immutable claim or fact ID was redefined': 'immutable_evidence_conflict',
        'output_limit': 'output_limit',
    }
    emit('admission_gate', {
        'stage': 'evaluate_admission_gate', 'admitted': result.admitted,
        'readiness': result.readiness.value,
        'diagnostic_codes': [code.value for code in result.diagnostic_codes],
        'blocking_sources': [{
            'source_id': diagnostic_text(s.source_id), 'required': s.required,
            'status': s.status.value, 'reason_class': reasons.get(s.detail, 'other'),
        } for s in sources[:64]],
        'blocking_sources_complete': len(sources) <= 64,
        'blocking_source_count': len(sources),
        'qualification_authority': False,
    })


def record_output(analyst, stage, attempt, diagnostics, *, outcome, personal_observation=None):
    retain = _PERSONAL_OUTPUT_SINK.get()
    if _SINK.get() is None and retain is None:
        return
    from tradingagents.agents.utils.structured import (
        bounded_analyst_output_usage,
        safe_output_diagnostics,
    )
    safe = safe_output_diagnostics(diagnostics)
    emit('analyst_output', {'analyst': analyst if analyst in _OUTPUT_ANALYSTS else 'other',
        'stage': stage if stage in _OUTPUT_STAGES else 'other',
        'attempt': attempt, 'outcome': outcome if outcome in _OUTPUT_OUTCOMES else 'other',
        **safe})
    if retain is None or not (
        analyst in _OUTPUT_ANALYSTS and stage in _OUTPUT_STAGES and outcome in _OUTPUT_OUTCOMES
    ):
        return

    observation = outcome
    usage = safe
    if personal_observation is not None:
        observed_code, observed_diagnostics = personal_observation
        if outcome == 'output_limit' and observed_code in _LENGTH_OBSERVATIONS:
            observation = observed_code
            usage = observed_diagnostics
        elif outcome == observed_code:
            usage = observed_diagnostics
    if outcome == 'validation_error' and safe.get('validation_kind') in {'json_syntax', 'schema_validation'}:
        observation = safe['validation_kind']
    retain({'analyst': analyst, 'stage': stage, 'observation': observation,
            **bounded_analyst_output_usage(usage)})


def record_news_response_shape(payload, *, callback_matches, stage='response_parse'):
    """Record fixed structural classes before business rejection; no body/keys."""
    if _SINK.get() is None:
        return
    if type(stage) is not str or stage not in {'decode', 'response_parse'}:
        raise ValueError('invalid_news_response_shape_stage')
    result = payload.get('result')
    article_count = None
    if 'result' not in payload:
        shape = 'result_missing'
    elif result is None:
        shape = 'result_null'
    elif type(result) is not dict:
        shape = 'result_wrong_type'
    elif 'cmsArticleWebOld' not in result:
        shape = 'missing'
    elif result['cmsArticleWebOld'] is None:
        shape = 'null'
    elif type(result['cmsArticleWebOld']) is not list:
        shape = 'wrong_type'
    else:
        shape = 'array'
        article_count = len(result['cmsArticleWebOld'])
    emit('news_response_shape', {
        'contract': 'news-response-shape-v1', 'stage': stage,
        'endpoint': 'eastmoney_search_news', 'target_collection': shape,
        'callback_matches': callback_matches if type(callback_matches) is bool else None,
        'article_count': min(article_count, 100000) if article_count is not None else None,
        'counts_capped': article_count is not None and article_count > 100000,
        'coverage_status': 'unknown',
    })


def record_news_selection(*, selected_count, excluded_counts):
    """Retain only parser-owned exclusion counters, never headlines or dates."""
    if _SINK.get() is None:
        return
    values = {key: excluded_counts.get(key) for key in
              ('malformed', 'undated', 'outside_window', 'wrong_stock')}
    values['selected'] = selected_count
    bounded = {key: min(value, 100000) if type(value) is int and value >= 0 else None
               for key, value in values.items()}
    emit('news_selection', {
        'contract': 'news-selection-diagnostic-v1', 'stage': 'article_selection',
        'endpoint': 'eastmoney_search_news', 'selected_count': bounded.pop('selected'),
        'excluded_counts': bounded,
        'counts_capped': any(type(value) is int and value > 100000 for value in values.values()),
        'coverage_status': 'unknown',
    })


def record_news_business_status(payload, result, *, stage='response_parse'):
    """Eight fixed fields at most; never retain arbitrary values or response text.

    Classes describe syntax only. Even zero/ok/true is NOT supplier success
    authority. Unknown strings/numbers/containers produce only ``other``.
    """
    if _SINK.get() is None:
        return
    if type(stage) is not str or stage not in {'decode', 'response_parse'}:
        raise ValueError('invalid_news_business_status_stage')
    fields = []
    type_names = {str: 'string', bool: 'boolean', int: 'integer', float: 'number',
                  dict: 'object', list: 'array', type(None): 'null'}
    for scope, obj in (('root', payload), ('result', result)):
        if type(obj) is not dict:
            continue
        for key in ('code', 'status', 'success', 'error'):
            if key not in obj:
                continue
            value = obj[key]
            kind = type(value)
            category = 'other'
            if value is None:
                category = 'null'
            elif kind is bool:
                category = 'true' if value else 'false'
            elif kind is int:
                category = {0: 'zero', 1: 'one', 200: 'http_ok'}.get(value, 'other')
            elif kind is str and len(value) <= 16:
                normalized = value.strip().lower()
                if normalized in {'ok', 'success', 'error', 'failed', 'failure'}:
                    category = normalized
            fields.append({'scope': scope, 'field': key,
                           'type': type_names.get(kind, 'other'), 'value_class': category})
    if fields:
        emit('news_business_status', {'stage': stage, 'endpoint': 'eastmoney_search_news',
             'contract': 'news-business-status-diagnostic-v1', 'fields': fields,
             'semantic_status': 'metadata_only_not_provider_success'})


def record_news_exception(exc, *, stage, role='primary', provider='akshare', status=None):
    """Fixed identities only. Never str(exc), messages, headers, URLs or context chains."""
    if _SINK.get() is None:
        return
    identities = {(t.__module__, t.__name__) for t in type(exc).__mro__}
    known = [
        ('requests.exceptions', 'ConnectTimeout'), ('requests.exceptions', 'ReadTimeout'),
        ('requests.exceptions', 'Timeout'), ('requests.exceptions', 'HTTPError'),
        ('requests.exceptions', 'ConnectionError'), ('json.decoder', 'JSONDecodeError'),
        ('tradingagents.dataflows.acquisition', 'AcquisitionFailure'),
        ('builtins', 'TimeoutError'), ('builtins', 'ConnectionError'),
        ('builtins', 'KeyError'), ('builtins', 'TypeError'), ('builtins', 'ValueError'),
        ('builtins', 'OSError'), ('builtins', 'RuntimeError'),
    ]
    error_type = next((name for module,name in known if (module,name) in identities), 'other')
    error_code = None
    if ('tradingagents.dataflows.acquisition', 'AcquisitionFailure') in identities:
        if status is None:
            status = getattr(exc, 'status_code', None)
        code = getattr(exc, 'error_code', None)
        error_code = code if isinstance(code, str) and code in _NEWS_ERROR_CODES else 'other'
    if status is None and any(module == 'requests.exceptions' for module,_ in identities):
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if type(exc) is NewsResponseRejected and role == 'primary':
        # Preserve the first local stage through adapter/provider wrappers.
        # Recheck closed fields even if an in-process caller mutated the object.
        stage = exc.stage if type(exc.stage) is str and exc.stage in NewsResponseRejected.STAGES else 'other'
        error_type = (exc.error_type if type(exc.error_type) is str
                      and exc.error_type in NewsResponseRejected.ERROR_TYPES else 'other')
        status = exc.http_status
    emit('news_exception', {'error_type': error_type, 'error_code': error_code,
        'stage': stage if stage in {'provider_call','validation','serialization','http_request','http_status',
                                    'response_parse','response_close','body_read','decode','retention'} else 'other',
        'role': role if role in {'primary','cleanup'} else 'other',
        'provider': provider if provider in {'akshare','yfinance','alpha_vantage','openai'} else 'other',
        'http_status': status if type(status) is int and 100 <= status <= 599 else None})
