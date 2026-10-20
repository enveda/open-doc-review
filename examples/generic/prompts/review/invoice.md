---
owner: example
version: 1
---
Review the invoice "{{ document_name }}" against its purchase order.

Fields already extracted from the invoice:
{{ fields }}

Reference documents:
{{ references }}

Check:
1. Every invoiced line item appears on the purchase order.
2. Quantities do not exceed the ordered quantities.
3. Unit prices match the agreed prices.
4. Arithmetic: line totals, subtotal and total are correct.
5. Supplier name and currency match the purchase order.

Severity: blocker = would cause an incorrect payment; major = a discrepancy someone must resolve
before payment; minor = a small inconsistency; info = worth knowing, no action needed.
Set "reference" to "Purchase order" when a finding compares against it. Give the page when the
document has pages. If a reference is NOT FOUND, review what you can and report the gap once.
