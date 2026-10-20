---
title: Invoice three-way match review
version: 1
owner: <the accounts-payable team accountable for this review>
approved_by: <who approved this wording, per your change-control procedure>
notice: >-
  STARTER TEXT. The checks, severity definitions and exclusions below must reflect your organization's own
  policies, procedures and payment controls. Replace every <placeholder>, and remove checks that do not
  apply to you.
---
Review the {{ doc_type }} "{{ document_name }}" ({{ doc_type_description }}) - {{ window }}.

Fields already extracted from it:
{{ fields }}

Reference documents attached (each titled with its name below):
{{ references }}

## What to check
1. Every invoiced line item appears on the Purchase order.
2. The invoiced quantity of each item does not exceed the quantity ACCEPTED on the Goods receipt
   (not the quantity ordered: only goods received and accepted may be paid).
3. Each unit price equals the unit price on the Purchase order.
4. No charge (fee, surcharge, freight) is invoiced unless the Purchase order lists it.
5. Arithmetic: each line total is quantity × unit price, and the stated total is the sum of the lines.
6. Supplier name, purchase order number and currency agree across all three documents.

## What NOT to flag
- Differences in item wording that clearly describe the same item (for example "Bolt 8x40" and
  "Bolt, 8 mm x 40 mm").
- Payment terms, bank details and tax, which are checked elsewhere.
- A goods receipt that records fewer items than ordered: a short delivery is not an invoice error,
  unless the invoice bills the missing quantity.

## Severity
- blocker: paying the invoice as it stands would pay for goods not received or not accepted, or at a
  price or amount not agreed.
- major: a discrepancy someone must resolve before payment, such as an unlisted charge.
- minor: an inconsistency that does not change the amount payable, such as a misspelled supplier name.
- info: worth knowing; no action needed.

## How to report
- One finding per issue. Quote the invoice value and the value it was compared with, and name the line.
- Set "reference" to "Purchase order" or "Goods receipt" when a finding compares against it.
- If a reference is NOT FOUND, review what you can and report the gap once.
