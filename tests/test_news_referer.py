"""Synthetic transport only: query-bound public Referer, never arbitrary input."""

import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from urllib3.response import HTTPResponse

from tradingagents.dataflows import akshare_data
from tradingagents.dataflows.eastmoney_news import NewsResponseError

START, END = "2026-11-06", "2026-11-13"


@pytest.fixture
def wire(monkeypatch):
    calls, closed, policies = [], [], []
    original_init, original_send = requests.Session.__init__, requests.Session.send

    def init(session):
        original_init(session)
        session.trust_env = False  # No implicit environment/.netrc credential discovery.

    def observe_send(session, request, **kwargs):
        policies.append((kwargs["allow_redirects"], kwargs["timeout"]))
        return original_send(session, request, **kwargs)

    def send(adapter, request, **kwargs):
        assert not calls, "unexpected second send"
        url = urlsplit(request.url)
        assert request.method == "GET"
        assert (url.scheme, url.netloc, url.path) == (
            "https", "search-api-web.eastmoney.com", "/search/jsonp")
        query = parse_qs(url.query)
        assert set(query) == {"cb", "param", "_"}
        assert query["cb"] == ["jQuery35101792940631092459_1764599530165"]
        assert query["_"] == ["1764599530176"]
        parameters = json.loads(query["param"][0])
        code = parameters["keyword"]
        assert parameters == {
            "uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                "pageIndex": 1, "pageSize": 10, "preTag": "<em>", "postTag": "</em>"}},
        }
        assert request.headers["Referer"] == "https://so.eastmoney.com/news/s?keyword=" + code
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        calls.append(code)
        payload = {"result": {"cmsArticleWebOld": [{
            "date": "2026-11-10 12:00:00", "code": "synthetic-article",
            "title": code + " synthetic news", "content": "Synthetic only", "mediaName": "Synthetic",
        }]}}
        body = (query["cb"][0] + "(" + json.dumps(payload) + ")").encode()
        raw = HTTPResponse(body=io.BytesIO(body), status=200, preload_content=False, decode_content=False)
        response = adapter.build_response(request, raw)
        original_close = response.close

        def close():
            closed.append(True)
            return original_close()

        response.close = close
        return response

    monkeypatch.setattr(requests.Session, "__init__", init)
    monkeypatch.setattr(requests.Session, "send", observe_send)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls, closed, policies


@pytest.mark.parametrize("ticker,code", [
    ("600919", "600919"), ("600895", "600895"),
    ("600919.SH", "600919"), ("600919.SS", "600919"),
    ("600895.SH", "600895"), ("000001.SZ", "000001"),
    (" 600895.sh++ ", "600895"),
])
def test_public_referer_matches_canonical_query(wire, ticker, code):
    result = akshare_data.get_news(ticker, START, END)
    assert code + " synthetic news" in result
    assert "news_window_coverage_unknown" in result
    assert wire == ([code], [True], [(False, 15)])


@pytest.mark.parametrize("code", ["600919", "600895"])
def test_direct_adapter_uses_same_validated_code(wire, code):
    frame = akshare_data._stock_news_em(code, START, END)
    assert len(frame) == 1 and frame.attrs["news_coverage"] == "unknown"
    assert wire == ([code], [True], [(False, 15)])


@pytest.mark.parametrize("entry", ["get_news", "_stock_news_em"])
@pytest.mark.parametrize("ticker", [
    None, 600919, "", "60091", "600919/../../", "600919?token=synthetic-only",
    "600919&keyword=600895", "600919#fragment", "//synthetic.invalid",
    "https://synthetic.invalid/600919", "600919%0d%0aX-Header:synthetic",
    "600919.SH@synthetic.invalid", "６００９１９", "600９１９", "600919\u2028",
])
def test_invalid_input_stops_before_request_preparation(wire, entry, ticker):
    with pytest.raises(NewsResponseError, match="^request_invalid$"):
        getattr(akshare_data, entry)(ticker, START, END)
    assert wire == ([], [], [])


@pytest.mark.parametrize("entry", ["get_news", "_stock_news_em"])
def test_every_ascii_control_is_rejected_before_normalization(wire, entry):
    for value in (*range(32), 127):
        for ticker in (chr(value) + "600919", "600919" + chr(value), "600" + chr(value) + "919"):
            with pytest.raises(NewsResponseError, match="^request_invalid$"):
                getattr(akshare_data, entry)(ticker, START, END)
    assert wire == ([], [], [])
