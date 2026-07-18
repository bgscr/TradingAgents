from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_global_news,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)
from tradingagents.evidence import (
    AnalystEvidenceReport,
    evidence_sources_from_tool_messages,
    merge_evidence_sources,
    merge_material_claims,
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        data_tools = [
            get_news,
            get_global_news,
            get_macro_indicators,
            get_prediction_markets,
        ]
        tools = [*data_tools, AnalystEvidenceReport]

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(ticker, start_date, end_date) for {asset_label}-specific news by ticker symbol, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions. For mainland China A-shares, ticker news may include source-labeled announcements, industry, sector, and policy snapshots; separate company-specific events from broad sector or policy context. When the analysis is complete, submit it through AnalystEvidenceReport: put human-readable Markdown in report_markdown and every decision-relevant factual premise in material_claims with source_refs."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(
            tool_names=", ".join(
                [tool.name for tool in data_tools]
                + [AnalystEvidenceReport.__name__]
            )
        )
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""

        report_submission = next(
            (
                tool_call
                for tool_call in result.tool_calls
                if tool_call["name"] == AnalystEvidenceReport.__name__
            ),
            None,
        )
        if report_submission is not None:
            submitted = AnalystEvidenceReport.model_validate(report_submission["args"])
            claims = tuple(
                claim.model_copy(update={"analyst": "news"})
                for claim in submitted.material_claims
            )
            report = submitted.report_markdown
            result = AIMessage(content=report)
        elif len(result.tool_calls) == 0:
            report = result.content

        update = {
            "messages": [result],
            "news_report": report,
        }
        if report_submission is not None:
            merged_evidence = merge_material_claims(
                state.get("evidence_state"),
                claims,
            )
            merged_evidence = merge_evidence_sources(
                merged_evidence,
                evidence_sources_from_tool_messages(state["messages"], claims),
            )
            update["evidence_state"] = merged_evidence.model_dump(mode="json")
        return update

    return news_analyst_node
