---
title: Classification
version: 1
notice: >-
  STARTER TEXT. Align the document-type descriptions in your config, and this prompt, with your
  organization's own document taxonomy and records policy.
---
Decide which one of these document types the attached file "{{ document_name }}" is:

{{ doc_types }}

How to decide:
- Judge by the content and layout of the document, not by its file name.
- If the file holds several documents (for example a cover letter followed by an invoice), classify it
  by the main document: the one the file was clearly sent for. Ignore cover pages, fax sheets,
  transmittal letters, signature or audit-trail pages, and blank pages.
- A draft, a copy or a scan of a document type is still that document type.
- If the file matches none of the types, or two types fit equally well, answer "unknown". Do not force a
  match: "unknown" sends the file to a person, which is the right outcome when in doubt.
- Set "confidence" to low when the file is hard to read (a poor scan, cut-off pages) or only partly matches.
- In "reason", name the features that decided it, such as a title, a header or the fields present.
