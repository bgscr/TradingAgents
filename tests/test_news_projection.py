"""Synthetic production projection tests; no stdin, credentials or network."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from tradingagents import news_projection as m
from tradingagents.powershell_news import make_request


def payload(request, rows=None, **extra):
    rows = rows if rows is not None else [article(request.ticker[:6], request.end)]
    return request.callback + '(' + json.dumps({'result': {'cmsArticleWebOld': rows}, **extra}, ensure_ascii=False) + ')'


def article(symbol, day):
    return {'date': day + ' 12:00:00', 'mediaName': '合成媒体', 'code': 'synthetic1',
            'title': symbol + ' 合成公告', 'content': '仅用于离线验证', 'image': ''}


def entry():
    spec = importlib.util.spec_from_file_location('news_entry_test', Path(__file__).resolve().parents[1] / 'scripts/analyze_mainland_bounded.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('ticker', ['600919.SH', '600895.SS', '000001.SZ'])
def test_request_is_stock_bound_and_uses_production_query(ticker):
    from tradingagents.dataflows.eastmoney_news import news_query
    req = make_request(ticker, '2026-11-13')
    query = parse_qs(urlsplit(req.url).query, strict_parsing=True)
    assert req.start == '2026-11-06' and req.end == '2026-11-13'
    assert set(query) == {'cb', 'param', '_'} and all(len(v) == 1 for v in query.values())
    assert query['cb'] == [req.callback]
    assert {k: v[0] for k, v in query.items()} == news_query(ticker[:6], req.callback, query['_'][0])
    assert json.loads(query['param'][0])['keyword'] == ticker[:6]
    assert req.callback == make_request(ticker, req.end).callback
    assert '%20' in req.url and '%3Cem%3E' in req.url


@pytest.mark.parametrize('ticker,day', [('AAPL','2026-11-13'), ('600919.SH?x=1','2026-11-13'), ('600919.SH','20261113')])
def test_invalid_request_has_no_url(ticker, day):
    with pytest.raises(ValueError):
        make_request(ticker, day)


def test_unordered_future_wrong_stock_empty_and_callback_are_not_facts():
    req = make_request('600919.SH', '2026-11-13')
    rows = [article('600919','2026-11-14'), article('600919','2026-11-10'), article('000001','2026-11-12'), article('600919','2026-11-13')]
    value = m.validate_response(req, payload(req, rows))
    assert value.status == 'accepted' and len(value.rows) == 2
    assert dict(value.excluded)['outside_window'] == 1 and dict(value.excluded)['wrong_stock'] == 1
    assert all(row[0][:10] <= req.end for row in value.rows)
    assert m.validate_response(req, payload(req).replace(req.callback, 'different_callback', 1)).status == 'callback_mismatch'
    assert m.validate_response(req, payload(req, [])).status == 'no_usable_articles'
    assert m.validate_response(req, payload(req, [article('000001',req.end)])).status == 'no_usable_articles'
    assert m.validate_response(req, req.callback + '({"result":{}})').status == 'articles_missing'
    assert m.validate_response(req, payload(req) + ';alert(1)').status == 'trailing_syntax'


@pytest.mark.parametrize('where', ['unknown_key','unknown_value','article','future','missing','oversize','nested','entity','tags'])
def test_sensitive_malicious_and_oversize_input_not_retained(where, capsys):
    req = make_request('600919.SH','2026-11-13')
    sentinel = 'authorization=Bearer_SECRET_SENTINEL'
    rows = [article('600919', req.end)]
    extra = {}
    if where == 'unknown_key': extra[sentinel] = 'x'
    if where == 'unknown_value': extra['extra'] = sentinel
    if where in ('article','future'):
        rows[0]['content'] = sentinel
        if where == 'future': rows[0]['date'] = '2026-11-14 12:00:00'
    if where == 'missing': del rows[0]['date']
    if where == 'nested': extra['extra'] = {'deep': {'token': sentinel}}
    if where == 'entity': rows[0]['content'] = '&#97;uthorization=Bearer_SECRET_SENTINEL'
    if where == 'tags': rows[0]['content'] = 'auth<em>orization</em>=Bearer_SECRET_SENTINEL'
    body = payload(req, rows, **extra) if where != 'oversize' else 'x' * (m.MAX_TEXT_CHARACTERS + 1)
    result = m.validate_response(req, body)
    if where in {'unknown_key', 'unknown_value', 'nested'}:
        assert result.status == 'accepted'
        assert sentinel not in json.dumps(result.rows)
    else:
        assert result.status != 'accepted' and result.rows == ()
    assert sentinel not in repr(result) + json.dumps(result.summary()) + str(capsys.readouterr())
    assert not hasattr(result, 'body') and not hasattr(result, 'raw_text')


def test_shared_public_retention_default_diagnostic_is_unchanged():
    from tradingagents.news_diagnostic import _public_payload
    from tradingagents.dataflows.eastmoney_news import public_news_payload
    req = make_request('600919.SH','2026-11-13')
    bodies = [payload(req), payload(req, [], token='SECRET_SENTINEL'), 'bad', payload(req, [], code=0)]
    for body in bodies:
        assert _public_payload(body.encode(), req.callback) == public_news_payload(body.encode(), req.callback)


def test_disabled_dotenv_never_discovers_or_reads_files(monkeypatch):
    import sys
    import types
    source = (Path(__file__).resolve().parents[1] / 'tradingagents/__init__.py').read_text()
    dotenv = types.ModuleType('dotenv')
    dotenv.find_dotenv = Mock(side_effect=AssertionError('unexpected discovery'))
    dotenv.load_dotenv = Mock(side_effect=AssertionError('unexpected credential read'))
    monkeypatch.setitem(sys.modules, 'dotenv', dotenv)
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED','1')
    exec(compile(source, 'synthetic_package_init', 'exec'), {})
    dotenv.find_dotenv.assert_not_called()
    dotenv.load_dotenv.assert_not_called()


@pytest.mark.parametrize('kind', ['wrong_stock', 'future', 'wider_window', 'missing', 'cancel', 'expired'])
def test_projection_keeps_query_and_budget_boundaries(monkeypatch, kind):
    from types import SimpleNamespace
    from tradingagents import analysis_budget
    req = make_request('600919.SH', '2026-11-13')
    value = m.validate_response(req, payload(req)) if kind != 'missing' else None
    class Stop(BaseException):
        pass
    def check():
        if kind in {'cancel', 'expired'}:
            raise Stop()
    budget = SimpleNamespace(check=check, admit=Mock(side_effect=AssertionError('projection cannot send')))
    monkeypatch.setattr(analysis_budget, 'active_budget', lambda: budget)
    args = (value, '000001.SZ' if kind == 'wrong_stock' else req.ticker,
            '2026-11-01' if kind == 'wider_window' else req.start,
            '2026-11-14' if kind == 'future' else req.end)
    kwargs = dict(tool_call_id='projection-reject', source_ref='news:synthetic', capability='get_news')
    if kind in {'cancel', 'expired'}:
        with pytest.raises(Stop):
            m.acquire_validated_news(*args, **kwargs)
    else:
        result = m.acquire_validated_news(*args, **kwargs)
        assert result.artifact is None and result.outcomes[0].retryable is False
    budget.admit.assert_not_called()
