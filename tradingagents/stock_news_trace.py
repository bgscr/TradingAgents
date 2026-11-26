import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from tradingagents import run_diagnostics as diag

CALL=ContextVar("news_diagnostic_call",default=None)
EXPECTED=ContextVar("news_expected_url",default=None)
RESERVING=ContextVar("news_reserving",default=None)
@contextmanager
def expected(url):
    token=EXPECTED.set(url)
    try:yield
    finally:EXPECTED.reset(token)
@contextmanager
def admission(request):
    from urllib.parse import urlsplit
    match=False
    if CALL.get() is not None and request is not None and request.method=="GET":
        url=urlsplit(request.url)
        if (url.scheme,url.netloc,url.path)==("https","search-api-web.eastmoney.com","/search/jsonp"):
            from tradingagents.dataflows.akshare_data import _stock_news_em
            import sys
            frame=sys._getframe(1)
            while frame:
                if frame.f_code is _stock_news_em.__code__:
                    match=True
                    break
                frame=frame.f_back
    token=RESERVING.set(CALL.get() if match else None)
    try:yield
    finally:RESERVING.reset(token)

def observe(invoke):
    sink=diag._SINK.get()
    if sink is None:return invoke()
    state={"ids":[]}; invocation=uuid.uuid4().hex
    def forward(event):
        sink(event)
        payload=event["payload"]
        if event["kind"]=="news_response_shape" and payload["target_collection"]=="array":state["count"]=payload["article_count"]
        if event["kind"]=="news_exception" and payload["role"]=="primary":
            state.setdefault("stage",payload["stage"]);state.setdefault("code",payload["error_code"])
    token=CALL.set(state)
    try:
        with diag.diagnostic_scope(forward):result=invoke()
        why=getattr(getattr(result.outcomes[-1],"reason",None),"value",None)
        kind="unconfirmed"
        if why=="circuit_open":kind="circuit_open"
        elif result.artifact:kind="available"
        elif state.get("code")=="news_no_usable_articles" and "count" in state:kind="valid_empty" if state["count"]==0 else "filtered_empty"
        elif state.get("stage") in ("response_parse","http_status"):kind="response_rejected"
        elif state.get("stage")=="http_request" and state["ids"]:kind="transport_failed"
        return result
    finally:
        CALL.reset(token)
        ids=state["ids"]
        try:sink(dict(version=1,kind="stock_news_attempt",qualification_authority=False,payload=dict(invocation=invocation,classification=locals().get("kind","unconfirmed"),attempt_ids=ids,association="reserved" if ids else "not_sent" if locals().get("kind")=="circuit_open" else "unconfirmed")))
        except (Exception,diag.DiagnosticPersistenceError):pass

