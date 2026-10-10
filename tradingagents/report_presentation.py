"""Presentation-only language and advisory stages; no decision authority."""

import re

from tradingagents.evidence_diagnostics import (
    DIAGNOSTIC_LABELS,
    claim_diagnostic,
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


_LABELS = {
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
    "Reason": "原因",
    "Readiness": "证据就绪状态",
}


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


def advisory_markdown(title, content, language):
    warning = (
        "辅助分析（模型生成，未经事实验证）。本节不属于权威决定依据；评级与规则决定见独立的经验证决定。"
        if is_chinese(language)
        else "Advisory Commentary (model-authored, not validated facts). This section does not determine the authoritative rating or rule-based decision."
    )
    return f"# {title}\n\n> {warning}\n\n{content.strip()}\n"


def evidence_diagnostic_markdown(evidence, language):
    chinese = is_chinese(language)
    unavailable = [source for source in evidence.sources if source.status.value == "unavailable"]
    unsupported = [
        item for item in evidence.claim_validations if item.status.value == "unsupported"
    ]
    if not unavailable and not unsupported:
        return ""
    lines = [
        "## 证据缺口与未支持主张" if chinese else "## Evidence gaps and unsupported claims",
        "",
    ]
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
        lines.append(
            f"- {label}: {details.display_name}; `{details.capability}`; {reason} (`{details.reason.value}`)."
        )
        if details.acquisition_reason is not None:
            lines.append(
                f"  - {'采集原因' if chinese else 'Acquisition reason'}: `{details.acquisition_reason.value}`"
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
            safe_id = (
                validation.claim_id
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", validation.claim_id)
                else "unpublished-claim-id"
            )
            lines.append(
                f"- `{safe_id}`: {DIAGNOSTIC_LABELS[code.value][int(chinese)]} (`{code.value}`)."
            )
    return "\n".join(lines) + "\n"
