# Demo tasks, as the operator types them

For humans: copy these into `frank run`. Nothing in this folder is shown to Frankenstein as a hint.

1. Session A:
   ```bash
   uv run frank run --session A "Is the supplier with IČO 27082440 a reliable VAT payer, and what's their registered address?"
   ```
2. Session B, a **fresh process**:
   ```bash
   uv run frank run --session B --attach testdata/invoice_ok.pdf "Here's an invoice PDF. Check the supplier, verify the bank account on the invoice is a published one, and tell me the total in EUR at today's ČNB rate."
   ```
   Repeat with `testdata/invoice_bad_account.pdf`: the account check must fail.
3. Session B or C:
   ```bash
   uv run frank run --session C "Which of my tools reach which domains, when were they last tested, and are any of them broken?"
   ```

`--attach` is stream B's flag (pending); the kernel side is `Host.attach`.
