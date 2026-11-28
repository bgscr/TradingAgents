"""Presentation-only language and advisory stages; no decision authority."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from tradingagents.evidence_diagnostics import (
    DIAGNOSTIC_LABELS,
    NEWS_DIAGNOSTIC_LABELS,
    claim_diagnostic,
    news_diagnostic_codes,
    source_diagnostic,
)


def is_chinese(language):
    return str(language or "English").strip().casefold() in {
        "chinese",
        "simplified chinese",
        "zh",
        "zh-cn",
        "zh_cn",
        "中文",
        "简体中文",
    }


_ADVISORY_RATINGS = {
    "research": {"Buy", "Overweight", "Hold", "Underweight", "Sell"},
    "trader": {"Buy", "Hold", "Sell"},
}
_RATING_ZH = {"Buy": "买入", "Overweight": "增配", "Hold": "持有",
              "Underweight": "减配", "Sell": "卖出"}


def advisory_direction_projection(state, stage, rating, body):
    """Optional display metadata after structured rendering, never decision evidence.

    Missing binding disables comparison; it must not turn successful rendering
    into a failed model call. No I/O, extraction from prose, or source certification.
    """
    if not isinstance(state, dict) or stage not in _ADVISORY_RATINGS:
        return None
    if type(rating) is not str or rating not in _ADVISORY_RATINGS[stage]:
        return None
    run_id, symbol, as_of = (state.get(key) for key in ("run_id", "company_of_interest", "trade_date"))
    if not (type(run_id) is str and re.fullmatch(r"run:[a-f0-9]{64}", run_id)
            and type(symbol) is str and re.fullmatch(r"[A-Z0-9.^_-]{1,32}", symbol)
            and type(as_of) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of)
            and type(body) is str and 0 < len(body) <= 1_000_000):
        return None
    header = "Recommendation" if stage == "research" else "Action"
    if not body.startswith(f"**{header}**: {rating}\n\n"):
        return None
    try:
        date.fromisoformat(as_of)
        digest = sha256(body.encode("utf-8")).hexdigest()
    except (ValueError, UnicodeError):
        return None
    return {"version": "advisory-direction-v1", "stage": stage, "rating": rating,
            "run_id": run_id, "symbol": symbol, "as_of": as_of, "body_sha256": digest}


def _bound_advisory_rating(state, stage, body, decision):
    record = state.get(f"{stage}_advisory_direction")
    if not isinstance(record, dict) or decision is None:
        return None
    expected = advisory_direction_projection(state, stage, record.get("rating"), body)
    if (expected is None or record != expected
            or expected["symbol"] != decision.instrument.symbol
            or expected["as_of"] != decision.as_of_date.isoformat()):
        return None
    return expected["rating"]


def _display_decimal(value):
    # Canonical JSON can preserve numeric values as strings. This normalizes
    # display only, after source/fact equality; it never repairs evidence.
    if type(value) not in (Decimal, int, str):
        return None
    if isinstance(value, str) and (len(value) > 80 or not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value)):
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and abs(number) < Decimal("1e12") else None


def _exact_percent(value):
    sign, digits, exponent = value.as_tuple()
    return f"{Decimal((sign, digits, exponent + 2))}%"


def decision_scope_markdown(state, decision, evidence, language):
    """Explain the gated rule scope, without changing or inferring a direction."""
    chinese = is_chinese(language)
    lines = ["## 先读：评级依据与辅助建议" if chinese else
             "## Read first: rule scope and advisory suggestions", ""]
    if decision is None:
        lines.append("本次未形成经验证的交易决定；辅助建议不能替代缺失的决定依据。无法自动比较评级。"
                     if chinese else "No validated trading decision was produced. Advisory suggestions cannot replace missing decision evidence; ratings cannot be compared automatically.")
        return "\n".join(lines) + "\n"
    rating = decision.rating.value
    lines.append(f"**规则评级：{_RATING_ZH[rating]}。** 由通过门禁的确定性规则授权，不是所有分析员意见的综合投票。"
                 if chinese else f"**Rule-based rating: {rating}.** Authorized by gated deterministic rules, not a vote across all analysts.")
    sources = {fact.fact_id: fact for fact in evidence.source_facts} if evidence is not None else {}
    bound = bool(decision.facts) and all(
        fact.fact_id in sources and all(
            getattr(sources[fact.fact_id], key) == getattr(fact, key)
            for key in ("canonical_field", "normalized_value", "unit", "instrument_symbol",
                        "effective_date", "artifact_sha256", "source_ref", "calculation_lineage")
        ) for fact in decision.facts
    )
    if bound:
        lines.append(f"本次采用 {len(decision.assertions)} 条经验证断言、{len(decision.facts)} 项来源事实；规则和来源编号见下方明细。"
                     if chinese else f"This decision uses {len(decision.assertions)} validated assertions and {len(decision.facts)} source facts; rule and source identifiers appear below.")
        if len(decision.assertions) == len(decision.facts) == 1:
            rule, fact = decision.assertions[0], decision.facts[0]
            value, threshold = _display_decimal(fact.normalized_value), _display_decimal(rule.threshold)
            comparisons = {"greater_than_or_equal": "≥", "less_than_or_equal": "≤"}
            if (rule.rule_id in {"market.return_20d.buy", "market.return_20d.sell"}
                    and rule.rule_version == "1.0" and rule.horizon.count == 20
                    and rule.horizon.unit.value == "trading_days"
                    and rule.fact_ids == (fact.fact_id,)
                    and fact.canonical_field == "market.close_return_20d" and fact.unit == "ratio"
                    and value is not None and threshold is not None
                    and rule.comparator in comparisons):
                basis = "前复权" if fact.calculation_lineage and fact.calculation_lineage.adjustment_basis == "qfq" else ""
                comparison = f"{value * 100:.2f}% {comparisons[rule.comparator]} {threshold * 100:.2f}%"
                lines.append(f"本次规则依据：20个交易日{basis}收盘价格收益 {comparison}。该评级只采用这项价格规则；五年趋势、资金流、融资余额及模型建议未加入本次评级依据。"
                             if chinese else f"Rule basis: 20-trading-session close-price return {comparison}. Only this price rule supports this rating; multiyear trend, moneyflow, financing balance and model commentary were not added to its premises.")
            elif (rating == rule.target_rating.value == "Hold"
                  and rule.rule_id == "market.return_20d.hold" and rule.rule_version == "1.0"
                  and rule.predicate_id == "market_return_threshold"
                  and rule.comparator == "absolute_value_less_than"
                  and rule.horizon.count == 20 and rule.horizon.unit.value == "trading_days"
                  and rule.fact_ids == (fact.fact_id,)
                  and fact.canonical_field == "market.close_return_20d" and fact.unit == "ratio"
                  and value is not None and threshold is not None and value.copy_abs() < threshold):
                qfq = fact.calculation_lineage and fact.calculation_lineage.adjustment_basis == "qfq"
                # Preserve the strict inequality at the threshold.
                comparison = f"|{_exact_percent(value)}| < {_exact_percent(threshold)}"
                lines.append(
                    f"本次评级只依据这一项市场事实。20 个交易日{'前复权' if qfq else ''}收盘价格收益满足 `{comparison}`。"
                    "财报与融资余额仅供参考，新闻、资金流和模型建议均未参与本次评级。"
                    if chinese else
                    f"The only market fact used for the rating is the 20-trading-session "
                    f"{'front-adjusted ' if qfq else ''}close-price return, which satisfies `{comparison}`. "
                    "The financial statements and financing balance are advisory. "
                    "News, moneyflow and model suggestions are outside the rating basis."
                )
    else:
        lines.append("当前展示输入缺少可核对的决定事实，无法自动比较；不以辅助正文补齐。"
                     if chinese else "Decision facts cannot be reconciled with this display input; automatic comparison is unavailable, and commentary cannot fill the gap.")
    selected_sources = state.get("personal_report_sources")
    if isinstance(selected_sources, tuple):
        if "news" in selected_sources:
            lines.append("发行人新闻正文未核验。" if chinese else "Issuer news article bodies were unverified.")
        pending = []
        if "margin_balance" in selected_sources:
            pending.append("融资余额在报告发布时尚未核验。" if chinese else
                           "Financing balance was unverified at publication.")
        if "eastmoney_fundflow_ratio" in selected_sources:
            pending.append("东财资金流占比在报告发布时尚未核验。" if chinese else
                           "Eastmoney flow ratio was unverified at publication.")
        if pending:
            pending.append("补充核验的最终状态见本次运行目录的 `status.json`。" if chinese else
                           "See `status.json` in the run directory for the final supplemental status.")
            lines.append((" " if not chinese else "").join(pending))
    debate = state.get("investment_debate_state")
    research = debate.get("judge_decision") if isinstance(debate, dict) else None
    research = research or state.get("investment_plan")
    for stage, zh, en, body in (
        ("research", "研究建议", "Research suggestion", research),
        ("trader", "交易建议", "Trader suggestion", state.get("trader_investment_plan")),
    ):
        advisory = _bound_advisory_rating(state, stage, body, decision) if bound else None
        if advisory is None:
            text = "未保留可核对的结构化建议，无法自动比较；不从正文关键词推断评级。" if chinese else "No reconciled structured suggestion is available; no rating is inferred from prose."
        else:
            relation = "相同" if advisory == rating else "不同"
            text = (f"{_RATING_ZH[advisory]}；标签与规则评级{relation}。" if chinese else
                    f"{advisory}; its label {'matches' if advisory == rating else 'differs from'} the rule-based rating.")
        lines.append(f"- {zh if chinese else en}：{text}")
    lines.append("研究采用五档建议、交易采用三档动作；标签相同不代表依据相同，标签不同也不证明来源事实冲突。结构化字段与正文的一致性不认证出处或投资事实，辅助建议不改变规则评级。缺失来源不等于没有风险，覆盖率不代表预测准确性。"
                 if chinese else "Research uses five labels and trading uses three actions. Matching labels need not share premises; different labels do not prove conflicting source facts. Metadata/body consistency does not authenticate origin or investment facts, and advisory suggestions do not change the rule rating. Missing sources do not mean no risk; coverage is not predictive accuracy.")
    return "\n\n".join(lines) + "\n"


_LABELS = {
    "History store access": "历史存储访问状态",
    "History runtime status": "历史运行时状态",
    "Trading Analysis Report": "交易分析报告",
    "Validated Trading Decision": "经验证的交易决定",
    "Run ID": "运行 ID",
    "Audit SHA-256": "审计 SHA-256",
    "Audit created": "审计创建时间",
    "Lifecycle Status": "运行状态",
    "Terminal Outcome Kind": "最终结果类型",
    "Evidence Integrity Status": "证据完整性状态",
    "Configuration Digest": "配置摘要",
    "Rating": "评级",
    "Decision ID": "决定 ID",
    "Context ID": "上下文 ID",
    "Instrument": "标的",
    "As of": "截至日期",
    "Decision horizon": "决定期限",
    "Strategy Rule Registry": "策略规则注册表",
    "Calculation Registry": "计算注册表",
    "Coverage": "覆盖情况",
    "Source Availability Coverage": "来源可用性覆盖率",
    "Validated Fact Coverage": "验证事实覆盖率",
    "Decision Assertion Coverage": "决定断言覆盖率",
    "Authoritative Market Snapshot": "权威市场快照",
    "Snapshot ID": "快照 ID",
    "Snapshot identity version": "快照标识版本",
    "Canonical manifest digest": "规范清单摘要",
    "Canonical manifest": "规范清单",
    "Acquisition Outcomes": "采集结果",
    "Provider": "提供方",
    "Capability": "能力",
    "Attempt": "尝试次数",
    "Retrieved at": "获取时间",
    "Outcome": "结果",
    "Unavailable reason": "不可用原因",
    "Retryable": "可重试",
    "Retry-After": "重试等待时间",
    "Physical Provider Attempts": "提供方实际请求",
    "Total physical-attempt count": "实际请求总数",
    "Attempt event ID": "请求事件 ID",
    "Request identity": "请求标识",
    "Sequence identity": "序列标识",
    "Upstream Service Identity": "上游服务标识",
    "Capacity scope": "容量范围",
    "Attempted at": "请求时间",
    "Pacing/permit event": "节流许可事件",
    "Pacing wait": "节流等待",
    "Typed outcome": "类型化结果",
    "Status code": "状态码",
    "Error code": "错误码",
    "Cooldown changed": "冷却状态变化",
    "Cooldown until": "冷却截止",
    "Final physical-attempt count": "最终实际请求数",
    "Canonical Source Facts": "规范来源事实",
    "Canonical value": "规范数值",
    "Effective date": "生效日期",
    "Fact ID": "事实 ID",
    "Artifact": "证据对象",
    "Source span": "原文区间",
    "Calculation": "计算",
    "Input snapshot": "输入快照",
    "Effective range": "有效区间",
    "Observations used": "观测数",
    "Adjustment basis": "复权基准",
    "Implementation": "实现版本",
    "Result digest": "结果摘要",
    "Decision Assertions": "决定断言",
    "Strategy Rule": "策略规则",
    "Target rating": "目标评级",
    "Polarity": "方向",
    "Predicate": "判定条件",
    "Horizon": "期限",
    "Source Fact IDs": "来源事实 ID",
    "Evaluation digest": "求值摘要",
    "Material Degradation": "实质性降级说明",
    "Analysis Outcome": "分析结果",
    "Non-Directional Analysis Outcome": "非方向性分析结果",
    "Reason Code": "原因代码",
    "Deterministic Blockers": "确定性阻断原因",
    "Current Tradeability": "当前可交易状态",
    "Latest Genuinely Traded Close": "最近实际成交收盘价",
    "Reason": "原因",
    "Readiness": "证据就绪状态",
}

_OUTCOME_ZH = {
    "preflight_blocked": "确定性预检要求尚未满足决定条件，分析在模型工作开始前停止。",
    "admission_blocked": "必需证据与规则支持的断言尚未满足决定条件，分析在观点综合前停止。",
    "decision_gate_blocked": "最终方向提议未通过确定性决定门禁，分析结束但未形成交易决定。",
    "portfolio_gate_blocked": "投资组合经理未能形成满足决定条件的证据链，分析结束但未形成交易决定。",
    "shadow_mode_blocked": "影子证据模式仅用于诊断，分析结束但未形成交易决定。",
    "instrument_currently_suspended": "大陆市场标的当前停牌、不可交易，分析结束但未形成交易决定。",
}
_BLOCKER_ZH = {
    "identity_unavailable": "权威标的身份不可用或无效。",
    "snapshot_unavailable": "权威市场快照不可用。",
    "snapshot_invalid": "权威市场快照未通过确定性验证。",
    "history_insufficient": "权威快照中的可用历史数据不足。",
    "required_evidence_unavailable": "必需证据能力不可用或不适用。",
    "optional_evidence_unavailable": "部分可选证据不可用。",
    "required_evidence_conflicted": "必需证据存在尚未解决的实质冲突。",
    "decision_configuration_invalid": "确定性决定配置不可用或无效。",
    "strategy_rule_invalid": "没有有效的已注册策略规则应用可授权该方向。",
    "direction_context_invalid": "经验证的方向上下文不可用或无效。",
    "direction_assertions_conflicted": "经验证的决定断言不能授权唯一明确的评级。",
    "direction_selection_invalid": "方向选择不可用或与其上下文不兼容。",
    "decision_assertion_invalid": "决定断言未通过确定性验证。",
    "structured_output_invalid": "必需的结构化投资组合输出不可用或无效。",
    "shadow_mode": "影子证据模式不能发布方向性决定。",
    "deterministic_gate_rejected": "确定性信任边界检查拒绝发布。",
    "instrument_currently_suspended": "最近适用的大陆市场交易时段已权威确认停牌。",
}


def localized_analysis_outcome(outcome, language):
    from tradingagents.evidence import render_analysis_outcome

    if not is_chinese(language):
        return render_analysis_outcome(outcome)
    readiness = "证据不足" if outcome.readiness.value == "insufficient" else "证据冲突"
    lines = [
        f"**Analysis Outcome:** {readiness}", "",
        f"**Reason Code:** `{outcome.reason.value}`", "",
        _OUTCOME_ZH[outcome.reason.value],
    ]
    if outcome.current_suspension is not None:
        suspension = outcome.current_suspension
        latest = str(suspension.latest_traded_close) if suspension.latest_traded_close_status == "available" else "不可用"
        lines += ["", "**Current Tradeability:** `suspended`", "",
                  f"**Latest Genuinely Traded Close:** `{latest}`"]
    lines += ["", "未形成交易决定。"]
    if outcome.diagnostic_codes:
        lines += ["", "### Deterministic Blockers", ""]
        lines += [
            f"- `{code.value}`: {_BLOCKER_ZH[code.value]}"
            for code in sorted(outcome.diagnostic_codes, key=lambda item: item.value)
        ]
    return "\n".join(lines)


def localize_report(text, language):
    """Translate template labels only; preserve quoted facts, code and IDs."""
    if not is_chinese(language):
        return text
    output, in_code = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and not line.startswith("```"):
            for english, chinese in _LABELS.items():
                line = line.replace(f"**{english}:**", f"**{chinese}:**")
                line = re.sub(
                    r"^(#{1,6} )" + re.escape(english) + r"(?=:|$)", rf"\g<1>{chinese}", line
                )
            for english, chinese in (
                ("Fact", "事实"),
                ("Assertion", "断言"),
                ("Acquisition", "采集"),
                ("Physical attempt", "实际请求"),
            ):
                line = re.sub(r"^(### )" + english + r" (\d+)", rf"\g<1>{chinese} \g<2>", line)
            if line.startswith(("- **评级:**", "- **目标评级:**")):
                for english, chinese in (("Sell", "卖出"), ("Buy", "买入"), ("Hold", "持有")):
                    line = line.removesuffix(english) + chinese if line.endswith(english) else line
            line = line.replace(
                "No predictive-confidence score is reported because no out-of-sample calibration is available.",
                "未进行样本外校准，因此不提供预测置信度。",
            )
            line = line.replace(
                "Current-analysis reuse only; `strict_history_qualified=false`.",
                "仅供当前分析复用；`strict_history_qualified=false`。",
            ).replace(
                "Stored access does not establish strict point-in-time eligibility.",
                "存储访问状态不代表已获得严格时点历史资格。",
            )
            line = line.replace(
                "Optional evidence was unavailable. The validated facts and every directional assertion above nevertheless passed the Decision Gate.",
                "部分可选证据不可用。上述经验证事实与每项方向性断言均已通过决定门禁；这不代表全部分析主张均有证据支持。",
            )
        output.append(line)
    return "\n".join(output).rstrip() + "\n"


def advisory_sections(state):
    """Extract only explicit stage fields; never promote or invent facts."""
    rows = []
    for key, name, zh in (
        ("market", "Market analyst", "市场分析师"),
        ("sentiment", "Sentiment analyst", "情绪分析师"),
        ("news", "News analyst", "新闻分析师"),
        ("fundamentals", "Fundamentals analyst", "基本面分析师"),
    ):
        rows.append((f"1_analysts/{key}.md", name, zh, state.get(f"{key}_report")))
    debate = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    rows.extend(
        [
            ("2_research/bull.md", "Bull researcher", "多头研究员", debate.get("bull_history")),
            ("2_research/bear.md", "Bear researcher", "空头研究员", debate.get("bear_history")),
            (
                "2_research/manager.md",
                "Research manager",
                "研究经理",
                debate.get("judge_decision") or state.get("investment_plan"),
            ),
            ("3_trading/trader.md", "Trader", "交易员", state.get("trader_investment_plan")),
            (
                "4_risk/aggressive.md",
                "Aggressive risk analyst",
                "激进风险分析师",
                risk.get("aggressive_history"),
            ),
            (
                "4_risk/conservative.md",
                "Conservative risk analyst",
                "保守风险分析师",
                risk.get("conservative_history"),
            ),
            (
                "4_risk/neutral.md",
                "Neutral risk analyst",
                "中立风险分析师",
                risk.get("neutral_history"),
            ),
            ("4_risk/manager.md", "Risk manager", "风险经理", risk.get("judge_decision")),
        ]
    )
    return [
        (path, name, zh, body)
        for path, name, zh, body in rows
        if isinstance(body, str) and body.strip()
    ]


def advisory_markdown(title, content, language, *, decision=None, evidence=None,
                      flow_windows_unverified=False):
    warning = (
        "辅助分析（模型生成，未经事实验证）。本节不属于权威决定依据；评级与规则决定见独立的经验证决定。"
        if is_chinese(language)
        else "Advisory Commentary (model-authored, not validated facts). This section does not determine the authoritative rating or rule-based decision."
    )
    context = []
    if flow_windows_unverified:
        context.append(
            "本节模型正文的资金流窗口与合计未逐句核验；请核对资金流增强中的窗口合计表。"
            "全采集窗口合计不能标作短期合计；表中材料不可用时，不以正文补齐。"
            if is_chinese(language) else
            "Moneyflow windows and totals in this model prose are not checked claim-by-claim; "
            "refer to the moneyflow window totals table. The acquisition-window total must not "
            "be labeled as a short-window total; prose cannot fill unavailable table evidence."
        )
    if evidence is not None:
        chinese = is_chinese(language)
        if decision is None:
            context.append("本次未形成经验证的交易决定。" if chinese
                           else "This run produced no validated trading decision.")
        else:
            rating = decision.rating.value
            context.append(
                f"当前经验证评级：{ {'Buy': '买入', 'Hold': '持有', 'Sell': '卖出'}[rating]} ({rating})。"
                if chinese else f"Authoritative rating: {rating}."
            )
        context.append(
            "下方正文按原样保留；其中不同方向、价位及交易动作均不是经验证的操作指令。"
            if chinese else
            "Original commentary is retained below; differing directions, price levels and trade actions are not validated instructions."
        )
        unsupported = [item for item in evidence.claim_validations if item.status.value == "unsupported"]
        unavailable = sum(source.status.value == "unavailable" for source in evidence.sources)
        context.append(
            f"本次运行不可用来源：{unavailable}；未支持主张：{len(unsupported)}。这些计数不代表预测可信度，未列出的正文也不因此得到验证。"
            if chinese else
            f"Unavailable sources in this run: {unavailable}; unsupported claims: {len(unsupported)}. These counts are not predictive confidence; unlisted prose is not thereby validated."
        )
        if unsupported:
            ids = [f"`{_safe_claim_id(item.claim_id)}`" for item in unsupported[:20]]
            context.append(("本次未支持主张 ID（最多列出 20 项）：" if chinese
                            else "Unsupported claim IDs in this run (up to 20): ") + ", ".join(ids))
    notes = "".join(f"> {line}\n>\n" for line in context)
    return f"# {title}\n\n> {warning}\n\n{notes}\n{content.strip()}\n"


def _safe_claim_id(value):
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) else "unpublished-claim-id"


def evidence_diagnostic_markdown(evidence, language):
    chinese = is_chinese(language)
    unavailable = [source for source in evidence.sources if source.status.value == "unavailable"]
    unsupported = [
        item for item in evidence.claim_validations if item.status.value == "unsupported"
    ]
    news_codes = news_diagnostic_codes(evidence.acquisition_outcomes)
    if not unavailable and not unsupported and not news_codes:
        return ""
    lines = [
        "## 证据缺口与未支持主张" if chinese else "## Evidence gaps and unsupported claims",
        "",
    ] if unavailable or unsupported else []
    for source in unavailable:
        details = source_diagnostic(source, evidence.acquisition_outcomes)
        reason = DIAGNOSTIC_LABELS[details.reason.value][int(chinese)]
        label = (
            "可选来源"
            if chinese and not source.required
            else "必需来源"
            if chinese
            else "Optional source"
            if not source.required
            else "Required source"
        )
        display_name = (
            {"macro_indicators": "宏观指标", "prediction_markets": "预测市场",
             "company_financials": "公司财务"}.get(
                details.capability, details.display_name
            )
            if chinese else details.display_name
        )
        lines.append(
            f"- {label}: {display_name}; `{details.capability}`; {reason} (`{details.reason.value}`)."
        )
        if details.acquisition_reason is not None:
            lines.append(
                f"  - {'采集原因' if chinese else 'Acquisition reason'}: `{details.acquisition_reason.value}`"
            )
        if (
            details.capability in {"macro_indicators", "prediction_markets"}
            and details.acquisition_reason is not None
            and details.acquisition_reason.value == "not_configured"
        ):
            lines.append(
                "  - 此采集记录显示该能力未配置，未取得可用数据；这是操作状态，不是来源不明的已采集事实。"
                if chinese else
                "  - This capability was not configured for this attempt; no usable data "
                "was acquired. This is an operational status, not an acquired fact with "
                "an unknown source."
            )
    if unsupported:
        lines += [
            "",
            (
                f"未支持主张：{len(unsupported)} 条；这些主张未计入经验证决定的事实覆盖率。"
                if chinese
                else f"Unsupported claims: {len(unsupported)}; these are outside the validated decision's fact coverage."
            ),
        ]
        for validation in unsupported:
            code = claim_diagnostic(validation)
            # Claim IDs are model supplied; do not echo arbitrary prose/URLs.
            safe_id = _safe_claim_id(validation.claim_id)
            lines.append(
                f"- `{safe_id}`: {DIAGNOSTIC_LABELS[code.value][int(chinese)]} (`{code.value}`)."
            )
            comparison = validation.comparison_evidence
            if comparison is not None:
                from tradingagents.comparison_evidence import bound_comparison, comparison_markdown

                lines.extend(comparison_markdown(bound_comparison(
                    validation, evidence.source_facts, evidence.source_artifacts), chinese=chinese))
    if news_codes:
        lines += [
            "",
            "## 新闻采集范围与限制" if chinese else "## News acquisition scope and limits",
            "",
            ("运行诊断，不是来源事实，不参与投资评分。" if chinese
             else "Operational diagnostics, not source facts; these do not affect investment scoring."),
            "",
        ]
        lines.extend(
            f"- `{code}`: {NEWS_DIAGNOSTIC_LABELS[code][int(chinese)]}."
            for code in news_codes
        )
    return "\n".join(lines) + "\n"
