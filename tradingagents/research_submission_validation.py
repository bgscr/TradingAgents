"""Validate graph and observation references without retaining submitted text."""


def rejection_reason(op, stages, market_rows, financial_rows, required_roles):
    keys = set(op.stages)
    if len(keys) != len(op.stages):
        return "duplicate_stage"
    if not keys <= stages.keys():
        return "unknown_stage"
    if {stages[key]["role"] for key in keys} != set(required_roles):
        return "missing_role"
    if op.support not in keys or op.opposition not in keys:
        return "opposing_reference_missing"
    if {stages[op.support]["role"], stages[op.opposition]["role"]} != {"bull", "bear"}:
        return "opposing_roles_invalid"
    if op.market_fact not in market_rows:
        return "unknown_market_fact"
    if op.financial_fact not in financial_rows:
        return "unknown_financial_fact"
    return None
