"""Invented data for the closed browser-observed shape; no copied news samples."""
import copy
import json
from types import SimpleNamespace

import pytest

from tradingagents import news_diagnostic as diagnostic

CB = 'synthetic'
START, END = '2026-10-31', '2026-11-07'


def envelope():
    return {'bizCode': '', 'bizMsg': '', 'code': 0, 'extra': {}, 'hitsTotal': 1, 'msg': 'OK',
            'result': {'cmsArticleWebOld': [{
                'date': '2026-11-04 10:00:00', 'image': '', 'code': '202611040000000001',
                'title': '600919 synthetic news', 'content': 'Synthetic content', 'mediaName': 'Synthetic',
                'url': 'http://bank.eastmoney.com/a/202611040000000001.html'}]},
            'searchId': '00000000-0000-4000-8000-000000000001'}


def wire(value, callback=CB):
    return (callback + '(' + json.dumps(value) + ')').encode()


def parse(body, **kwargs):
    return diagnostic.eastmoney_news.parse_news_page(
        body, expected_callback=CB, ticker='600919', start_date=START, end_date=END, **kwargs)


def capture(root, value, callback=None, *, closed=None, calls=None):
    def get(url, *, params, **kwargs):
        if calls is not None:
            calls.append(True)
        return SimpleNamespace(status_code=200, content=wire(value, params['cb'] if callback is None else callback),
                               close=lambda: closed.append(True) if closed is not None else None)
    return diagnostic.capture(root, ticker='600919.SH', transport='direct', get=get, start=START, end=END)


def test_closed_observed_profile_and_exact_replay(tmp_path):
    value = envelope()
    page = parse(wire(value))
    assert page.business_status == 'observed_public_news_envelope'
    assert page.callback_matches and page.coverage_status == 'unknown' and len(page.rows) == 1
    calls, closed = [], []
    result = capture(tmp_path, value, calls=calls, closed=closed)
    assert result['capture_passed'] and result['raw_count'] == 1
    assert result['selection']['accepted_count'] == 1
    assert calls == closed == [True]
    saved = (tmp_path / diagnostic.BODY_NAME).read_bytes()
    replay = diagnostic.replay(tmp_path, ticker='600919.SH', transport='direct', start=START, end=END)
    assert replay['acceptance_passed'] and replay['same_source_bindings']
    assert replay['same_artifact_sha256'] and replay['same_adapter_frame_sha256']
    assert calls == closed == [True] and (tmp_path / diagnostic.BODY_NAME).read_bytes() == saved


@pytest.mark.parametrize('field,value', [
    ('code', 1), ('code', -1), ('code', 200), ('code', '0'), ('code', False), ('code', None),
    ('msg', 'ERROR'), ('msg', 'ok'), ('msg', ''), ('msg', None), ('msg', {}),
    ('bizCode', '0'), ('bizCode', 0), ('bizCode', None), ('bizMsg', 'failure'), ('bizMsg', None),
    ('extra', {'token': 'SYNTHETIC-PRIVATE'}), ('extra', []), ('hitsTotal', '1'),
    ('hitsTotal', True), ('hitsTotal', -1), ('hitsTotal', 0), ('hitsTotal', 2**53 + 1),
    ('searchId', 'token=SYNTHETIC-PRIVATE'), ('searchId', None), ('result', None),
    ('result', {}), ('result', {'cmsArticleWebOld': None}), ('result', {'cmsArticleWebOld': {}}),
    ('result', {'passportWeb': []}), ('result', {'cmsArticleWebOld': []}),
    ('unknown', 'token=SYNTHETIC-PRIVATE'), ('success', False), ('error', 'SYNTHETIC-PRIVATE'),
])
def test_neighbor_status_and_schema_fail_closed(tmp_path, field, value):
    payload = envelope()
    payload[field] = value
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError):
        parse(wire(payload))
    result = capture(tmp_path, payload)
    assert not result['capture_passed'] and not result['body_saved']
    assert result['checks']['parser']['status'] == 'not_started'
    assert not result.get('artifact_sha256')
    assert 'SYNTHETIC-PRIVATE' not in (tmp_path / 'capture.json').read_text()


@pytest.mark.parametrize('field', ['bizCode', 'bizMsg', 'code', 'extra', 'hitsTotal', 'msg', 'result', 'searchId'])
def test_missing_profile_field_is_not_a_success(tmp_path, field):
    payload = envelope()
    payload.pop(field)
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError):
        parse(wire(payload))
    assert not capture(tmp_path, payload)['capture_passed']


@pytest.mark.parametrize('field,value', [
    ('url', 'https://secret.invalid/a/202611040000000001.html'),
    ('url', 'http://bank.eastmoney.com.evil.invalid/a/202611040000000001.html'),
    ('url', 'http://bank.eastmoney.com/a/202611040000000002.html'),
    ('url', 'http://bank.eastmoney.com/a/202611040000000001.html?token=secret'),
    ('url', 'http://user:secret@bank.eastmoney.com/a/202611040000000001.html'),
    ('url', {}), ('image', 'http://secret.invalid/x'), ('date', 123), ('date', '2026-02-30 10:00:00'),
    ('code', 123), ('code', '123'), ('title', {}), ('content', 'token=SYNTHETIC-PRIVATE'),
    ('mediaName', []), ('private', 'SYNTHETIC-PRIVATE'),
])
def test_article_fields_and_public_url_are_closed(tmp_path, field, value):
    payload = envelope()
    payload['result']['cmsArticleWebOld'][0][field] = value
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError):
        parse(wire(payload))
    calls, closed = [], []
    result = capture(tmp_path, payload, calls=calls, closed=closed)
    assert calls == closed == [True]
    assert not result['body_saved'] and not result.get('artifact_sha256')
    assert result['checks']['parser']['status'] == 'not_started'


@pytest.mark.parametrize('callback', ['different', 'a.b', 'function', 'x);run();//'])
def test_observed_profile_requires_matching_valid_callback(tmp_path, callback):
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError):
        parse(wire(envelope(), callback))
    result = capture(tmp_path, envelope(), callback=callback)
    assert not result['capture_passed'] and not result['body_saved']


def test_observed_profile_keeps_date_and_stock_selection(tmp_path):
    payload = envelope()
    rows = payload['result']['cmsArticleWebOld']
    rows += [copy.deepcopy(rows[0]), copy.deepcopy(rows[0])]
    rows[1]['date'] = '2026-11-08 00:00:00'
    rows[2]['title'] = '600895 unrelated news'
    payload['hitsTotal'] = 196
    page = parse(wire(payload))
    assert len(page.rows) == 1 and page.excluded_counts == {
        'malformed': 0, 'undated': 0, 'outside_window': 1, 'wrong_stock': 1}
    result = capture(tmp_path, payload)
    assert result['capture_passed'] and result['raw_count'] == 3
    assert result['selection']['accepted_count'] == 1 and result['selection']['coverage'] == 'unknown'


def test_code_zero_alone_still_unverified():
    payload = envelope()
    payload = {'code': 0, 'result': payload['result']}
    with pytest.raises(diagnostic.eastmoney_news.NewsResponseError) as error:
        parse(wire(payload))
    assert error.value.reason == 'business_status_unverified'


def test_synthetic_unordered_page_filters_every_row_and_rejects_future_stock():
    payload=envelope()
    base=payload['result']['cmsArticleWebOld'][0]
    rows=[]
    # Invented article data; dates exercise a newer row after an older row,
    # cutoff+1, before-window, and a different stock. No browser text copied.
    for index,(date,symbol) in enumerate([
        ('2026-11-12 17:00:00','600919'),('2026-11-14 09:00:00','600919'),
        ('2026-11-13 15:00:00','600919'),('2026-11-05 10:00:00','600919'),
        ('2026-11-13 11:00:00','600919'),('2026-11-13 08:00:00','600895')],1):
        row=copy.deepcopy(base)
        row.update(date=date,code=f'20261113000000000{index}',title=symbol+' synthetic news')
        row['url']=f'http://bank.eastmoney.com/a/{row["code"]}.html'
        rows.append(row)
    payload['result']['cmsArticleWebOld']=rows
    payload['hitsTotal']=6
    page=diagnostic.eastmoney_news.parse_news_page(wire(payload),expected_callback=CB,
        ticker='600919',start_date='2026-11-06',end_date='2026-11-13')
    assert len(page.rows)==3 and page.excluded_counts=={
        'malformed':0,'undated':0,'outside_window':2,'wrong_stock':1}
    one_day=diagnostic.eastmoney_news.parse_news_page(wire(payload),expected_callback=CB,
        ticker='600919',start_date='2026-11-13',end_date='2026-11-13')
    assert len(one_day.rows)==2 and one_day.excluded_counts['outside_window']==3
    assert page.coverage_status=='unknown'
