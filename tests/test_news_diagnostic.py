import json
from types import SimpleNamespace

import pytest


def test_news_diagnostic_replays_actual_sdk_and_production_path(tmp_path):
    from tradingagents import news_diagnostic as diagnostic

    calls = []

    def get(url, *, params, headers, **options):
        calls.append((url, params, options))
        assert params["_"] == "1764599530176"
        assert "referer" in headers and "cookie" in headers
        assert options == {"allow_redirects": False, "timeout": 20}
        text = (
            params["cb"]
            + "("
            + json.dumps(
                {
                    "result": {
                        "cmsArticleWebOld": [
                            {
                                "date": "2026-10-10 12:00:00",
                                "mediaName": "Synthetic public fixture",
                                "code": "202610101234",
                                "title": "<em>Title</em>",
                                "content": "Content\u3000fixture",
                                "image": "",
                            }
                        ]
                    }
                }
            )
            + ")"
        )
        return SimpleNamespace(
            status_code=200,
            text=text,
            content=text.encode(),
            headers={"Content-Type": "application/javascript", "Content-Encoding": "gzip"},
        )

    result = diagnostic.capture(tmp_path, get=get, start="2026-10-05", end="2026-10-12")
    assert result["stage"] == "available"
    assert result["request_count"] == 1
    assert result["body_saved"]
    replay = diagnostic.replay(tmp_path, start="2026-10-05", end="2026-10-12")
    assert replay["stage"] == "available"
    assert replay["artifact_sha256"] == result["artifact_sha256"]
    assert len(calls) == 1
    saved = (tmp_path / "public-response.txt").read_text(encoding="utf-8")
    assert "202610101234" in saved
    assert "cookie" not in saved


@pytest.mark.parametrize(
    "text,stage",
    [
        ("<html>not news</html>", "jsonp"),
        ("jQuery35101792940631092459_1764599530165(not json)", "json"),
        (
            'jQuery35101792940631092459_1764599530165({"result":{"cmsArticleWebOld":[]}})',
            "sdk_dataframe",
        ),
    ],
)
def test_news_diagnostic_names_failure_stage_without_claiming_recovery(tmp_path, text, stage):
    from tradingagents import news_diagnostic as diagnostic

    result = diagnostic.capture(
        tmp_path,
        get=lambda *a, **k: SimpleNamespace(
            status_code=200, text=text, content=text.encode(), headers={}
        ),
        start="2026-10-05",
        end="2026-10-12",
    )
    assert result["stage"] == stage
    assert result["request_count"] == 1
    assert result["body_saved"] == (stage == "sdk_dataframe")
