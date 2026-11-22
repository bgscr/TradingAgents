"""Synthetic source metadata; no provider articles or real network access."""
import copy
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tradingagents.dataflows import eastmoney_news as parser
from tradingagents import news_projection as projection
from tests.test_news_projected_envelope import envelope, validate, wire, SENTINEL

CODE = '202611130000000001'
URL = f'http://bank.eastmoney.com/a/{CODE}.html'


@pytest.mark.parametrize('value,issue', [
    (None, 'non_string'), ({'token': SENTINEL}, 'non_string'), ([], 'non_string'), (1, 'non_string'),
    ('', 'empty'), ('x'*2049, 'length_limit'), (URL+'\n', 'unsafe_characters'),
    (' '+URL, 'unsafe_characters'), ('javascript:'+SENTINEL, 'url_format'),
    (URL.replace('http:', 'ftp:'), 'scheme_not_allowed'),
    (URL.replace('bank.', SENTINEL+'@bank.'), 'credentials_present'),
    (URL.replace('bank.eastmoney.com', 'untrusted.invalid'), 'host_not_allowed'),
    (URL.replace('bank.eastmoney.com', 'bank.eastmoney.com.evil.invalid'), 'host_not_allowed'),
    (URL.replace('.com/', '.com:80/'), 'host_not_allowed'),
    (URL+'?token='+SENTINEL, 'query_or_fragment'), (URL+'#'+SENTINEL, 'query_or_fragment'),
    (URL+'?', 'query_or_fragment'), (URL.replace('/a/', '/other/'), 'path_format'),
    (URL.replace(CODE, '202611130000000002'), 'article_id_mismatch'),
    (URL.replace('http:', 'https:'), 'https_not_in_current_policy'),
])
def test_closed_source_issue_without_values(value, issue, capsys):
    data = envelope()
    data['result']['cmsArticleWebOld'][0]['url'] = value
    result = validate(data)
    summary = result.summary()
    assert result.status == 'article_source_invalid' and result.rows == ()
    assert summary['article_source_issue'] == issue
    assert summary['accepted_count'] is None and summary['excluded_counts'] == {}
    assert summary['actual_date_min'] is None and summary['actual_date_max'] is None
    retained = json.dumps(summary)+repr(result)+str(capsys.readouterr())
    assert SENTINEL not in retained and CODE not in retained and 'eastmoney.com' not in retained


@pytest.mark.parametrize('host', ['bank', 'stock', 'finance'])
@pytest.mark.parametrize('code', [CODE, 'synthetic-legacy_1'])
def test_exact_legacy_acceptance_set_unchanged(host, code):
    exact = f'http://{host}.eastmoney.com/a/{code}.html'
    assert parser._article_source_issue(exact, code) is None
    for value in [exact.replace('http:', 'https:'), exact+'?', exact+'/', exact.upper(),
                  exact.replace(code, 'different'), exact.replace('.com', '.com:80')]:
        assert parser._article_source_issue(value, code) in parser.ARTICLE_SOURCE_ISSUES


def test_optional_missing_url_and_default_retention_unchanged():
    data = envelope()
    result = validate(data)
    assert result.status == 'accepted' and 'article_source_issue' not in result.summary()
    request = result.request
    assert parser.public_news_payload(wire(data, request).encode(), request.callback)[0] is None
    del data['result']['cmsArticleWebOld'][0]['url']
    assert validate(data).status == 'accepted'
    assert parser.public_news_payload(wire(data, request).encode(), request.callback)[0] == 'unknown_envelope_fields'


def test_one_bad_source_still_stops_whole_batch_without_partial_claim():
    data = envelope()
    bad = copy.deepcopy(data['result']['cmsArticleWebOld'][0])
    bad['url'] = URL+'?token='+SENTINEL
    data['result']['cmsArticleWebOld'].append(bad)
    result = validate(data)
    assert result.rows == () and result.status == 'article_source_invalid'
    assert result.summary()['accepted_count'] is None and result.excluded == ()
    # No later scan: retain the first source failure, not a last-error overwrite.
    data['result']['cmsArticleWebOld'].append(dict(bad, url=None))
    assert validate(data).source_issue == 'query_or_fragment'


@pytest.mark.parametrize('failure', ['callback', 'business', 'sensitive'])
def test_prior_error_precedes_source_diagnostic(failure):
    data = envelope()
    data['result']['cmsArticleWebOld'][0]['url'] = None
    request = validate(envelope()).request
    if failure == 'business':
        data['code'] = 1
    if failure == 'sensitive':
        data['result']['cmsArticleWebOld'][0]['content'] = SENTINEL
    body = wire(data, request)
    if failure == 'callback':
        body = body.replace(request.callback, 'different', 1)
    result = projection.validate_response(request, body)
    assert result.status == {'callback': 'callback_mismatch', 'business': 'business_status_unverified',
                             'sensitive': 'sensitive_article_text'}[failure]
    assert 'article_source_issue' not in result.summary()


def test_unknown_metadata_cannot_escape_closed_summary():
    result = validate(envelope())
    for value in [SENTINEL, {}, [], None]:
        assert 'article_source_issue' not in replace(result, status='article_source_invalid', source_issue=value).summary()
        error = parser.NewsResponseError('article_source_invalid', source_issue=value)
        assert error.source_issue is None and SENTINEL not in str(error)


def test_source_rejection_uses_one_send_and_no_evidence(tmp_path, monkeypatch, capsys):
    from tests.test_powershell_news import owned
    from tradingagents import powershell_news as ps, analysis_budget as budget_module
    owner = owned(tmp_path)
    data = envelope(owner.request.ticker[:6])
    data['result']['cmsArticleWebOld'][0]['url'] = URL+'?token='+SENTINEL
    send = Mock(return_value=(wire(data, owner.request).encode(), {'status': 'ok', 'cleanup': 'closed'}))
    monkeypatch.setattr(ps, 'run_child', send)
    budget = budget_module.AnalysisBudget(1, 30, allowed_data_vendors=['akshare'])
    monkeypatch.setattr(budget_module, 'active_budget', lambda: budget)
    for i in range(2):
        result = owner.acquire(owner.request.ticker, owner.request.start, owner.request.end,
            tool_call_id='synthetic-source-'+str(i), source_ref='synthetic:source', capability='get_news')
        assert result.artifact is None
    assert send.call_count == 1 and budget._counts == {'data': 1, 'model': 0}
    assert owner.summary()['admission']['article_source_issue'] == 'query_or_fragment'
    assert SENTINEL not in json.dumps(owner.summary())+str(capsys.readouterr())
