"""Seeded invoice PDFs for task 2 (plan §2, §12). Owner: C.

Test data, not code: the PDFs are committed, this script only documents how
they were made. Same supplier and total in both; only the bank account differs.
`invoice_ok.pdf` uses an account the supplier published to the VAT register
(checked 2026-10-08), `invoice_bad_account.pdf` a made-up one.

  uv run --with fpdf2 python scripts/make_invoices.py [--font path/to/unicode.ttf]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fpdf import FPDF

OUT = Path(__file__).resolve().parent.parent / "testdata"
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"

SUPPLIER = ["Alza.cz a.s.", "Jankovcova 1522/53", "170 00 Praha 7 - Holešovice", "IČO: 27082440", "DIČ: CZ27082440"]
BUYER = ["Jana Nováková", "Korunní 2569/108", "101 00 Praha 10"]
ITEMS = [("Notebook Lenovo ThinkPad E14 Gen 5", 1, 18990.00), ("Dokovací stanice USB-C", 1, 2490.00), ("Myš Logitech MX Master 3S", 2, 1990.00)]
VAT_RATE = 21
ACCOUNTS = {"invoice_ok.pdf": "2171532/0800", "invoice_bad_account.pdf": "4471029385/0800"}


def czk(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " Kč"


def invoice(account: str, font: str) -> FPDF:
    pdf = FPDF(format="A4")
    pdf.add_font("body", fname=font)
    pdf.add_page()
    pdf.set_font("body", size=18)
    pdf.cell(0, 12, "Faktura - daňový doklad č. 2026104417", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("body", size=10)
    y = pdf.get_y() + 4
    for x, title, lines in ((10, "Dodavatel", SUPPLIER), (110, "Odběratel", BUYER)):
        pdf.set_xy(x, y)
        pdf.multi_cell(90, 5, "\n".join([title.upper(), *lines]))
    pdf.set_y(y + 34)
    for k, v in (("Datum vystavení", "06.10.2026"), ("Datum zdanitelného plnění", "06.10.2026"), ("Datum splatnosti", "20.10.2026"),
                 ("Forma úhrady", "bankovní převod"), ("Číslo účtu", account), ("Variabilní symbol", "2026104417")):
        pdf.cell(60, 6, k)
        pdf.cell(0, 6, v, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    widths = (90, 20, 40, 40)
    for w, h in zip(widths, ("Položka", "Množství", "Cena za kus bez DPH", "Celkem bez DPH")):
        pdf.cell(w, 7, h, border="B")
    pdf.ln()
    base = 0.0
    for name, qty, price in ITEMS:
        base += qty * price
        for w, v in zip(widths, (name, str(qty), czk(price), czk(qty * price))):
            pdf.cell(w, 7, v)
        pdf.ln()
    vat = round(base * VAT_RATE / 100, 2)
    pdf.ln(4)
    for k, v in ((f"Základ daně {VAT_RATE} %", czk(base)), (f"DPH {VAT_RATE} %", czk(vat)), ("Celkem k úhradě", czk(base + vat))):
        pdf.cell(150, 7, k, align="R")
        pdf.cell(40, 7, v, align="R", new_x="LMARGIN", new_y="NEXT")
    return pdf


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--font", default=FONT, help="a TTF with Czech glyphs")
    a = p.parse_args()
    for name, account in ACCOUNTS.items():
        invoice(account, a.font).output(str(OUT / name))
        print(OUT / name)


if __name__ == "__main__":
    main()
