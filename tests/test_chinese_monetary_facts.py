from decimal import Decimal

import pytest
from hypothesis import given, strategies as st

from tradingagents.dataflows.monetary_facts import (
    extract_chinese_monetary_facts,
    render_monetary_source_facts,
)


@pytest.mark.unit
def test_extracts_exact_yi_yuan_source_fact():
    facts = extract_chinese_monetary_facts(
        "预计产生不超过50亿元的收入。",
        source_ref="akshare:stock_news_em:article-1",
    )

    assert len(facts) == 1
    assert facts[0].raw_text == "50亿元"
    assert facts[0].value == Decimal("5000000000")
    assert facts[0].currency == "CNY"
    assert facts[0].source_ref == "akshare:stock_news_em:article-1"


@pytest.mark.unit
def test_preserves_decimal_fact_source_span():
    facts = extract_chinese_monetary_facts(
        "披露金额为200.62亿元。",
        source_ref="akshare:stock_news_em:article-2",
    )

    assert facts[0].value == Decimal("20062000000")
    assert facts[0].span == (5, 13)


@pytest.mark.unit
def test_extracts_range_as_one_source_fact_with_both_endpoints():
    facts = extract_chinese_monetary_facts(
        "预计收入50亿元~55亿元。",
        source_ref="akshare:stock_news_em:article-3",
    )

    assert len(facts) == 1
    assert facts[0].raw_text == "50亿元~55亿元"
    assert facts[0].value == Decimal("5000000000")
    assert facts[0].upper_value == Decimal("5500000000")
    assert facts[0].is_range is True


@pytest.mark.unit
def test_normalizes_yuan_without_scaling():
    facts = extract_chinese_monetary_facts("费用25元。", source_ref="announcement:1")

    assert facts[0].value == Decimal("25")
    assert facts[0].unit == "元"


@pytest.mark.unit
def test_normalizes_wan_yuan():
    facts = extract_chinese_monetary_facts("合同金额3.5万元。", source_ref="announcement:2")

    assert facts[0].value == Decimal("35000")
    assert facts[0].unit == "万元"


@pytest.mark.unit
def test_extracts_every_monetary_fact_in_source_order():
    facts = extract_chinese_monetary_facts(
        "注册资本100万元，项目投资2亿元。",
        source_ref="fundamentals:profile",
    )

    assert [fact.value for fact in facts] == [
        Decimal("1000000"),
        Decimal("200000000"),
    ]


@pytest.mark.unit
def test_rejects_unsupported_or_non_cny_expressions_without_guessing():
    for text in (
        "亏损-50亿元",
        "金额50.亿元",
        "revenue US$50 million",
        "海外收入美元50亿元",
    ):
        assert extract_chinese_monetary_facts(text, source_ref="source:bad") == ()


@pytest.mark.unit
def test_rejects_mixed_unit_range_as_ambiguous():
    facts = extract_chinese_monetary_facts(
        "预计金额50亿元~55万元。",
        source_ref="source:mixed-range",
    )

    assert facts == ()


@pytest.mark.unit
def test_renders_deterministic_structured_annotation():
    facts = extract_chinese_monetary_facts("金额50亿元。", source_ref="news:item-7")

    assert render_monetary_source_facts(facts) == (
        "## Structured Monetary Source Facts\n"
        "```json\n"
        '[{"currency":"CNY","is_range":false,"raw_text":"50亿元",'
        '"source_ref":"news:item-7","span":[2,6],"unit":"亿元",'
        '"value":"5000000000"}]\n'
        "```"
    )


@pytest.mark.unit
def test_rendered_normalized_values_are_canonical_across_source_precision():
    facts = extract_chinese_monetary_facts(
        "先披露50亿元，后写作50.00亿元。",
        source_ref="news:precision",
    )

    rendered = render_monetary_source_facts(facts)

    assert rendered.count('"value":"5000000000"') == 2


_FULLWIDTH_NUMERIC_TRANSLATION = str.maketrans(
    "0123456789,",
    "０１２３４５６７８９，",
)


@pytest.mark.unit
@given(yi_yuan=st.integers(min_value=1_000, max_value=999_999))
def test_equivalent_units_and_locale_forms_have_identical_canonical_value(yi_yuan):
    expected_value = Decimal(yi_yuan) * Decimal("100000000")
    forms = []
    for unit, numeric_value in (
        ("元", yi_yuan * 100_000_000),
        ("万元", yi_yuan * 10_000),
        ("亿元", yi_yuan),
    ):
        ascii_plain = str(numeric_value)
        ascii_grouped = f"{numeric_value:,}"
        fullwidth_grouped = ascii_grouped.translate(_FULLWIDTH_NUMERIC_TRANSLATION)
        forms.extend(
            f"{numeric_token}{unit}"
            for numeric_token in (ascii_plain, ascii_grouped, fullwidth_grouped)
        )

    text = "、".join(forms)
    facts = extract_chinese_monetary_facts(text, source_ref="source:locale-property")

    assert [fact.value for fact in facts] == [expected_value] * len(forms)
    assert [fact.raw_text for fact in facts] == forms

    expected_spans = []
    cursor = 0
    for raw_text in forms:
        expected_spans.append((cursor, cursor + len(raw_text)))
        cursor += len(raw_text) + 1
    assert [fact.span for fact in facts] == expected_spans
