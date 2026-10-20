---
title: Goods receipt review
version: 1
owner: <the receiving / warehouse team accountable for this review>
approved_by: <who approved this wording, per your change-control procedure>
notice: >-
  STARTER TEXT. The checks, severity definitions and exclusions below must reflect your organization's own
  policies and receiving procedures. Replace every <placeholder>, and remove checks that do not apply to you.
---
Review the {{ doc_type }} "{{ document_name }}" ({{ doc_type_description }}) - {{ window }}.

Fields already extracted from it:
{{ fields }}

Reference documents attached (each titled with its name below):
{{ references }}

## What to check
1. Every received item appears on the Purchase order.
2. The quantity received of each item does not exceed the quantity ordered.
3. The receipt date is on or after the Purchase order's order date.
4. For each item, accepted quantity = received quantity - rejected or damaged quantity.
5. The purchase order number and supplier match the Purchase order.

## What NOT to flag
- Receiving fewer items than ordered (a short or partial delivery), when the receipt records it.
- Storage locations, carrier names and delivery-note numbers.

## Severity
- blocker: the receipt would let goods be paid for or used that were not ordered or not accepted.
- major: a quantity or date that someone must correct before the receipt is used for a match.
- minor: an inconsistency that does not change any quantity, such as a misspelled supplier name.
- info: worth knowing; no action needed.

## How to report
- One finding per issue. Quote the receipt value and the Purchase order value it was compared with.
- Set "reference" to "Purchase order" when a finding compares against it.
- If a reference is NOT FOUND, review what you can and report the gap once.
