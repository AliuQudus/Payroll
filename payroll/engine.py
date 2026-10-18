"""Payroll calculation engine.

Pure Python (no Django imports) so it can be unit tested on its own.
Every salary element is a formula; statutory rules come from data (Rules),
never from code.
"""
from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

D = Decimal
ZERO = D("0")
TWO_PLACES = D("0.01")

EARNING, DEDUCTION, EMPLOYER = "EARNING", "DEDUCTION", "EMPLOYER"


def money(value) -> Decimal:
    return D(value).quantize(TWO_PLACES, ROUND_HALF_UP)


class FormulaError(Exception):
    pass


# --------------------------------------------------------------------------
# Safe formula evaluation (whitelisted syntax only, no eval())
# --------------------------------------------------------------------------
_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_COMPARES = {
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}
_FUNCTIONS = {
    "MIN": min,
    "MAX": max,
    "ABS": abs,
    "ROUND": lambda x, n=0: round(x, int(n)),
    "IF": lambda cond, a, b: a if cond else b,
}


def parse_formula(formula: str) -> ast.AST:
    try:
        return ast.parse(formula.strip().upper(), mode="eval").body
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula: {formula!r}") from exc


def referenced_names(node: ast.AST) -> set[str]:
    func_names = {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - func_names


def evaluate(node: ast.AST, variables: dict[str, Decimal]):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return D(str(node.value))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaError(f"Unknown name: {node.id}")
        return variables[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = evaluate(node.left, variables), evaluate(node.right, variables)
        if isinstance(node.op, ast.Div) and right == 0:
            raise FormulaError("Division by zero")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = evaluate(node.operand, variables)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Compare) and len(node.ops) == 1 \
            and type(node.ops[0]) in _COMPARES:
        return _COMPARES[type(node.ops[0])](
            evaluate(node.left, variables),
            evaluate(node.comparators[0], variables),
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCTIONS and not node.keywords:
        return _FUNCTIONS[node.func.id](*[evaluate(a, variables) for a in node.args])
    raise FormulaError("Unsupported expression in formula")


def calculate(formula: str, variables: dict[str, Decimal]) -> Decimal:
    result = evaluate(parse_formula(formula), variables)
    if isinstance(result, bool) or not isinstance(result, Decimal):
        raise FormulaError(f"Formula must produce a number: {formula!r}")
    return result


# --------------------------------------------------------------------------
# Elements and ordering
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Element:
    code: str
    name: str
    kind: str                      # EARNING / DEDUCTION / EMPLOYER
    formula: str
    taxable: bool = False          # earning counts toward taxable income
    pensionable: bool = False      # earning counts toward pension base
    nhf_base: bool = False         # earning counts toward NHF base
    cap: Optional[Decimal] = None  # maximum monthly amount
    leave_base: bool = False       # earning counts toward the leave allowance base
    one_off: bool = False          # taxable lump sum: taxed once rather than as 12 equal months


def order_elements(elements: list[Element], base_names=("GROSS",)) -> list[Element]:
    """Sort elements so each is calculated after the ones it references."""
    by_code = {e.code.upper(): e for e in elements}
    deps: dict[str, set[str]] = {}
    for code, element in by_code.items():
        names = referenced_names(parse_formula(element.formula))
        unknown = names - set(by_code) - set(base_names)
        if unknown:
            raise FormulaError(
                f"{code}: unknown reference(s) {', '.join(sorted(unknown))}"
            )
        deps[code] = names & set(by_code)

    ordered: list[Element] = []
    state: dict[str, int] = {}

    def visit(code: str, trail: list[str]):
        if state.get(code) == 2:
            return
        if state.get(code) == 1:
            raise FormulaError("Circular reference: " + " -> ".join(trail + [code]))
        state[code] = 1
        for dep in sorted(deps[code]):
            visit(dep, trail + [code])
        state[code] = 2
        ordered.append(by_code[code])

    for code in sorted(by_code):
        visit(code, [])
    return ordered


# --------------------------------------------------------------------------
# Statutory rules (loaded from the database, effective-dated)
# --------------------------------------------------------------------------
@dataclass
class Rules:
    # (lower, upper or None, rate) on ANNUAL taxable income
    bands: list[tuple[Decimal, Optional[Decimal], Decimal]]
    pension_employee_rate: Decimal = D("0.08")
    pension_employer_rate: Decimal = D("0.10")
    nhf_rate: Decimal = D("0")
    nsitf_rate: Decimal = D("0")
    itf_rate: Decimal = D("0")
    rent_relief_rate: Decimal = D("0.20")
    rent_relief_cap: Decimal = D("500000")
    absence_divisor: Decimal = D("30")   # days used to value one day of absence
    leave_allowance_rate: Decimal = D("0")   # share of annual pay paid as leave allowance


def annual_tax(taxable: Decimal, bands) -> Decimal:
    tax = ZERO
    for lower, upper, rate in bands:
        if taxable <= lower:
            break
        top = taxable if upper is None else min(taxable, upper)
        tax += (top - lower) * rate
    return tax


# --------------------------------------------------------------------------
# Payslip calculation
# --------------------------------------------------------------------------
@dataclass
class Line:
    code: str
    name: str
    kind: str
    amount: Decimal


@dataclass
class PayslipResult:
    gross: Decimal
    lines: list[Line] = field(default_factory=list)
    total_earnings: Decimal = ZERO
    total_deductions: Decimal = ZERO
    paye: Decimal = ZERO
    net_pay: Decimal = ZERO
    employer_cost: Decimal = ZERO
    rent_relief: Decimal = ZERO   # annual relief that reduced taxable income


def compute_payslip(
    gross: Decimal,
    elements: list[Element],
    rules: Rules,
    overrides: Optional[dict[str, str]] = None,
    annual_rent: Decimal = ZERO,
    pension_applies: bool = True,
    nhf_applies: bool = True,
) -> PayslipResult:
    gross = money(gross)
    overrides = {k.upper(): v for k, v in (overrides or {}).items()}
    effective = [
        replace(e, formula=overrides.get(e.code.upper(), e.formula)) for e in elements
    ]

    variables: dict[str, Decimal] = {"GROSS": gross}
    result = PayslipResult(gross=gross)
    flags: dict[str, Element] = {}

    for element in order_elements(effective):
        amount = calculate(element.formula, variables)
        if element.cap is not None:
            amount = min(amount, element.cap)
        amount = money(amount)
        variables[element.code.upper()] = amount
        flags[element.code.upper()] = element
        result.lines.append(Line(element.code, element.name, element.kind, amount))

    def total(kind, flag=None):
        return sum(
            (l.amount for l in result.lines
             if l.kind == kind and (flag is None or getattr(flags[l.code.upper()], flag))),
            ZERO,
        )

    earnings = total(EARNING)
    taxable_earnings = total(EARNING, "taxable")
    pension_base = total(EARNING, "pensionable")
    nhf_base = total(EARNING, "nhf_base")

    pension_ee = money(pension_base * rules.pension_employee_rate) if pension_applies else ZERO
    pension_er = money(pension_base * rules.pension_employer_rate) if pension_applies else ZERO
    nhf = money(nhf_base * rules.nhf_rate) if nhf_applies else ZERO
    nsitf = money(earnings * rules.nsitf_rate)
    itf = money(earnings * rules.itf_rate)

    rent_relief = min(annual_rent * rules.rent_relief_rate, rules.rent_relief_cap)
    result.rent_relief = money(rent_relief)
    one_off = sum((l.amount for l in result.lines if l.kind == EARNING
                   and flags[l.code.upper()].taxable and flags[l.code.upper()].one_off), ZERO)
    annual_taxable = max(
        (taxable_earnings - one_off - pension_ee - nhf) * 12 - rent_relief, ZERO
    )
    base_tax = annual_tax(annual_taxable, rules.bands)
    # regular pay is taxed as 12 equal months; a one-off lump sum adds its own extra tax once
    paye = money(base_tax / 12 + (annual_tax(annual_taxable + one_off, rules.bands) - base_tax))

    # Take element totals before statutory lines are appended (avoids double counting)
    element_deductions = total(DEDUCTION)
    element_employer = total(EMPLOYER)

    def stat(code, name, kind, amount):
        if amount:
            result.lines.append(Line(code, name, kind, amount))

    stat("PENSION_EE", "Pension (employee)", DEDUCTION, pension_ee)
    stat("NHF", "National Housing Fund", DEDUCTION, nhf)
    stat("PAYE", "PAYE tax", DEDUCTION, paye)
    stat("PENSION_ER", "Pension (employer)", EMPLOYER, pension_er)
    stat("NSITF", "NSITF", EMPLOYER, nsitf)
    stat("ITF", "ITF", EMPLOYER, itf)

    result.total_earnings = earnings
    result.paye = paye
    result.total_deductions = element_deductions + pension_ee + nhf + paye
    result.net_pay = earnings - result.total_deductions
    result.employer_cost = earnings + element_employer + pension_er + nsitf + itf
    return result
