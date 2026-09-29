import frappe
from frappe.utils import flt

from ksa_compliance.standard_doctypes.tax_category import map_tax_category
from .service import get_right_fieldname, dataclass_to_frappe_dict
from .models import TaxCategory, TaxCategoryByItems, TaxTotal, TaxSubtotal, AllowanceCharge

from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice
from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry
from ksa_compliance.invoice import get_zatca_discount_reason_by_name

from ksa_compliance.translation import ft
from ksa_compliance.throw import fthrow


def create_tax_categories(doc: SalesInvoice | PaymentEntry, item_lines: list, is_tax_included: bool) -> dict:
    tax_category_map = frappe._dict()
    sales_taxes_and_charges_template = doc.get(get_right_fieldname('taxes_and_charges', doc.doctype))
    item_tax_templates = [row.item_tax_template for row in item_lines if row.item_tax_template]
    if sales_taxes_and_charges_template and not item_tax_templates:
        tax_category_id = frappe.db.get_value(
            'Sales Taxes and Charges Template', sales_taxes_and_charges_template, 'tax_category'
        )
        if not tax_category_id:
            fthrow(
                msg=ft(
                    'Please set Tax Category on Sales Taxes and Charges Template $sales_taxes_and_charges_template.',
                    sales_taxes_and_charges_template=sales_taxes_and_charges_template,
                )
            )
        zatca_category = frappe.db.get_value('Tax Category', tax_category_id, 'custom_zatca_category')
        if not zatca_category:
            fthrow(
                msg=ft(
                    'Please set custom ZATCA category on Tax Category $tax_category_id.',
                    tax_category_id=tax_category_id,
                )
            )
        tax_category_percent = frappe.db.get_value(
            'Sales Taxes and Charges', {'parent': sales_taxes_and_charges_template}, 'rate'
        )

        tax_category_id = map_tax_category(tax_category_id=tax_category_id)
        tax_category = TaxCategory(
            zatca_tax_category_id=tax_category_id, percent=tax_category_percent, tax_scheme_id='VAT'
        )

        for row in item_lines:
            row.tax_category = dataclass_to_frappe_dict(tax_category)
        tax_category_by_items = TaxCategoryByItems(tax_category=tax_category, items=[row for row in item_lines])
        tax_category_by_items_cls = tax_category_map.setdefault(
            zatca_category + str(tax_category_percent), tax_category_by_items
        )
        return tax_category_map

    check_item_tax_template(doc, item_lines, sales_taxes_and_charges_template)

    for row in item_lines:
        if not row.item_tax_template and sales_taxes_and_charges_template:
            tax_category_id = frappe.db.get_value(
                'Sales Taxes and Charges Template', sales_taxes_and_charges_template, 'tax_category'
            )
            zatca_tax_category = map_tax_category(tax_category_id=tax_category_id)
            tax_category_percent = frappe.db.get_value(
                'Sales Taxes and Charges', {'parent': sales_taxes_and_charges_template}, 'rate'
            )
        else:
            zatca_tax_category = map_tax_category(item_tax_template_id=row.item_tax_template)
            tax_category_percent = frappe.db.get_value(
                'Item Tax Template Detail', {'parent': row.item_tax_template}, 'tax_rate'
            )
        tax_category = TaxCategory(
            zatca_tax_category_id=zatca_tax_category, percent=tax_category_percent, tax_scheme_id='VAT'
        )

        row.tax_category = dataclass_to_frappe_dict(tax_category)
        tax_category_by_items = TaxCategoryByItems(tax_category=tax_category, items=[])
        tax_category_by_items_cls = tax_category_map.setdefault(
            zatca_tax_category.tax_category_code + str(tax_category_percent), tax_category_by_items
        )
        tax_category_by_items_cls.items.append(row)
    return tax_category_map


def check_item_tax_template(doc: SalesInvoice, item_lines: list, sales_taxes_and_charges_template: str) -> None:
    invalid_items = [row.item_name for row in item_lines if not row.item_tax_template]
    if invalid_items and not sales_taxes_and_charges_template:
        frappe.throw(
            'Please Include Sales Taxes and Charges Template on invoice\nOr include Item Tax Template on {0}'.format(
                ', '.join(invalid_items)
            )
        )


def canonical_money(val: float | None) -> float:
    if not val:
        return 0.0
    r = flt(val, 2)
    return 0.0 if abs(r) < 1e-9 else r


MAX_ROUNDING_DRIFT = 0.05
MAX_ROUNDING_DRIFT_PER_LINE = 0.01


def create_tax_total(
    tax_categories: dict,
    total_taxes_and_charges: float | None = None,
    allowance_total_amount: float | None = None,
) -> dict:
    amounts_by_category = {key: _get_amounts(tax_categories[key]) for key in tax_categories}

    # Reconcile category discounts if an invoice-level document allowance is provided
    if allowance_total_amount is not None:
        canonical_doc_allowance = canonical_money(allowance_total_amount)
        if len(tax_categories) == 1:
            key = next(iter(tax_categories))
            amounts_by_category[key].total_discount = canonical_doc_allowance
            amounts_by_category[key].taxable_amount = canonical_money(
                amounts_by_category[key].line_extension - canonical_doc_allowance
            )
        elif canonical_doc_allowance > 0.0:
            raw_discounts = {key: amounts_by_category[key].total_discount for key in tax_categories}
            total_raw_discount = sum(raw_discounts.values())
            if total_raw_discount > 0.0:
                residue_key = max(tax_categories, key=lambda k: raw_discounts[k])
                allocated = 0.0
                for key in tax_categories:
                    if key == residue_key:
                        continue
                    cat_discount = canonical_money(canonical_doc_allowance * raw_discounts[key] / total_raw_discount)
                    amounts_by_category[key].total_discount = cat_discount
                    amounts_by_category[key].taxable_amount = canonical_money(
                        amounts_by_category[key].line_extension - cat_discount
                    )
                    allocated += cat_discount
                cat_discount = canonical_money(canonical_doc_allowance - allocated)
                amounts_by_category[residue_key].total_discount = cat_discount
                amounts_by_category[residue_key].taxable_amount = canonical_money(
                    amounts_by_category[residue_key].line_extension - cat_discount
                )

    tax_amount_by_category = _allocate_tax_amounts(tax_categories, amounts_by_category, total_taxes_and_charges)

    tax_sub_totals = []
    tax_amount = 0.0
    taxable_amount = 0.0
    total_discount = 0.0
    for key in tax_categories:
        amounts = amounts_by_category[key]
        tax_sub_total = TaxSubtotal(
            taxable_amount=amounts.taxable_amount,
            tax_amount=tax_amount_by_category[key],
            tax_category=tax_categories[key].tax_category,
            total_discount=amounts.total_discount,
        )
        tax_amount += tax_sub_total.tax_amount
        taxable_amount += amounts.taxable_amount
        total_discount += amounts.total_discount
        tax_sub_totals.append(tax_sub_total)

    return dataclass_to_frappe_dict(
        TaxTotal(
            tax_amount=canonical_money(tax_amount),
            taxable_amount=canonical_money(taxable_amount),
            tax_subtotal=tax_sub_totals,
        )
    )


def _allocate_tax_amounts(
    tax_categories: dict, amounts_by_category: dict, total_taxes_and_charges: float | None
) -> dict:
    """Decide the VAT amount (BT-117) of each tax category."""
    line_amounts = {key: canonical_money(amounts_by_category[key].tax_amount) for key in tax_categories}
    if total_taxes_and_charges is None:
        return line_amounts

    target = canonical_money(total_taxes_and_charges)
    if canonical_money(target - sum(line_amounts.values())) == 0.0:
        return line_amounts

    expected = {
        key: canonical_money(
            amounts_by_category[key].taxable_amount * flt(tax_categories[key].tax_category.percent or 0.0) / 100
        )
        for key in tax_categories
    }
    expected_total = sum(expected.values())
    if not expected_total:
        return line_amounts

    line_count = sum(len(tax_categories[key].items) for key in tax_categories)
    max_drift = max(MAX_ROUNDING_DRIFT, MAX_ROUNDING_DRIFT_PER_LINE * line_count)
    if abs(target - expected_total) > max_drift:
        return line_amounts

    # The residue from rounding each share goes to the category carrying the most VAT
    residue_key = max(tax_categories, key=lambda key: expected[key])
    allocated = 0.0
    result = {}
    for key in tax_categories:
        if key == residue_key:
            continue
        result[key] = canonical_money(target * expected[key] / expected_total)
        allocated += result[key]
    result[residue_key] = canonical_money(target - allocated)
    return result


def _get_amounts(tax_category: TaxCategoryByItems) -> frappe._dict:
    line_extension = sum(canonical_money(row.amount) for row in tax_category.items)
    raw_discount = sum(flt(row.amount) - flt(row.net_amount) for row in tax_category.items)
    line_tax = sum(canonical_money(row.tax_amount) for row in tax_category.items)

    amounts = frappe._dict()
    amounts.line_extension = canonical_money(line_extension)
    amounts.total_discount = canonical_money(raw_discount)
    amounts.taxable_amount = canonical_money(amounts.line_extension - amounts.total_discount)
    amounts.tax_amount = canonical_money(line_tax)

    return amounts


def create_allowance_charge(doc: SalesInvoice | PaymentEntry, tax_total: frappe._dict) -> list:
    allowance_charges = []
    discount_reason, discount_reason_code = None, None
    if doc.doctype == 'Sales Invoice' and doc.discount_amount:
        zatca_discount_reason = get_zatca_discount_reason_by_name(name=doc.custom_zatca_discount_reason)
        discount_reason = zatca_discount_reason.name
        discount_reason_code = zatca_discount_reason.code

    for row in tax_total.tax_subtotal:
        discount_amt = canonical_money(row.total_discount)
        if doc.doctype == 'Payment Entry':
            discount_amt = 0.0
        allowance_charge = AllowanceCharge(
            tax_category=row.tax_category,
            charge_indicator='false',
            allowance_charge_reason=discount_reason,
            allowance_charge_reason_code=discount_reason_code,
            amount=discount_amt,
        )
        allowance_charges.append(dataclass_to_frappe_dict(allowance_charge))
    return allowance_charges
