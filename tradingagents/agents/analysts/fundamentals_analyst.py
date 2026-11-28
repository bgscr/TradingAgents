from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.submission import (
    ANALYST_OUTPUT_GUIDANCE,
    bind_analyst_finalizer,
    build_analyst_update,
    personal_full_tool_prompt,
    process_analyst_response,
    render_allowed_source_ref_catalog,
)
from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.evidence import AnalystEvidenceReport


def create_fundamentals_analyst(llm):
    finalizer = bind_analyst_finalizer(llm, "fundamentals")

    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        data_tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
        ]
        tools = [*data_tools, AnalystEvidenceReport]

        system_message = personal_full_tool_prompt(
            "fundamentals",
            "Review the balance sheet, income and cash flow and available company information as of the "
            "analysis date across the full requested financial window. Focus on material changes, risks and "
            "limitations. Keep disclosure snapshots supplemental, and never invent filing details or use "
            "omitted business, summary or fund-flow fields as evidence.",
        ) or (
            "You analyze company fundamentals as of the requested analysis date, using the full requested financial window available in the tools. Review the statements and available company information for decision-relevant changes, risks, and limitations. Summarize the findings instead of reproducing financial history or every table row."
            + " Use one compact summary table only if it adds information without repeating the prose. "
            + ANALYST_OUTPUT_GUIDANCE
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements."
            + " For mainland China A-shares, fundamentals may include source-labeled disclosure snapshots; treat them as supplemental company-event context and do not invent missing filing details."
            + " When the analysis is complete, submit it through AnalystEvidenceReport: put human-readable Markdown in report_markdown and every decision-relevant factual premise in material_claims. Each claim must contain only claim_id, statement, one exact source_ref, and an exact contiguous source_quote."
            + " Record synthetic-data warnings and missing or inapplicable bank metrics as evidence_notes, not as financial premises. Preserve their limitations in the report. An available financial table does not make every metric available; omitted business, summary or fund-flow sources provide no financial evidence. Notes never satisfy the evidence required for a financial conclusion."
            + get_language_instruction()
        )
        system_message += "\n\n" + render_allowed_source_ref_catalog(
            state["messages"],
            state["company_of_interest"],
            current_date,
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

        response = chain.invoke(state["messages"])
        result = process_analyst_response(
            response,
            finalizer=finalizer,
            analyst="fundamentals",
            ticker=state["company_of_interest"],
            trade_date=current_date,
            messages=state["messages"],
        )
        return build_analyst_update(state, result, "fundamentals_report")

    return fundamentals_analyst_node
