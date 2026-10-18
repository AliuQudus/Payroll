import unittest
from decimal import Decimal as D

from payroll.engine import (
    DEDUCTION, EARNING, Element, FormulaError, Rules,
    annual_tax, calculate, compute_payslip, order_elements,
)

# Sample bands for testing only. Confirm against the client's NTA 2025 source.
BANDS = [
    (D("0"), D("800000"), D("0")),
    (D("800000"), D("3000000"), D("0.15")),
    (D("3000000"), D("12000000"), D("0.18")),
    (D("12000000"), D("25000000"), D("0.21")),
    (D("25000000"), D("50000000"), D("0.23")),
    (D("50000000"), None, D("0.25")),
]

STANDARD = [
    Element("BASIC", "Basic", EARNING, "GROSS * 0.4", True, True, True),
    Element("HOUSING", "Housing", EARNING, "GROSS * 0.3", True, True),
    Element("TRANSPORT", "Transport", EARNING, "GROSS * 0.3", True, True),
]


class FormulaTests(unittest.TestCase):
    def test_functions_and_references(self):
        v = {"GROSS": D("500000"), "BASIC": D("200000")}
        self.assertEqual(calculate("MIN(BASIC * 0.2, 50000)", v), D("40000"))
        self.assertEqual(calculate("IF(GROSS > 400000, 1000, 500)", v), D("1000"))

    def test_rejects_unsafe_code(self):
        for bad in ["__import__('os')", "GROSS.real", "[1,2]", "open('x')"]:
            with self.assertRaises(FormulaError):
                calculate(bad, {"GROSS": D("1")})

    def test_circular_reference(self):
        els = [Element("A", "A", EARNING, "B * 2"), Element("B", "B", EARNING, "A * 2")]
        with self.assertRaises(FormulaError):
            order_elements(els)

    def test_unknown_reference(self):
        with self.assertRaises(FormulaError):
            order_elements([Element("A", "A", EARNING, "NOPE * 2")])

    def test_dependency_order(self):
        els = [Element("B", "B", EARNING, "A * 2"), Element("A", "A", EARNING, "GROSS")]
        self.assertEqual([e.code for e in order_elements(els)], ["A", "B"])


class TaxTests(unittest.TestCase):
    def test_marginal_bands(self):
        self.assertEqual(annual_tax(D("3000000"), BANDS), D("330000"))
        self.assertEqual(annual_tax(D("5000000"), BANDS), D("690000"))
        self.assertEqual(annual_tax(D("500000"), BANDS), D("0"))


class PayslipTests(unittest.TestCase):
    rules = Rules(bands=BANDS)

    def test_standard_payslip(self):
        r = compute_payslip(D("500000"), STANDARD, self.rules)
        self.assertEqual(r.total_earnings, D("500000.00"))
        self.assertEqual(r.paye, D("65300.00"))
        self.assertEqual(r.net_pay, D("394700.00"))

    def test_new_element_needs_no_code_change(self):
        extra = STANDARD + [
            Element("LOAN", "Loan repayment", DEDUCTION, "20000"),
            Element("MEAL", "Meal allowance", EARNING, "BASIC * 0.1", True, True),
        ]
        r = compute_payslip(D("500000"), extra, self.rules)
        meal = next(l for l in r.lines if l.code == "MEAL")
        self.assertEqual(meal.amount, D("20000.00"))
        self.assertTrue(any(l.code == "LOAN" for l in r.lines))

    def test_employee_override(self):
        r = compute_payslip(
            D("500000"), STANDARD, self.rules, overrides={"housing": "100000"}
        )
        housing = next(l for l in r.lines if l.code == "HOUSING")
        self.assertEqual(housing.amount, D("100000.00"))

    def test_rent_relief_is_capped(self):
        no_rent = compute_payslip(D("500000"), STANDARD, self.rules)
        capped = compute_payslip(D("500000"), STANDARD, self.rules, annual_rent=D("4000000"))
        # relief capped at 500,000 annual, taxed at 18% marginal => 7,500/month saved
        self.assertEqual(no_rent.paye - capped.paye, D("7500.00"))

    def test_one_off_lump_sum_is_taxed_once_not_twelve_times(self):
        lump = STANDARD + [Element("LUMP", "Lump sum", EARNING, "600000", taxable=True, one_off=True)]
        r = compute_payslip(D("500000"), lump, self.rules)
        # regular PAYE 65,300 plus 600,000 x 18% (the band it falls in) = 108,000
        self.assertEqual(r.paye, D("173300.00"))
        annualised = STANDARD + [Element("LUMP", "Lump sum", EARNING, "600000", taxable=True)]
        self.assertGreater(compute_payslip(D("500000"), annualised, self.rules).paye, r.paye)

    def test_element_cap(self):
        els = [Element("BONUS", "Bonus", EARNING, "GROSS", True, cap=D("100000"))]
        r = compute_payslip(D("500000"), els, self.rules)
        self.assertEqual(r.lines[0].amount, D("100000.00"))


if __name__ == "__main__":
    unittest.main()
