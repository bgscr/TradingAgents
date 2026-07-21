from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.submission import (
    bind_analyst_finalizer,
    build_analyst_update,
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

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements."
            + " For mainland China A-shares, fundamentals may include source-labeled disclosure snapshots; treat them as supplemental company-event context and do not invent missing filing details."
            + " When the analysis is complete, submit it through AnalystEvidenceReport: put human-readable Markdown in report_markdown and every decision-relevant factual premise in material_claims. Each claim must contain only claim_id, statement, one exact source_ref, and an exact contiguous source_quote."
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
