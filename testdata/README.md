# testdata/ (owner: C)

Seeded **test data, not code** (plan §12). Nothing here is shown to the agent as a hint.

- `invoice_ok.pdf`: supplier Alza.cz a.s. (IČO 27082440, DIČ CZ27082440), bank account `2171532/0800`, one of the accounts it
  published to the VAT register (checked 2026-10-08), total `30 806,60 Kč` incl. 21 % VAT
- `invoice_bad_account.pdf`: the same invoice with a made-up account `4471029385/0800`, which isn't published
- `tasks.md`: the three demo prompts exactly as the operator types them

The PDFs have a text layer (any PDF text extractor reads them). Regenerate with
`uv run --with fpdf2 python scripts/make_invoices.py`.
