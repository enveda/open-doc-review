# SharePoint Online adapter (EXPERIMENTAL)

> **Experimental.** This adapter follows the Microsoft Graph v1.0 documentation and is tested
> against a fake Graph only. It has **not been run against a live tenant**. Try it on a test site
> with `review.dry_run: true` first, and report what breaks.

Connects open-doc-review to one document library of one SharePoint Online site, through Microsoft
Graph with app-only credentials. It reads files and writes **library columns** beside them. It has no
delete, move, rename or upload call, so it never changes a document's content or location.

```bash
pip install 'open-doc-review[sharepoint]'   # adds httpx
```

## 1. Register the app

In the Microsoft Entra admin center → App registrations → **New registration**:

1. Name it (e.g. `doc-review`), single tenant, no redirect URI.
2. **Certificates & secrets** → New client secret. Copy the value; it is shown once.
3. **API permissions** → Add → Microsoft Graph → **Application permissions** → `Sites.Selected` →
   then **Grant admin consent**.

Do not grant `Sites.ReadWrite.All`: `Sites.Selected` gives the app no access at all until an admin
grants it one site (step 2).

## 2. Grant the app one site

An admin with `Sites.FullControl.All` (or a SharePoint admin, with PnP PowerShell) gives the app
`write` on the one site it serves. `write` lets it read files and edit list-item columns.

PnP PowerShell:

```powershell
Connect-PnPOnline -Url https://contoso.sharepoint.com/sites/Quality -Interactive
Grant-PnPAzureADAppSitePermission -AppId <client-id> -DisplayName doc-review -Permissions Write `
    -Site https://contoso.sharepoint.com/sites/Quality
```

Or Graph (as the admin):

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
Content-Type: application/json

{"roles": ["write"],
 "grantedToIdentities": [{"application": {"id": "<client-id>", "displayName": "doc-review"}}]}
```

Find the site id with `GET /sites/contoso.sharepoint.com:/sites/Quality?$select=id`.

## 3. Create the library columns

SharePoint columns are flat, so the adapter stores each namespace as columns of the library
(`P` = `column_prefix`, default `DocReview`):

| Column (internal name) | Type | Holds |
|---|---|---|
| `PClassification` | Multiple lines of text, **plain text** | classification as JSON: `doc_type`, `classified_at`, `run_id`, `fields_version`, `processed_version`, `failed_version`, `failure_count`, `last_error` |
| `PDocType` | Single line of text, **indexed** | a copy of `doc_type`, so `find` can filter on it |
| `PReview` | Multiple lines of text, plain text | review as JSON: `outcome`, `finding_counts`, `reviewed_version`, `reviewed_at`, `run_id`, `references` |
| `PReviewRequested` | Yes/No | the review's `requested`. A person ticks it to ask for a re-review; the review write clears it. |
| `PComments` | Multiple lines of text, plain text | the comments (see §5) |
| `P_<field>` | per field (below), **indexed** if used in a reference `match:` | one column per extracted field, e.g. `DocReview_po_number` |

Field types: config `string` → Single line of text, `number` / `integer` → Number,
`date` → Date and Time (date only), `boolean` → Yes/No.

Use plain text (not rich text) for the multi-line columns, with **Append changes to existing text**
off. Rich text wraps the JSON in HTML.

PnP PowerShell (library `Documents`, prefix `DocReview`, two example fields):

```powershell
$list = "Documents"
$note = '<Field Type="Note" Name="{0}" StaticName="{0}" DisplayName="{0}" RichText="FALSE" NumLines="6" AppendOnly="FALSE" />'
foreach ($n in "DocReviewClassification", "DocReviewReview", "DocReviewComments") {
    Add-PnPFieldFromXml -List $list -FieldXml ($note -f $n)
}
Add-PnPField -List $list -InternalName DocReviewDocType -DisplayName DocReviewDocType -Type Text
Add-PnPField -List $list -InternalName DocReviewReviewRequested -DisplayName "Review requested" -Type Boolean
Add-PnPField -List $list -InternalName DocReview_po_number -DisplayName "PO number" -Type Text
Add-PnPField -List $list -InternalName DocReview_invoice_date -DisplayName "Invoice date" -Type DateTime
foreach ($n in "DocReviewDocType", "DocReview_po_number") {
    Set-PnPField -List $list -Identity $n -Values @{Indexed = $true}
}
```

Set the internal name when you create a column: renaming one later changes only its display name.

## 4. Configure

```yaml
ecm:
  adapter: sharepoint
  options:
    tenant_id: ${SHAREPOINT_TENANT_ID}
    client_id: ${SHAREPOINT_CLIENT_ID}
    client_secret: ${SHAREPOINT_CLIENT_SECRET}
    hostname: contoso.sharepoint.com     # or site_id: "contoso.sharepoint.com,<guid>,<guid>"
    site_path: sites/Quality
    library: Documents                   # or drive_id: "b!..."
    root_path: /Reviewed                 # nothing outside this folder is read, found or written
    inbox_path: /Reviewed/Inbox          # the folder poll scans; default = root_path
```

When a credential option is left out, the adapter reads `SHAREPOINT_TENANT_ID`, `SHAREPOINT_CLIENT_ID`
and `SHAREPOINT_CLIENT_SECRET` from the environment. Never put the secret in the YAML.

| Option | Default | Meaning |
|---|---|---|
| `site_id` or `hostname` + `site_path` | (required) | The site. |
| `drive_id` or `library` | (required) | The document library (a library name is looked up once). |
| `root_path` | `/` | Folder path inside the library that scopes everything. |
| `inbox_path` | `root_path` | Folder `poll` lists. It must be inside `root_path`. |
| `recursive` | `false` | Also scan the inbox's subfolders. |
| `column_prefix` | `DocReview` | Prefix of every column the adapter uses (letters, digits, `_`). |
| `comments_max_chars` | `30000` | Size cap of the comments column; the oldest comments drop off first. |

Run `docreview check`. It gets a token, reads the site, the library and its columns, confirms that
`root_path` and `inbox_path` are folders, and confirms that the five namespace columns exist with
the right types.

## 5. How the port maps to Graph

| Port method | Graph |
|---|---|
| `poll` | `GET /drives/{d}/root:{inbox}:/children?$expand=listItem($expand=fields)` (paged). A file is offered as `new_document` while `processed_version` ≠ its **cTag**, and as `review_requested` while `PReviewRequested` is ticked. Stateless: there's no cursor. |
| `get` | `GET /drives/{d}/items/{id}`. The version is the file's **cTag**, which changes only when the content changes (the eTag would also change on our own column writes). |
| `download` | `GET /drives/{d}/items/{id}/content`. If `ref.version` is set and the current cTag differs, it raises. Office files are fetched with `?format=pdf`. |
| `read_metadata` / `write_metadata` | `GET` / `PATCH /drives/{d}/items/{id}/listItem/fields`. A classification or review write merges into the JSON that is there (a `None` removes a key). A fields write sets one column per field (a `None` clears it; a `YYYY-MM-DD` date goes to a date column as `YYYY-MM-DDT00:00:00Z` and reads back as `YYYY-MM-DD`). |
| `find` | `GET /sites/{s}/lists/{l}/items?$filter=fields/PDocType eq '…' and fields/P_<field> eq '…'` with `Prefer: HonorNonIndexedQueriesWarningMayFailRandomly`. Values are OData literals with `'` doubled; number columns accept numbers only. The adapter then keeps only files under `root_path` (and under `scope`, a parent folder id), re-checks every value case-insensitively with whitespace trimmed, and sorts newest first. |
| `post_comment` / `list_comments` | **Graph has no API for file comments.** Comments are kept in the `PComments` column, newest first, each with a `[timestamp]` line, separated by a line of `─`. People can read them in the library view or the file's details pane. |

## 6. Known limits

- **Experimental and unverified against a live tenant.** These points come from the documentation
  and are the most likely to need fixes: `$expand=listItem($expand=fields)` on a children listing;
  `$expand=driveItem` together with `fields` on list items; clearing a column by PATCHing `null`;
  and how `eq` compares text (case-insensitively, as assumed).
- Index the columns `find` filters on (`PDocType` and every field used in a reference `match:`).
  Without an index, SharePoint refuses filtered queries on a list over 5,000 items even with the
  `Prefer` header. That header only lets small lists work unindexed.
- There are no real comments: findings go to a column, and comments that no longer fit under
  `comments_max_chars` are dropped, oldest first. A finding whose comment was dropped can be
  posted again.
- A write reads the namespace and then PATCHes it, without an `If-Match`. Two writers editing the
  same JSON column at the same moment can lose one edit; a person editing the JSON by hand should
  keep it valid.
- `poll` lists the whole inbox on every run, with one request per 200 files. A delta query
  (`/drives/{d}/root/delta`) with its token as the cursor would scale better and is a possible
  later improvement.
- `download` checks the cTag and then downloads, so a change in between is not caught.
- A document reviewed before its reference arrives is re-reviewed automatically when the reference is
  processed, for rules whose `match` values are bare `{field}` placeholders. For other rules, tick
  `PReviewRequested` once the reference is there.
- Office files (`.docx`, `.xlsx`, `.pptx`, …) are downloaded as Graph's PDF rendition (`/content?format=pdf`). Files Graph can't render fail that download and are retried on later polls.
