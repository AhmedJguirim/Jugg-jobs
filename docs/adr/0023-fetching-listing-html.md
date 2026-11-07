# 0023 - Fetching a listing's HTML from its link (experimental)

Status: Accepted - 2026-10-28

## Context

Every raw listing so far was made by opening the link in a browser and copying the
posting's element by hand. The parsers were written against that copy. The goal is to
stop doing this by hand, starting with an experiment that cannot disturb the waiting
list or grading.

A look at a Lever posting showed the page needs no API call and no session: one plain
`GET` answers `200` with server-rendered HTML, and none of the browser's cookies
(`cf_clearance`, `__cf_bm`) were needed. But it is the **whole page**, 720-740 KB, while
a hand copy is the one `<div class="content">` (5-15 KB). The parsers read that block
and nothing else.

## Decision

- **Table `job_listings_test`**, the experimental twin of `raw_job_listings`: `url`
  (unique), `html_body`, `status`, `http_status`, `page_bytes`, `note`, `fetched_at`
  plus timestamps. No link to `jobs` and no place on the waiting list. Fetching a link
  again replaces its row.
- **The fetcher stores what a hand copy would have been**, not the page.
  `app/integrations/listing_pages/extract.py` names, per platform, the element to keep
  (Lever: the `div.content` that contains `posting-headline`; the page has other
  `content` divs) and the address to request (Lever: `/apply` is cut off, because that
  is the application form, not the posting). A test shows the fetched block parses to
  the same `JobCreate` as the hand copy.
- **Every platform that has a parser is fetched** (Lever, Ashby, Greenhouse, Workable,
  Breezy). A link on any other site is recorded as `unsupported` without a request.
  Each platform is one entry in `listing_pages/extract.py`: how to make the request(s),
  how to get the block from the answer, and what a removed posting looks like.
- **Ashby is rebuilt, not cut out.** Ashby is a single-page app: the response is a ~40 KB
  shell and the posting exists only as JSON in `window.__appData` (`posting`:
  title, department, location, address, workplace and employment type, compensation,
  `descriptionHtml`; `organization`: name, website, logo). `listing_pages/ashby.py` writes
  the elements the Ashby parser reads (header with logo link, `h1`, the left pane's
  labelled facts, the description) from it, using Ashby's own class names and labels
  (`FullTime` -> "Full time", `OnSite` -> "On-site"). Values are escaped. The root has
  `data-synthetic="appData"`, so a stored block can be told from a hand copy.
  A removed posting answers **404** with `posting: null` and a shell that has no "Job not
  found" text (the script writes it), so a 404 stores a block with that heading and the
  parser returns `ListingNotFound`. If Ashby renames a JSON key the block comes out
  without it, or `no_content` if the posting is missing; nothing is guessed.
- **Greenhouse is cut out like Lever.** The page is server-rendered; a hand copy is
  the plain `div` that is the parent of `div.job__description` (it holds the header,
  tags, description and the "career at X?" box). `boards.greenhouse.io` links answer
  301 to `job-boards.greenhouse.io`; the client follows redirects and the link is stored
  as collected. A removed job is not a 404: it **redirects (302) to the board with
  `?error=true` and answers 200** with no job on it. The client therefore reports the
  final address, a redirect to `error=true` is `not_found`, and the stored block is a
  sentence ("This job is no longer open") the parser knows, because the url the parser
  is later given is the collected one, which has no `error=true` in it.
- **Breezy is cut out.** The page is server-rendered; the parser reads only
  `<div class="description">` (title and company come from the link), and a hand copy is
  exactly that element, so that is what is stored (5-17 KB of a 20-45 KB page). A removed
  position comes in **two forms**, both HTTP 200: a redirect to the company's page (the
  final address is no longer under `/p/`), or the same link showing a "Position Closed"
  heading with no description. Either is `not_found` and stores a sentence the parser
  knows. (The parser's own phrase list has "no longer accepting applications" but the
  closed page says "no longer accepting candidates", so the stored sentence matters.) The
  "closed" test requires the missing description block, so a live job whose text happens
  to contain those words is still a job. The fetched page also has an `h1`, the location,
  the job type and JSON-LD, which the parser does not read, so titles and companies are
  still the link's slug and subdomain (ADR-0022); reading them is a parser change.
- **Workable is rebuilt from its JSON, in up to three requests.** The page is an 8 KB
  loading shell. The page's script calls `/api/v2/accounts/<company>/jobs/<id>`
  (title, locations, department, type, workplace, code, description / requirements /
  benefits HTML; a removed job is a 404) and the job record carries no company name or
  logo, so `/api/v1/accounts/<company>` is asked too (once per company per process). A
  link without a company (`/j/<id>`) redirects to the one with it, so that redirect is
  followed first. The block uses the `data-ui` attributes the Workable parser reads,
  the page's labels ("Full time", "Remote") and headings ("Description",
  "Requirements", "Benefits"), each section's body in a `div` as on the page. If the
  company request fails the job is still stored, with no company. Root:
  `data-synthetic="api"`.
- **Outcomes** (`status`): `fetched`; `not_found` (404/410 - the body is kept, because
  the parser reads the platform's "job gone" page as a result); `blocked` (403, 429,
  5xx, other); `no_content` (a 200 without the block); `unsupported`; `error` (timeout,
  DNS). `blocked`, `error` and `no_content` are retried with `--retry`; the rest are
  settled.
- **Rate limit:** one request at a time with a random 1-3 s pause before every request
  after the first (`listing_fetch.paced` wraps the HTTP call, so it is per request, not
  per link: Workable's second request is paced too). Links that make no request cause
  no pause. Each outcome is committed when it is known, so stopping a run loses nothing.
- **Which links:** live `manual_job_listing_urls` with no hand-pasted raw listing and no
  `job_listings_test` row, oldest first. `--include-pasted` also takes links that were
  pasted by hand, to compare the two; `--platform NAME` keeps one platform's links.
- **Run by a script** (`scripts/fetch_listings.py`), not a DBOS workflow: it uses no GPU,
  has no queue to share and each step is cheap to repeat. Moving it to the `job-ingest`
  queue is a small step if it is later triggered from the page.
- Headers are those of a browser following a link from Google, without cookies. No
  evasion of any kind is attempted; if a host answers with a challenge or 403 the link
  is recorded as `blocked` and left.

## Consequences

- The first run over 11 collected Lever links fetched all 11, and the Lever parser read
  all 11. Over 15 Ashby links, 8 postings were fetched and parsed and 7 were 404s read as
  gone. Against the two hand-copied Ashby fixtures, Docplanner came out identical in
  every field; Lendable differed only in description wording that the company has since
  changed. Over 15 Greenhouse links, 10 postings were fetched and parsed and 5 were
  redirects read as gone; over 15 Workable links, 11 and 4. Against the hand copies still
  live: Grafana (Greenhouse) and Pearl (Workable) are identical in all 45 fields; Affirm
  differs only in its title and a "New" tag, which the posting has since changed; 2K
  has since been taken down and is correctly read as gone. Over 12 Breezy links, 10 were
  fetched and 2 were gone; 4 of the 9 hand-copied fixtures are reproduced **byte for
  byte**, 3 differ because the employers have rewritten the posting since (different title
  and wording), and 2 have since been closed.
- One Greenhouse job came out with no company: its page has no "career at X?" box, which
  is the only place the Greenhouse parser reads the company from. A hand copy would
  lack it as well; the fix belongs to the parser.
- A site that starts challenging requests gives `blocked` rows, not wrong data. The fix
  then is slower pacing or giving that site up, not a workaround.
- A platform whose response has neither the posting block nor embedded data lands in
  `no_content` and needs a different approach.
- The Ashby block is a reconstruction. Whatever the page shows that is not in the JSON
  (nothing found so far) is missing from it.
- Nothing in this ADR feeds `raw_job_listings`; ADR-0024 does that, behind a button.
