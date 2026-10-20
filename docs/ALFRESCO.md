# Alfresco adapter (example)

> **Example adapter, not tested against a live system.** It follows the Alfresco Content Services
> REST API v1 documentation and is tested against a fake server only. Try it on a test folder with
> `review.dry_run: true` first. Every assumption it makes is listed under [Known limits](#6-known-limits).

The adapter connects open-doc-review to one folder tree of an Alfresco repository. It reads files.
Beside them it writes **the properties of one aspect** and **native comments**. It never changes a
document's content or location.

```bash
pip install 'open-doc-review[alfresco]'   # adds httpx
```

## 1. Deploy the content model

The metadata lives in one custom aspect, `dr:documentReview` (prefix `dr`). Each entry in your
`doc_types.*.fields` config needs a property `dr:f_<field>` in it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<model name="dr:docReviewModel" xmlns="http://www.alfresco.org/model/dictionary/1.0">
  <description>open-doc-review metadata</description>
  <version>1.0</version>
  <imports>
    <import uri="http://www.alfresco.org/model/dictionary/1.0" prefix="d"/>
  </imports>
  <namespaces>
    <namespace uri="http://example.com/model/docreview/1.0" prefix="dr"/>
  </namespaces>
  <aspects>
    <aspect name="dr:documentReview">
      <title>Document review</title>
      <properties>
        <!-- namespaces stored as JSON; not searched -->
        <property name="dr:classification"><type>d:text</type><index enabled="false"/></property>
        <property name="dr:review"><type>d:text</type><index enabled="false"/></property>
        <!-- find filters on these -->
        <property name="dr:docType"><type>d:text</type>
          <index enabled="true"><tokenised>both</tokenised></index></property>
        <property name="dr:reviewRequested"><type>d:boolean</type><default>false</default></property>
        <!-- one per extracted field: string → d:text, number → d:double, integer → d:long,
             date → d:date, boolean → d:boolean -->
        <property name="dr:f_po_number"><type>d:text</type>
          <index enabled="true"><tokenised>both</tokenised></index></property>
        <property name="dr:f_total"><type>d:double</type></property>
        <property name="dr:f_invoice_date"><type>d:date</type></property>
      </properties>
    </aspect>
  </aspects>
</model>
```

To deploy it, do one of the following:
- save it as `alfresco/extension/docReviewModel.xml`, with a bootstrap context;
- upload it to *Repository → Data Dictionary → Models* and activate it there;
- recreate it in Share's *Model Manager*.

Do not apply the aspect yourself. The adapter adds it on its first write.

## 2. Service user and versioning

- **Service user.** Create a user for the adapter and give it **Collaborator** on the root folder.
  Collaborator is the narrowest built-in role that can edit properties and add comments. The role
  could also edit content, but the adapter has no call that does.
- **Versioning.** The adapter uses `cm:versionLabel` as the document version, so files should have
  the `cm:versionable` aspect. Uploads through Share and the REST API get it by default. A file
  without it is still processed, with a weaker version (see Known limits). Keep
  `cm:autoVersionOnUpdateProps` **off**, which is the default: if it were on, the adapter's own
  property writes would create versions and every document would look new on every poll. The
  adapter refuses such a node: `get` and `download` raise, and `poll` skips the node and logs it, so
  the rest of the inbox is still processed.

## 3. Configure

```yaml
ecm:
  adapter: alfresco
  options:
    base_url: https://acs.example.com
    username: ${ALFRESCO_USERNAME}
    password: ${ALFRESCO_PASSWORD}       # or ticket: ${ALFRESCO_TICKET}
    root_node_id: 8f2c...                # nothing outside this folder is read, found or written
    inbox_node_id: 1a7d...               # the folder poll lists; default = root_node_id
```

| Option | Default | Meaning |
|---|---|---|
| `base_url` | (required) | Scheme and host of the repository. The adapter adds `/alfresco/api/...`. |
| `root_node_id` | (required) | Folder node that scopes everything. |
| `inbox_node_id` | `root_node_id` | Folder whose direct children `poll` lists. It must be inside the root. |
| `username` + `password`, or `ticket` | env `ALFRESCO_*` | Basic auth. A ticket (`POST /tickets`) is sent as `Basic base64(ticket)`. |
| `prefix` | `dr` | Namespace prefix of the properties. |
| `aspect` | `<prefix>:documentReview` | The aspect that holds them. |

Run `docreview check`. It checks four things and prints one line per problem:
- it signs in (`GET /people/-me-`);
- the root and inbox nodes are folders;
- the inbox is inside the root;
- the aspect has its four properties, with the right types (Model API, `GET /aspects/{id}`).

## 4. How the port maps to the API

All paths are under `/alfresco/api/-default-/public/alfresco/versions/1`.

| Port method | API |
|---|---|
| `poll` | `GET /nodes/{inbox}/children?where=(isFile=true)&include=properties,path,aspectNames`, paged 100 at a time. A file is offered as `new_document` while `processed_version` ≠ its version (`cm:versionLabel`, or the unversioned stand-in), and as `review_requested` while `dr:reviewRequested` is true. There is no cursor. |
| `get` | `GET /nodes/{id}?include=properties,path,aspectNames`. The file must be inside the root: the root's id must appear in `path.elements`. Metadata reads and writes and comment calls make the same check first. |
| `download` | `GET /nodes/{id}/content`, after checking that the version label is unchanged. For Office files it uses the PDF rendition: `GET /nodes/{id}/renditions/pdf` reports its status. When it is `CREATED`, the adapter downloads `.../renditions/pdf/content`. Otherwise it requests the rendition with `POST /nodes/{id}/renditions` `{"id": "pdf"}` (a 409 means it was already requested) and raises "rendition pending", so a later poll picks the file up. |
| `read_metadata` / `write_metadata` | `GET /nodes/{id}`, then `PUT /nodes/{id}` with `{aspectNames, properties}`, which never touches content. Classification and review are JSON in a `d:text` property: a write merges into it, and `None` removes a key. Fields are one property each, and `None` clears one. A `YYYY-MM-DD` date is written as `YYYY-MM-DDT00:00:00.000+0000` and read back as `YYYY-MM-DD`. |
| `find` | `POST /search/versions/1/search` with an AFTS query, sorted by `cm:modified` descending. The query is `ANCESTOR:"workspace://SpacesStore/{root}"` (or `PARENT:` the scope folder), the aspect, `dr:docType`, and each `dr:f_<field>` as a quoted phrase with `\` and `"` escaped. Field names must be identifiers. The adapter then checks every result again in Python: it must be a file inside the root, in the scope folder, with every value equal after trimming surrounding whitespace and folding case. |
| `post_comment` / `list_comments` | `POST` / `GET /nodes/{id}/comments` (paged). |

## 5. Re-review and late references

Tick `dr:reviewRequested` in the document's properties to ask for a re-review. A review result
clears it.

A document reviewed before its reference arrived is re-reviewed automatically once the reference
is processed, for rules whose `match` values are bare `{field}` placeholders.

## 6. Known limits

Assumptions taken from the documentation and not yet checked against a server:

- **Aspect list on PUT.** `PUT /nodes/{id}` replaces the whole aspect list when `aspectNames` is
  sent. The adapter therefore sends the node's current aspects plus its own. If an aspect is
  added between the read and the PUT, it would be removed.
- **Clearing a property.** A property set to `null` in `PUT .../properties` is removed.
- **No conditional update.** There is no `If-Match` on node updates. Two writers can lose one edit.
- **Renditions.** `POST /nodes/{id}/renditions` with `{"id": "pdf"}` creates the rendition
  asynchronously, and the `pdf` rendition definition exists, which Transform Service provides.
- **Search.** The search assumptions are these:
  - Search Services (Solr) or Search Enterprise is installed;
  - AFTS accepts `prefix:property:"phrase"`, `ANCESTOR:` and `PARENT:`;
  - the search API returns `path` and `properties` when they are requested in `include`.
- **Search is eventually consistent.** A document whose fields were written seconds ago may not
  be found yet. A late-reference re-review can then miss it until that reference's next version,
  or until someone ticks `dr:reviewRequested` by hand.
- **Unversioned nodes.** A node without `cm:versionable` has no version label, so its version is
  `unversioned:<content size in bytes>`. It is offered until it has been processed, and offered again
  whenever its size changes.
  - An edit that keeps the size exactly the same is not picked up.
  - `modifiedAt` is deliberately **not** used: the adapter's own property writes change it, so every
    document would look new on every poll.
  - Apply `cm:versionable` to the library for exact versions.
- **Download race.** `download` checks the version label and then downloads. A new version
  uploaded in between is not caught; `GET /nodes/{id}/versions/{label}/content` would pin the
  version and is a possible improvement.
- **Comments.** They are stored as given. Share renders comment content as HTML, so line breaks in
  findings may collapse there.
- **The Model API** (`GET /aspects/{id}`) needs ACS 7.0 or later. On older servers, `check` and
  field writes fail.
- **Polling** lists the whole inbox on every run, one request per 100 files. A query on
  `cm:modified` would scale better.
