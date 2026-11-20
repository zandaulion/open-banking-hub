"""Read-only MCP server over the redacted ledger.

Opens SQLite with mode=ro and only calls bankhub.queries, which reads the
redacted tables. No tool accepts SQL.
"""

from contextlib import closing
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import queries
from .config import load_config
from .db import connect_readonly

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def build_server(db_path=None) -> MCPServer:
    path = db_path or load_config().db_path
    mcp = MCPServer("bankhub", instructions=(
        "Personal banking data (read-only, redacted). Amounts are negative for money out. "
        "Use get_spending_summary for 'where did my money go' questions: it excludes transfers between "
        "the owner's own accounts and lists one-off payments separately. Each transaction has a kind: "
        "spending, refund, income, other_in, one_off or internal. Unrecognised payees are labels such as "
        "'individual' or 'unknown_merchant', never names."))

    def db():
        return closing(connect_readonly(path))

    @mcp.tool(annotations=READ_ONLY)
    def get_sync_status() -> dict:
        """Last sync per bank, consent expiry and the date of the newest transaction."""
        with db() as conn:
            return queries.sync_status(conn)

    @mcp.tool(annotations=READ_ONLY)
    def get_balances() -> dict:
        """Latest balance per account alias, plus totals in RON and EUR (BNR rate)."""
        with db() as conn:
            return queries.balances(conn)

    @mcp.tool(annotations=READ_ONLY)
    def query_transactions(date_from: str | None = None, date_to: str | None = None, account: str | None = None,
                           category: str | None = None, merchant: str | None = None,
                           min_amount_ron: float | None = None, max_amount_ron: float | None = None,
                           currency: str | None = None, direction: str | None = None,
                           include_internal_transfers: bool = True, include_pending: bool = True,
                           limit: int = 100, offset: int = 0) -> dict:
        """Filter the redacted ledger. Dates are YYYY-MM-DD; direction is 'debit' or 'credit';
        amount filters apply to the absolute RON value; at most 500 rows per call."""
        with db() as conn:
            return queries.query_transactions(
                conn, date_from=date_from, date_to=date_to, account=account, category=category,
                merchant=merchant, min_amount=min_amount_ron, max_amount=max_amount_ron, currency=currency,
                direction=direction, include_internal=include_internal_transfers,
                include_pending=include_pending, limit=limit, offset=offset)

    @mcp.tool(annotations=READ_ONLY)
    def get_spending_summary(date_from: str | None = None, date_to: str | None = None,
                             group_by: str = "category", include_internal_transfers: bool = False) -> dict:
        """Regular spending (net of refunds) grouped by category, subcategory, merchant, month or account,
        plus income (salary-like regular money in and interest), other money in, what is left, and the
        one-offs listed separately (very large payments such as property or loan closure, kept out of the
        totals). Defaults to the current month. Transfers between own accounts are never counted."""
        if not date_from:
            date_from, end = queries.month_bounds(date.today().strftime("%Y-%m"))
            date_to = date_to or end
        with db() as conn:
            return queries.spending_summary(conn, date_from=date_from, date_to=date_to or date.today().isoformat(),
                                            group_by=group_by, include_internal=include_internal_transfers)

    @mcp.tool(annotations=READ_ONLY)
    def get_recurring_payments(include_missed: bool = True) -> dict:
        """Subscriptions and recurring bills with cadence, next expected date, price changes and
        monthly/annual totals in RON."""
        with db() as conn:
            return queries.recurring_payments(conn, include_missed=include_missed)

    return mcp


def main() -> None:
    build_server().run(transport="stdio")
