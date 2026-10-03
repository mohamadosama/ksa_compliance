from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch
import xml.etree.ElementTree as ET

import frappe
from frappe.tests.utils import FrappeTestCase

from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
    SalesInvoiceAdditionalFields,
)
from ksa_compliance.output_models.e_invoice_output_model import Einvoice
from ksa_compliance.output_models.models import ZatcaTaxCategory
from ksa_compliance.generate_xml import generate_xml_file


class TestZatcaMonetaryRounding(FrappeTestCase):
    """ZATCA monetary rounding regression tests.

    Verifies the compliance of emitted UBL 2.1 XML monetary business terms
    against ZATCA's arithmetic rules (BR-CO-10, BR-CO-11, BR-CO-13, BR-CO-14,
    BR-CO-15, BR-CO-16, BR-KSA-51, BR-S-08, BR-CO-17).
    """

    def setUp(self):
        super().setUp()
        self.ns = {
            'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
            'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        }

    def _build_test_xml(
        self,
        items_data: list[dict],
        discount_amount: float = 0.0,
        apply_discount_on: str = 'Net Total',
        additional_discount_percentage: float = 0.0,
        rounding_adjustment: float = 0.0,
        rounded_total: float = 0.0,
        disable_rounded_total: int = 1,
        is_return: int = 0,
        is_tax_included: int = 0,
        custom_zatca_discount_reason: str = 'Discount',
    ) -> str:
        """Constructs an in-memory Sales Invoice document and generates the ZATCA XML.

        Avoids database writes and side effects while running the exact production
        Einvoice output model and Jinja XML template.
        """
        base_inv = frappe.get_doc('Sales Invoice', 'ACC-SINV-2026-00433')
        base_siaf = frappe.get_doc('Sales Invoice Additional Fields', {'sales_invoice': 'ACC-SINV-2026-00433'})

        doc = frappe.copy_doc(base_inv)
        doc.name = base_inv.name
        doc.is_return = is_return
        doc.discount_amount = discount_amount
        doc.apply_discount_on = apply_discount_on
        doc.additional_discount_percentage = additional_discount_percentage
        doc.custom_zatca_discount_reason = custom_zatca_discount_reason
        doc.disable_rounded_total = disable_rounded_total
        doc.rounding_adjustment = rounding_adjustment
        doc.items = []

        total_amount = 0.0
        total_net = 0.0
        total_tax = 0.0

        for idx, it in enumerate(items_data, 1):
            amount = it['amount']
            net_amount = it.get('net_amount', amount)
            tax_rate = it.get('tax_rate', 15.0)
            tax_amount = it.get('tax_amount', 0.0)

            item_row = frappe._dict({
                'doctype': 'Sales Invoice Item',
                'idx': idx,
                'item_code': it.get('item_code', 'Ex-00010'),
                'item_name': it.get('item_name', f'Item {idx}'),
                'qty': it.get('qty', 1.0),
                'uom': 'Nos',
                'rate': it.get('rate', amount),
                'amount': amount,
                'net_amount': net_amount,
                'tax_rate': tax_rate,
                'tax_amount': tax_amount,
                'discount_percentage': it.get('discount_percentage', 0.0),
                'discount_amount': it.get('discount_amount', 0.0),
                'item_tax_template': it.get('item_tax_template', None),
                'custom_zatca_discount_reason': custom_zatca_discount_reason,
            })
            doc.items.append(item_row)
            total_amount += amount
            total_net += net_amount
            total_tax += tax_amount

        doc.total = total_amount
        doc.net_total = total_net
        doc.total_taxes_and_charges = total_tax
        doc.base_total_taxes_and_charges = total_tax
        doc.grand_total = total_net + total_tax
        doc.rounded_total = rounded_total or (doc.grand_total + rounding_adjustment)
        doc.taxes[0].included_in_print_rate = is_tax_included
        doc.taxes[0].tax_amount = total_tax
        doc.taxes[0].total = doc.grand_total

        siaf = frappe.copy_doc(base_siaf)
        siaf.sales_invoice = doc.name
        siaf.invoice_type_code = '381' if is_return else '388'

        orig_get_doc = frappe.get_doc
        orig_get_value = frappe.db.get_value

        def patched_get_doc(*args, **kwargs):
            if args and len(args) >= 2 and args[0] == 'Sales Invoice' and args[1] == doc.name:
                return doc
            return orig_get_doc(*args, **kwargs)

        def patched_get_value(*args, **kwargs):
            if args and len(args) >= 2 and args[0] == 'Item Tax Template Detail':
                parent = args[1].get('parent') if isinstance(args[1], dict) else None
                if parent == 'ZERO_TEMPLATE':
                    return 0.0
                if parent == 'EXEMPT_TEMPLATE':
                    return 0.0
                return 15.0
            return orig_get_value(*args, **kwargs)

        def patched_map_tax_category(tax_category_id=None, item_tax_template_id=None):
            if item_tax_template_id == 'ZERO_TEMPLATE':
                return ZatcaTaxCategory('Z', 'VATEX-SA-32', 'عقد تأمين')
            if item_tax_template_id == 'EXEMPT_TEMPLATE':
                return ZatcaTaxCategory('E', 'VATEX-SA-30', 'عقارات')
            return ZatcaTaxCategory('S')

        with patch('frappe.get_doc', side_effect=patched_get_doc), \
             patch('frappe.db.get_value', side_effect=patched_get_value), \
             patch('ksa_compliance.output_models.tax.map_tax_category', side_effect=patched_map_tax_category):
            einvoice = Einvoice(siaf, invoice_type='Simplified')
            return generate_xml_file(einvoice.result)

    def assert_zatca_invariants(self, xml_str: str, target_payable: Decimal | None = None):
        """Parses emitted XML and evaluates exact Decimal mathematical invariants."""
        root = ET.fromstring(xml_str)
        ns = self.ns

        lmt = root.find('.//cac:LegalMonetaryTotal', ns)
        self.assertIsNotNone(lmt, "cac:LegalMonetaryTotal missing")

        bt_106 = Decimal(lmt.find('cbc:LineExtensionAmount', ns).text)
        bt_109 = Decimal(lmt.find('cbc:TaxExclusiveAmount', ns).text)
        bt_112 = Decimal(lmt.find('cbc:TaxInclusiveAmount', ns).text)

        bt_107_el = lmt.find('cbc:AllowanceTotalAmount', ns)
        bt_107 = Decimal(bt_107_el.text) if bt_107_el is not None and bt_107_el.text else Decimal('0.00')

        bt_108_el = lmt.find('cbc:ChargeTotalAmount', ns)
        bt_108 = Decimal(bt_108_el.text) if bt_108_el is not None and bt_108_el.text else Decimal('0.00')

        bt_113_el = lmt.find('cbc:PrepaidAmount', ns)
        bt_113 = Decimal(bt_113_el.text) if bt_113_el is not None and bt_113_el.text else Decimal('0.00')

        bt_114_el = lmt.find('cbc:PayableRoundingAmount', ns)
        bt_114 = Decimal(bt_114_el.text) if bt_114_el is not None and bt_114_el.text else Decimal('0.00')

        bt_115 = Decimal(lmt.find('cbc:PayableAmount', ns).text)

        doc_currency = root.find('cbc:DocumentCurrencyCode', ns).text

        # Invoice-level VAT Breakdown (second cac:TaxTotal with TaxSubtotal elements)
        tax_totals = root.findall('.//cac:TaxTotal', ns)
        doc_tax_total = None
        for tt in tax_totals:
            amt_el = tt.find('cbc:TaxAmount', ns)
            if amt_el is not None and amt_el.attrib.get('currencyID') == doc_currency and tt.findall('cac:TaxSubtotal', ns):
                doc_tax_total = tt
                break
        if doc_tax_total is None and tax_totals:
            doc_tax_total = tax_totals[-1]

        self.assertIsNotNone(doc_tax_total, "cac:TaxTotal with TaxSubtotal missing")
        bt_110 = Decimal(doc_tax_total.find('cbc:TaxAmount', ns).text)

        subtotals = []
        for st in doc_tax_total.findall('cac:TaxSubtotal', ns):
            taxable = Decimal(st.find('cbc:TaxableAmount', ns).text)
            tax = Decimal(st.find('cbc:TaxAmount', ns).text)
            cat_code = st.find('cac:TaxCategory/cbc:ID', ns).text
            percent_el = st.find('cac:TaxCategory/cbc:Percent', ns)
            percent = Decimal(percent_el.text) if percent_el is not None and percent_el.text else Decimal('0.00')
            subtotals.append({
                'taxable_amount': taxable,
                'tax_amount': tax,
                'category_code': cat_code,
                'percent': percent,
            })

        lines = []
        for idx, line in enumerate(root.findall('.//cac:InvoiceLine', ns), 1):
            bt_131 = Decimal(line.find('cbc:LineExtensionAmount', ns).text)
            line_tax_el = line.find('cac:TaxTotal/cbc:TaxAmount', ns)
            ksa_11 = Decimal(line_tax_el.text) if line_tax_el is not None and line_tax_el.text else Decimal('0.00')
            line_round_el = line.find('cac:TaxTotal/cbc:RoundingAmount', ns)
            ksa_12 = Decimal(line_round_el.text) if line_round_el is not None and line_round_el.text else Decimal('0.00')
            cat_code = line.find('cac:Item/cac:ClassifiedTaxCategory/cbc:ID', ns).text
            lines.append({
                'idx': idx,
                'bt_131': bt_131,
                'ksa_11': ksa_11,
                'ksa_12': ksa_12,
                'category_code': cat_code,
            })

        doc_allowances = []
        for ac in root.findall('cac:AllowanceCharge', ns):
            amt = Decimal(ac.find('cbc:Amount', ns).text)
            cat_code = ac.find('cac:TaxCategory/cbc:ID', ns).text
            doc_allowances.append({'amount': amt, 'category_code': cat_code})

        # BR-CO-10: BT-106 == sum(BT-131)
        sum_bt_131 = sum(l['bt_131'] for l in lines)
        self.assertEqual(
            bt_106,
            sum_bt_131,
            f"BR-CO-10 violated: BT-106 ({bt_106}) != sum(BT-131) ({sum_bt_131})"
        )

        # BR-CO-11: BT-107 == sum(BT-92)
        if doc_allowances and bt_107 != Decimal('0.00'):
            sum_bt_92 = sum(a['amount'] for a in doc_allowances)
            self.assertEqual(
                bt_107,
                sum_bt_92,
                f"BR-CO-11 violated: BT-107 ({bt_107}) != sum(BT-92) ({sum_bt_92})"
            )

        # BR-CO-13: BT-109 == BT-106 - BT-107 + BT-108
        self.assertEqual(
            bt_109,
            bt_106 - bt_107 + bt_108,
            f"BR-CO-13 violated: BT-109 ({bt_109}) != BT-106 ({bt_106}) - BT-107 ({bt_107}) + BT-108 ({bt_108})"
        )

        # BR-CO-14: BT-110 == sum(BT-117)
        sum_bt_117 = sum(st['tax_amount'] for st in subtotals)
        self.assertEqual(
            bt_110,
            sum_bt_117,
            f"BR-CO-14 violated: BT-110 ({bt_110}) != sum(BT-117) ({sum_bt_117})"
        )

        # BR-CO-15: BT-112 == BT-109 + BT-110
        self.assertEqual(
            bt_112,
            bt_109 + bt_110,
            f"BR-CO-15 violated: BT-112 ({bt_112}) != BT-109 ({bt_109}) + BT-110 ({bt_110})"
        )

        # BR-CO-16: BT-115 == BT-112 - BT-113 + BT-114
        self.assertEqual(
            bt_115,
            bt_112 - bt_113 + bt_114,
            f"BR-CO-16 violated: BT-115 ({bt_115}) != BT-112 ({bt_112}) - BT-113 ({bt_113}) + BT-114 ({bt_114})"
        )

        # BR-KSA-51: KSA-12 == BT-131 + KSA-11 for each line
        for l in lines:
            self.assertEqual(
                l['ksa_12'],
                l['bt_131'] + l['ksa_11'],
                f"BR-KSA-51 violated on line {l['idx']}: KSA-12 ({l['ksa_12']}) != BT-131 ({l['bt_131']}) + KSA-11 ({l['ksa_11']})"
            )

        # BR-S-08: Standard-rated VAT category taxable amount
        std_lines_sum = sum(l['bt_131'] for l in lines if l['category_code'] == 'S')
        std_allowance_sum = sum(a['amount'] for a in doc_allowances if a['category_code'] == 'S')
        expected_std_taxable = std_lines_sum - std_allowance_sum
        std_subtotal = next((st for st in subtotals if st['category_code'] == 'S'), None)
        if std_subtotal is not None:
            self.assertEqual(
                std_subtotal['taxable_amount'],
                expected_std_taxable,
                f"BR-S-08 violated: BT-116 ({std_subtotal['taxable_amount']}) != expected ({expected_std_taxable})"
            )

        # BR-CO-17: Category tax amount within 0.01 halalah of taxable * rate / 100
        for st in subtotals:
            if st['category_code'] in ('Z', 'E', 'O'):
                self.assertEqual(st['tax_amount'], Decimal('0.00'), f"Category {st['category_code']} must have 0.00 VAT")
            elif st['category_code'] == 'S':
                expected_vat = (st['taxable_amount'] * st['percent'] / Decimal('100')).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
                self.assertLessEqual(
                    abs(st['tax_amount'] - expected_vat),
                    Decimal('0.01'),
                    f"BR-CO-17 violated: BT-117 ({st['tax_amount']}) drifts beyond 1 halalah from {expected_vat}"
                )

        # Target payable assertion (where specified)
        if target_payable is not None:
            self.assertEqual(
                bt_115,
                target_payable,
                f"Payable amount BT-115 ({bt_115}) != target ({target_payable})"
            )

    def assert_zatca_positive_values(self, xml_str: str):
        """Asserts that ZATCA monetary and quantity business terms comply with KSA positive-value rules.

        Specifically verifies BR-KSA-F-04: document amounts and quantities in Credit Notes
        must be represented as positive magnitudes, while allowing legitimate signed fields
        such as PayableRoundingAmount (BT-114).
        """
        root = ET.fromstring(xml_str)
        ns = self.ns

        lmt = root.find('.//cac:LegalMonetaryTotal', ns)
        self.assertIsNotNone(lmt, "cac:LegalMonetaryTotal missing")

        bt_106 = Decimal(lmt.find('cbc:LineExtensionAmount', ns).text)
        self.assertGreaterEqual(bt_106, Decimal('0.00'), f"BT-106 ({bt_106}) must be non-negative")

        bt_107_el = lmt.find('cbc:AllowanceTotalAmount', ns)
        if bt_107_el is not None and bt_107_el.text:
            bt_107 = Decimal(bt_107_el.text)
            self.assertGreaterEqual(bt_107, Decimal('0.00'), f"BT-107 ({bt_107}) must be non-negative")

        bt_108_el = lmt.find('cbc:ChargeTotalAmount', ns)
        if bt_108_el is not None and bt_108_el.text:
            bt_108 = Decimal(bt_108_el.text)
            self.assertGreaterEqual(bt_108, Decimal('0.00'), f"BT-108 ({bt_108}) must be non-negative")

        bt_109 = Decimal(lmt.find('cbc:TaxExclusiveAmount', ns).text)
        self.assertGreaterEqual(bt_109, Decimal('0.00'), f"BT-109 ({bt_109}) must be non-negative")

        bt_112 = Decimal(lmt.find('cbc:TaxInclusiveAmount', ns).text)
        self.assertGreaterEqual(bt_112, Decimal('0.00'), f"BT-112 ({bt_112}) must be non-negative")

        bt_113_el = lmt.find('cbc:PrepaidAmount', ns)
        if bt_113_el is not None and bt_113_el.text:
            bt_113 = Decimal(bt_113_el.text)
            self.assertGreaterEqual(bt_113, Decimal('0.00'), f"BT-113 ({bt_113}) must be non-negative")

        bt_115 = Decimal(lmt.find('cbc:PayableAmount', ns).text)
        self.assertGreaterEqual(bt_115, Decimal('0.00'), f"BT-115 ({bt_115}) must be non-negative")

        # Note: PayableRoundingAmount (BT-114) is deliberately NOT asserted positive,
        # as it can legitimately be signed.

        # Invoice-level tax totals
        for tt in root.findall('.//cac:TaxTotal', ns):
            amt_el = tt.find('cbc:TaxAmount', ns)
            if amt_el is not None and amt_el.text:
                tax_amt = Decimal(amt_el.text)
                self.assertGreaterEqual(tax_amt, Decimal('0.00'), f"TaxTotal TaxAmount ({tax_amt}) must be non-negative")
            for st in tt.findall('cac:TaxSubtotal', ns):
                taxable_el = st.find('cbc:TaxableAmount', ns)
                if taxable_el is not None and taxable_el.text:
                    taxable = Decimal(taxable_el.text)
                    self.assertGreaterEqual(taxable, Decimal('0.00'), f"BT-116 TaxableAmount ({taxable}) must be non-negative")
                st_tax_el = st.find('cbc:TaxAmount', ns)
                if st_tax_el is not None and st_tax_el.text:
                    st_tax = Decimal(st_tax_el.text)
                    self.assertGreaterEqual(st_tax, Decimal('0.00'), f"BT-117 TaxAmount ({st_tax}) must be non-negative")

        # Lines
        for idx, line in enumerate(root.findall('.//cac:InvoiceLine', ns), 1):
            qty_el = line.find('cbc:InvoicedQuantity', ns)
            if qty_el is not None and qty_el.text:
                qty = Decimal(qty_el.text)
                self.assertGreaterEqual(qty, Decimal('0.00'), f"Line {idx} InvoicedQuantity ({qty}) must be non-negative")

            bt_131 = Decimal(line.find('cbc:LineExtensionAmount', ns).text)
            self.assertGreaterEqual(bt_131, Decimal('0.00'), f"Line {idx} BT-131 ({bt_131}) must be non-negative")

            price_el = line.find('cac:Price/cbc:PriceAmount', ns)
            if price_el is not None and price_el.text:
                price = Decimal(price_el.text)
                self.assertGreaterEqual(price, Decimal('0.00'), f"Line {idx} PriceAmount ({price}) must be non-negative")

            line_tax_el = line.find('cac:TaxTotal/cbc:TaxAmount', ns)
            if line_tax_el is not None and line_tax_el.text:
                ksa_11 = Decimal(line_tax_el.text)
                self.assertGreaterEqual(ksa_11, Decimal('0.00'), f"Line {idx} KSA-11 ({ksa_11}) must be non-negative")

            line_round_el = line.find('cac:TaxTotal/cbc:RoundingAmount', ns)
            if line_round_el is not None and line_round_el.text:
                ksa_12 = Decimal(line_round_el.text)
                self.assertGreaterEqual(ksa_12, Decimal('0.00'), f"Line {idx} KSA-12 ({ksa_12}) must be non-negative")

            for ac in line.findall('cac:AllowanceCharge', ns):
                ac_amt = Decimal(ac.find('cbc:Amount', ns).text)
                self.assertGreaterEqual(ac_amt, Decimal('0.00'), f"Line {idx} AllowanceCharge Amount ({ac_amt}) must be non-negative")
                base_el = ac.find('cbc:BaseAmount', ns)
                if base_el is not None and base_el.text:
                    base_amt = Decimal(base_el.text)
                    self.assertGreaterEqual(base_amt, Decimal('0.00'), f"Line {idx} AllowanceCharge BaseAmount ({base_amt}) must be non-negative")

        # Document-level allowance/charge
        for ac in root.findall('cac:AllowanceCharge', ns):
            ac_amt = Decimal(ac.find('cbc:Amount', ns).text)
            self.assertGreaterEqual(ac_amt, Decimal('0.00'), f"Doc AllowanceCharge Amount ({ac_amt}) must be non-negative")
            base_el = ac.find('cbc:BaseAmount', ns)
            if base_el is not None and base_el.text:
                base_amt = Decimal(base_el.text)
                self.assertGreaterEqual(base_amt, Decimal('0.00'), f"Doc AllowanceCharge BaseAmount ({base_amt}) must be non-negative")

    # =========================================================================
    # Historical Production Regression Cases
    # =========================================================================

    def test_case_credit_note_00232_regression(self):
        """Historical Pattern: ACC-SINV-RET-2026-00232 (Credit Note / Return).

        10-line return invoice with document discount on Net Total and rounded payable.
        Demonstrates BR-KSA-F-04 (negative amounts in Credit Note), BR-CO-17, and BR-S-09
        when ERPNext return signs are not normalized to positive magnitudes for ZATCA XML.
        """
        items = [
            {'qty': -1.0, 'rate': 9.95, 'amount': -9.95, 'net_amount': -9.4624, 'tax_amount': -1.4194},
            {'qty': -2.0, 'rate': 10.71, 'amount': -21.42, 'net_amount': -20.3705, 'tax_amount': -3.0556},
            {'qty': -3.0, 'rate': 11.48, 'amount': -34.44, 'net_amount': -32.7524, 'tax_amount': -4.9129},
            {'qty': -1.0, 'rate': 11.48, 'amount': -11.48, 'net_amount': -10.9175, 'tax_amount': -1.6376},
            {'qty': -1.0, 'rate': 8.42, 'amount': -8.42, 'net_amount': -8.0074, 'tax_amount': -1.2011},
            {'qty': -2.0, 'rate': 9.18, 'amount': -18.36, 'net_amount': -17.4604, 'tax_amount': -2.6191},
            {'qty': -1.0, 'rate': 17.60, 'amount': -17.60, 'net_amount': -16.7376, 'tax_amount': -2.5106},
            {'qty': -2.0, 'rate': 13.01, 'amount': -26.02, 'net_amount': -24.7450, 'tax_amount': -3.7118},
            {'qty': -1.0, 'rate': 8.42, 'amount': -8.42, 'net_amount': -8.0074, 'tax_amount': -1.2011},
            {'qty': -2.0, 'rate': 11.48, 'amount': -22.96, 'net_amount': -21.8350, 'tax_amount': -3.2753},
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=-8.77443,
            apply_discount_on='Net Total',
            rounding_adjustment=-0.1601,
            rounded_total=-196.00,
            disable_rounded_total=0,
            is_return=1,
        )
        self.assert_zatca_invariants(xml, target_payable=Decimal('196.00'))
        self.assert_zatca_positive_values(xml)

    def test_case_a_acc_sinv_2026_00426_regression(self):
        """Historical Pattern Case A: ACC-SINV-2026-00426.

        High precision 5-line standard VAT invoice with document-level discount.
        Demonstrates BR-CO-10 (BT-106 != sum(BT-131)), BR-CO-15 (BT-112 != BT-109 + BT-110),
        and BR-KSA-51 failures on unchanged master.
        """
        items = [
            {'amount': 115.8180, 'net_amount': 110.0271, 'rate': 9.6515, 'qty': 12.0, 'tax_amount': 16.5041},
            {'amount': 151.4364, 'net_amount': 143.8646, 'rate': 12.6197, 'qty': 12.0, 'tax_amount': 21.5797},
            {'amount': 98.0088, 'net_amount': 93.1083, 'rate': 8.1674, 'qty': 12.0, 'tax_amount': 13.9662},
            {'amount': 133.6272, 'net_amount': 126.9459, 'rate': 11.1356, 'qty': 12.0, 'tax_amount': 19.0419},
            {'amount': 204.8640, 'net_amount': 194.6208, 'rate': 17.0720, 'qty': 12.0, 'tax_amount': 29.1931},
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=35.18772,
            apply_discount_on='Net Total',
            rounding_adjustment=0.1483,
            rounded_total=769.00,
            disable_rounded_total=0,
        )
        self.assert_zatca_invariants(xml, target_payable=Decimal('769.00'))

    def test_case_b_acc_sinv_2026_00427_regression(self):
        """Historical Pattern Case B: ACC-SINV-2026-00427.

        High precision 7-line standard VAT invoice with document-level discount.
        Demonstrates BR-CO-13 (BT-109 != BT-106 - BT-107), BR-S-08, and BR-KSA-51 failures.
        """
        items = [
            {'amount': 65.9736, 'net_amount': 62.6749, 'rate': 16.4934, 'qty': 4.0, 'tax_amount': 9.4012},
            {'amount': 68.9920, 'net_amount': 65.5424, 'rate': 17.2480, 'qty': 4.0, 'tax_amount': 9.8314},
            {'amount': 72.0104, 'net_amount': 68.4099, 'rate': 18.0026, 'qty': 4.0, 'tax_amount': 10.2615},
            {'amount': 72.0104, 'net_amount': 68.4099, 'rate': 18.0026, 'qty': 4.0, 'tax_amount': 10.2615},
            {'amount': 72.0104, 'net_amount': 68.4099, 'rate': 18.0026, 'qty': 4.0, 'tax_amount': 10.2615},
            {'amount': 206.9760, 'net_amount': 196.6272, 'rate': 17.2480, 'qty': 12.0, 'tax_amount': 29.4941},
            {'amount': 117.0120, 'net_amount': 111.1614, 'rate': 9.7510, 'qty': 12.0, 'tax_amount': 16.6742},
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=33.74924,
            apply_discount_on='Net Total',
            rounding_adjustment=-0.4209,
            rounded_total=737.00,
            disable_rounded_total=0,
        )
        self.assert_zatca_invariants(xml, target_payable=Decimal('737.00'))

    def test_case_c_acc_sinv_2026_00433_regression(self):
        """Historical Pattern Case C: ACC-SINV-2026-00433.

        Single-line standard VAT invoice, no discount.
        Demonstrates that independent rounding fails even on 1 line:
        BT-131 = 206.98, BT-110 = 31.05 -> BT-112 is 238.02 instead of 238.03 (BR-CO-15),
        and KSA-12 is 238.02 instead of 238.03 (BR-KSA-51).
        """
        items = [
            {'amount': 206.9760, 'net_amount': 206.9760, 'rate': 17.2480, 'qty': 12.0, 'tax_amount': 31.0464}
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=0.0,
            rounding_adjustment=-0.0224,
            rounded_total=238.00,
            disable_rounded_total=0,
        )
        self.assert_zatca_invariants(xml, target_payable=Decimal('238.00'))

    def test_case_d_acc_sinv_2026_00436_regression(self):
        """Historical Pattern Case D: ACC-SINV-2026-00436.

        Document discount where BT-109 was rounded to 682.58 while BT-106 (718.50) - BT-107 (35.93) = 682.57.
        BT-112 is already 784.96 (682.57 + 102.39 = 784.96).
        Proves that BT-109 must be derived canonically from BT-106 - BT-107.
        """
        items = [
            {'amount': 718.5040, 'net_amount': 682.5788, 'rate': 718.5040, 'qty': 1.0, 'tax_amount': 102.3868}
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=35.9252,
            apply_discount_on='Net Total',
            rounding_adjustment=0.0,
            disable_rounded_total=1,
        )
        self.assert_zatca_invariants(xml)

    # =========================================================================
    # Control Cases & Non-Regression Scenarios
    # =========================================================================

    def test_control_single_line_clean(self):
        """Clean single-line invoice with 2-decimal values."""
        items = [{'amount': 100.00, 'net_amount': 100.00, 'rate': 100.00, 'qty': 1.0, 'tax_amount': 15.00}]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)

    def test_control_multi_line_clean(self):
        """Clean multi-line invoice without rounding drift."""
        items = [
            {'amount': 50.00, 'net_amount': 50.00, 'rate': 50.00, 'qty': 1.0, 'tax_amount': 7.50},
            {'amount': 150.00, 'net_amount': 150.00, 'rate': 150.00, 'qty': 1.0, 'tax_amount': 22.50},
        ]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)

    def test_control_item_level_discount(self):
        """Item-level percentage discount."""
        items = [
            {
                'amount': 90.00,
                'net_amount': 90.00,
                'rate': 100.00,
                'qty': 1.0,
                'tax_amount': 13.50,
                'discount_percentage': 10.0,
                'discount_amount': 10.0,
            }
        ]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)

    def test_control_document_discount_on_net_total(self):
        """Document-level discount on Net Total."""
        items = [
            {'amount': 200.00, 'net_amount': 180.00, 'rate': 200.00, 'qty': 1.0, 'tax_amount': 27.00}
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=20.00,
            apply_discount_on='Net Total',
        )
        self.assert_zatca_invariants(xml)

    def test_control_item_and_document_discount(self):
        """Combined item-level and document-level discounts."""
        items = [
            {
                'amount': 180.00,
                'net_amount': 162.00,
                'rate': 200.00,
                'qty': 1.0,
                'tax_amount': 24.30,
                'discount_percentage': 10.0,
                'discount_amount': 20.0,
            }
        ]
        xml = self._build_test_xml(
            items_data=items,
            discount_amount=18.00,
            apply_discount_on='Net Total',
        )
        self.assert_zatca_invariants(xml)

    def test_control_rounded_total_disabled(self):
        """Invoice with disable_rounded_total = 1 (no payable rounding)."""
        items = [
            {'amount': 100.00, 'net_amount': 100.00, 'rate': 100.00, 'qty': 1.0, 'tax_amount': 15.00}
        ]
        xml = self._build_test_xml(items_data=items, disable_rounded_total=1)
        self.assert_zatca_invariants(xml)

    def test_control_positive_payable_rounding(self):
        """Invoice with positive rounding adjustment."""
        items = [
            {'amount': 100.00, 'net_amount': 100.00, 'rate': 100.00, 'qty': 1.0, 'tax_amount': 15.00}
        ]
        xml = self._build_test_xml(
            items_data=items,
            rounding_adjustment=0.05,
            rounded_total=115.05,
            disable_rounded_total=0,
        )
        self.assert_zatca_invariants(xml, target_payable=Decimal('115.05'))

    def test_control_negative_payable_rounding(self):
        """Invoice with negative rounding adjustment."""
        items = [
            {'amount': 100.00, 'net_amount': 100.00, 'rate': 100.00, 'qty': 1.0, 'tax_amount': 15.00}
        ]
        xml = self._build_test_xml(
            items_data=items,
            rounding_adjustment=-0.05,
            rounded_total=114.95,
            disable_rounded_total=0,
        )
        self.assert_zatca_invariants(xml, target_payable=Decimal('114.95'))

    def test_control_tax_inclusive_pricing(self):
        """Tax-inclusive pricing (included_in_print_rate = 1)."""
        items = [
            {'amount': 115.00, 'net_amount': 100.00, 'rate': 115.00, 'qty': 1.0, 'tax_amount': 15.00}
        ]
        xml = self._build_test_xml(items_data=items, is_tax_included=1)
        self.assert_zatca_invariants(xml)

    def test_control_multiple_quantities(self):
        """Invoice with multiple quantities per line."""
        items = [
            {'amount': 300.00, 'net_amount': 300.00, 'rate': 100.00, 'qty': 3.0, 'tax_amount': 45.00}
        ]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)

    def test_control_repeated_items(self):
        """Repeated item on multiple invoice lines."""
        items = [
            {'item_code': 'Ex-00010', 'amount': 100.00, 'net_amount': 100.00, 'tax_amount': 15.00},
            {'item_code': 'Ex-00010', 'amount': 100.00, 'net_amount': 100.00, 'tax_amount': 15.00},
        ]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)

    def test_control_return_credit_note(self):
        """Return / Credit note invoice with negative amounts."""
        items = [
            {'amount': -100.00, 'net_amount': -100.00, 'rate': 100.00, 'qty': -1.0, 'tax_amount': -15.00}
        ]
        xml = self._build_test_xml(items_data=items, is_return=1)
        self.assert_zatca_invariants(xml, target_payable=Decimal('115.00'))
        self.assert_zatca_positive_values(xml)

    def test_control_mixed_standard_and_zero_rated(self):
        """Mixed invoice with Standard rate (15%) and Zero-rated (0%) categories."""
        items = [
            {'amount': 100.00, 'net_amount': 100.0, 'rate': 100.0, 'qty': 1.0, 'tax_rate': 15.0, 'tax_amount': 15.00, 'item_tax_template': None},
            {'amount': 100.00, 'net_amount': 100.0, 'rate': 100.0, 'qty': 1.0, 'tax_rate': 0.0, 'tax_amount': 0.00, 'item_tax_template': 'ZERO_TEMPLATE'},
        ]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)

    def test_control_mixed_standard_and_exempt(self):
        """Mixed invoice with Standard rate (15%) and Exempt (0%) categories."""
        items = [
            {'amount': 100.00, 'net_amount': 100.0, 'rate': 100.0, 'qty': 1.0, 'tax_rate': 15.0, 'tax_amount': 15.00, 'item_tax_template': None},
            {'amount': 100.00, 'net_amount': 100.0, 'rate': 100.0, 'qty': 1.0, 'tax_rate': 0.0, 'tax_amount': 0.00, 'item_tax_template': 'EXEMPT_TEMPLATE'},
        ]
        xml = self._build_test_xml(items_data=items)
        self.assert_zatca_invariants(xml)
