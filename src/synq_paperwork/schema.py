"""The shape of the data we pull out of an invoice.

One definition is used everywhere:
  - the fake-invoice generator writes its answer keys in this shape
  - the AI (from Step 2) must return exactly this shape (structured output)
  - the evals compare the two, field by field
  - later, the Google Sheets step writes these fields as columns

Money is stored as a plain number with 2 decimals. The evals compare numbers with a
tolerance of half a cent, so 12.5 and 12.50 count as the same.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ColumnValue(BaseModel):
    """A value from a printed column that has no field of its own (unit of measure, tax code, weight...).
    Nothing printed is ever dropped: whatever doesn't fit a field is copied here, word for word."""
    column: str = Field(description="The column's header exactly as printed, e.g. 'UOM', 'Tax', 'Freight'")
    value: str = Field(description="What this row shows in that column, exactly as printed")


class LineItem(BaseModel):
    sku: str = Field(default="", description="The supplier's item/product code, or '' if the line has none")
    description: str = Field(description="What was bought, exactly as written (join wrapped lines with a space)")
    quantity: float
    unit_price: float = Field(description="Price for one unit")
    amount: float = Field(description="The line total as printed (after any discount printed on the line)")
    discount_percent: float | None = Field(
        default=None, description="Only if the invoice has a discount % COLUMN: this row's value (5.0 = 5%)")
    discount_amount: float | None = Field(
        default=None, description="Only if the invoice has a discount COLUMN in money: this row's value")
    other_columns: list[ColumnValue] = Field(
        default=[], description="Every OTHER printed column of this row that has no field above")


class TotalsRow(BaseModel):
    """One row of the totals block between the first subtotal and the amount due."""
    label: str = Field(description="The row's label exactly as printed")
    amount: float = Field(description="Its amount; discounts and credits are negative")
    kind: Literal["tax", "discount", "charge", "subtotal"] = Field(
        description="tax; discount (or credit); charge (freight, fees, deposits...); "
                    "subtotal = a running total like 'Adjusted subtotal' that is NOT added again")


class Invoice(BaseModel):
    supplier_name: str = Field(description="The company that sent the invoice")
    invoice_number: str
    invoice_date: str = Field(description="Date of the invoice as YYYY-MM-DD")
    currency: str = Field(description="3-letter code, e.g. USD")
    lines: list[LineItem] = Field(description="Every line item, in order. Not subtotals, not 'carried forward' rows.")
    subtotal: float = Field(description="Total of all lines before tax")
    tax: float
    total: float = Field(description="The amount due (subtotal + tax)")
    totals_rows: list[TotalsRow] = Field(
        default=[], description="Every row between the subtotal and the amount due, in order (taxes, "
                                "discounts, freight, running subtotals). Empty if there is only one tax row.")


class PageData(BaseModel):
    """What the AI reads from ONE page (Step 3). A long invoice = many PageData, stitched into one Invoice.
    The 'control totals' printed on the page are kept too: Step 4 checks the lines against them."""
    supplier_name: str = Field(default="", description="Only if printed on this page, else ''")
    invoice_number: str = Field(default="", description="Only if printed on this page, else ''")
    invoice_date: str = Field(default="", description="YYYY-MM-DD, only if printed on this page, else ''")
    currency: str = Field(default="", description="3-letter code if shown or clear from the page (e.g. $ -> USD), else ''")
    columns: list[str] = Field(default=[], description="Every column header printed above the line items on this "
                                                       "page, left to right, exactly as printed")
    brought_forward: float | None = Field(default=None, description="'Balance brought forward' amount at the TOP of the page, if printed")
    lines: list[LineItem] = Field(description="Every line item on this page, in order")
    page_subtotal: float | None = Field(default=None, description="'Subtotal this page' amount, if printed")
    carried_forward: float | None = Field(default=None, description="'Carried forward' amount at the BOTTOM of the page, if printed")
    subtotal: float | None = Field(default=None, description="Invoice subtotal/net amount, only if the final totals block is on this page")
    tax: float | None = Field(default=None, description="Only if the final totals block is on this page")
    total: float | None = Field(default=None, description="Amount due, only if the final totals block is on this page")
    totals_rows: list[TotalsRow] = Field(
        default=[], description="Only if the final totals block is on this page: every row between the "
                                "subtotal and the amount due, in order. Empty if there is only one tax row.")
