# Reference Checker

A tool for journal editors and authors to verify the references in academic
PDFs against authoritative databases (Crossref, Semantic Scholar, OpenAlex,
WG21, IETF, OpenLibrary) and the references' own cited URLs.

Current accuracy on the 125-PDF / 4,207-reference development corpus:
**90.1% verified, 307 `not_found`** — see [Benchmarks](#benchmarks).

## What's new in this version

1. **Crossref-first pipeline.** OpenAlex moved to a paid credit model
   (~100 requests/day free, ~1,000 with a key), which makes it unusable as a
   primary backend at corpus scale. Backends are now ordered by *daily
   capacity*, not by data quality: Crossref leads, Semantic Scholar backstops,
   OpenAlex is opportunistic and disables itself when its budget runs out.
2. **Verification against the reference's own URL.** GROBID records each
   reference's link in a `<ptr target="...">` attribute, which the extractor
   previously ignored. It's now harvested and used as a verification phase.
   This is the only viable path for grey literature — software projects,
   datasets, standards, industry reports, news articles — that no academic
   database indexes.
3. **Fewer false positives.** A contained-title guard stops a short reference
   title from matching a longer, different work that merely contains it
   ("CSTA Standards for CS Teachers" no longer matches "Using CSTA Standards
   for CS Teachers to Design CS Teacher Pathways"). Such candidates are routed
   to `flawed_reference` for review instead of being asserted as verified.
4. **Correctness fixes to the network layer.** Failed API calls are no longer
   cached as empty results, Semantic Scholar's throttle is thread-safe, and
   both Crossref and S2 retry on HTTP 429 instead of silently dropping a
   lookup.

---

## Prerequisites

- **Docker Desktop** ([download](https://www.docker.com/products/docker-desktop/)).

## Quick start

```bash
# macOS / Linux
./scripts/start.sh

# Windows
scripts\start.bat
```

On the first run the script:

1. Copies `env.example` to `.env` and opens it for editing. Set
   `OPENALEX_EMAIL` to any valid email address — this is used for both
   Crossref's and OpenAlex's polite pools — then re-run.
2. Builds the app image and pulls GROBID (~2 GB, one-time download).
3. Waits for GROBID's ML models to load (~30–60 s) and for the app's health
   check.

When you see the success banner, open <http://localhost:5000>.

To stop everything: `docker compose down`.

> **Note:** the app reads `.env`, not `env.example`. Editing the template has
> no effect on a running stack.

## Web UI

Visit <http://localhost:5000>. You can:

- Paste raw reference text and click **Verify** for a quick check.
- Upload a single PDF — references are auto-extracted by GROBID.
- Upload a batch of PDFs (typically ≤ 20 at a time through the browser).

For corpora larger than ~20 PDFs, use **batch mode** instead.

## Batch mode (CLI) — for hundreds of PDFs

The web upload route holds everything in memory and returns one big JSON; not
viable for a journal-sized corpus. The CLI processes PDFs one by one, persists
to SQLite as it goes, and is fully resumable.

The CLI runs *inside* the running app container so it shares GROBID and the
API cache with the web UI:

```bash
# 1. Start the stack (only needed once per session)
./scripts/start.sh

# 2. Place your PDFs somewhere under ./data so the container can see them.
#    The compose file mounts ./data → /data inside the container.
mkdir -p data/pdfs
cp /path/to/your/pdfs/*.pdf data/pdfs/

# 3. Run the batch
docker compose exec app python cli.py batch \
    --input  /data/pdfs \
    --output /data/results/run1.db \
    --resume \
    --continue-on-error
```

Re-running with `--resume` skips files already marked `done` in the results
DB. Killing the run with Ctrl-C is safe — the next `--resume` picks up
exactly where you stopped.

> **Windows users:** run these from PowerShell, not Git Bash. Git Bash's MSYS
> layer rewrites container paths like `/data/pdfs` into Windows paths and the
> run fails immediately with `not a directory`. (`MSYS_NO_PATHCONV=1` also
> works if you prefer Git Bash.)

### Inspecting a finished run

```bash
# Summary across all PDFs and references
docker compose exec app python cli.py stats --output /data/results/run1.db

# Export every reference as CSV
docker compose exec app python cli.py export \
    --output /data/results/run1.db \
    --csv    /data/results/run1.csv

# Export just the references that need editor review — usually the most
# useful slice
docker compose exec app python cli.py export \
    --output /data/results/run1.db \
    --csv    /data/results/run1-needs-review.csv \
    --not-found-only
```

The CSV columns are: `filename, bucket, original_text, matched_title,
matched_year, matched_url, matched_source, note`.

### Single-PDF check via CLI

```bash
docker compose exec app python cli.py check --pdf /data/pdfs/some_paper.pdf
```

## Configuration (`.env`)

All settings are environment variables. The supported ones:

| Variable               | Default                  | Purpose |
|------------------------|--------------------------|---------|
| `OPENALEX_EMAIL`       | _(empty)_                | Your email. Used by **both** Crossref and OpenAlex polite pools, and in the shared User-Agent. Set this. |
| `OPENALEX_API_KEY`     | _(empty)_                | OpenAlex API key. Without it OpenAlex allows ~100 requests/day and self-disables early in a batch. |
| `S2_API_KEY`           | _(empty)_                | Optional Semantic Scholar key. Lifts you out of the shared anonymous pool, but per-key rate limiting still applies. |
| `USE_OLLAMA_FALLBACK`  | `false`                  | Enable the LLM parse fallback. See the note under Troubleshooting before turning this on. |
| `OLLAMA_HOST`          | `http://localhost:11434` | Where the LLM lives (uncomment Ollama in `docker-compose.yml` first). |
| `OLLAMA_MODEL`         | `llama3`                 | Ollama model to use. |
| `MAX_REF_WORKERS`      | `6`                      | Parallel reference checks per PDF. |
| `BATCH_PDF_WORKERS`    | `2`                      | Parallel PDFs in the batch driver. |
| `VERIFY_SCORE`         | `90`                     | Title-similarity threshold for VERIFIED. |
| `FLAWED_SCORE`         | `78`                     | Threshold for FLAWED_REFERENCE (editor review). |
| `MIN_CONSIDER_SCORE`   | `60`                     | Below this, candidate is rejected. |
| `CACHE_DB`             | `cache/api_cache.db`     | API cache path. **Under Docker set this to `/data/cache/api_cache.db`** — a relative path resolves inside the image, outside the mounted volume, and the warm cache is silently ignored. |
| `CACHE_TTL_DAYS`       | `30`                     | How long to keep cached API responses. |

Edit `.env` and run `docker compose up -d --build` to pick up changes.

> The app container runs a **copy** of `src/` baked in at image build time.
> Editing source files on disk changes nothing until you rebuild.

## How verification works

For each reference the verifier walks through phases in order, returning as
soon as one succeeds:

| Phase | What it tries                                        | Source |
|-------|------------------------------------------------------|--------|
| 0     | WG21 / IETF RFC / ISBN sniper                        | wg21.link, datatracker.ietf.org, OpenLibrary |
| 1     | DOI / arXiv exact lookup                             | Crossref → S2 → OpenAlex |
| 2     | GROBID `processCitation` re-parse (local, unmetered) | GROBID |
| 3     | **Bibliographic search — the primary path**          | Crossref |
| 4     | Bibliographic search backstop                        | Semantic Scholar |
| 5     | Structured title/author/year search (opportunistic)  | OpenAlex |
| 5.25  | Raw-reference general search (opportunistic)         | OpenAlex |
| 5.5   | Optional Ollama LLM parse (if `USE_OLLAMA_FALLBACK=true`) | Ollama |
| 5.8   | **Fetch the URL the reference itself cites**         | the cited page |
| 5.9   | Web search + page metadata                           | DuckDuckGo |
| 6     | Returns `NOT_FOUND` or `NOT_REFERENCE`               | — |

### Why this order

Backends are ranked by **daily capacity**, not by data quality:

| Backend | Practical ceiling | Role |
|---|---|---|
| Crossref | no hard cap (polite pool) | **primary** |
| Semantic Scholar | ~1 request/second | backstop |
| OpenAlex | ~100/day free, ~1,000/day with a key | opportunistic |
| Cited URL / web | no cap | grey-literature backstop |

A thousand-PDF day is roughly 34,000 references. OpenAlex covers a fraction of
a percent of that, and Semantic Scholar's 1 req/s ceiling means it can only be
asked about references Crossref already failed on. Crossref's
`query.bibliographic` endpoint is also purpose-built for raw reference
strings, which is what we have.

OpenAlex phases are safe to leave enabled: once the daily budget is spent the
client latches off and those phases cost nothing, so small runs still benefit
while large runs self-disable after the first 429.

Every external HTTP call is wrapped in a SQLite cache
(`data/cache/api_cache.db`, 30-day TTL). Re-runs are much faster, and you can
tune the thresholds in `.env` without re-paying for API calls. Failed calls
are deliberately *not* cached, so a transient outage doesn't turn into a
30-day "no results".

### Result buckets

| Bucket               | Meaning |
|----------------------|---------|
| `verified`           | Confidently matched in a database, or against the reference's own cited URL. |
| `edition_mismatch`   | Strong title match but the year is well outside preprint lag — likely a different edition. |
| `flawed_reference`   | A near-match exists but isn't confident enough to assert. Editor should look. |
| `not_found`          | No match. May be wrong, very recent, or grey literature. |
| `not_reference`      | Heuristically not a reference (e.g. body text caught by GROBID). |

The `flawed_reference` and `not_found` buckets are the output an editor
actually reads. **A rising `flawed_reference` count is not a regression** — it
generally means candidates that used to be over-confidently marked `verified`
are now being surfaced for review.

`edition_mismatch` is deliberately small. A large year gap on a similar title
is more often evidence of a *wrong match* than of a genuine edition
difference, so only strong matches reach this bucket; the rest are routed to
`flawed_reference`.

## Accuracy notes

**Backend ordering.** See [Why this order](#why-this-order) above. This was
worth ~100 references on the development corpus on its own, and it makes the
tool's accuracy independent of any single provider's rate limits.

**Cited-URL verification.** GROBID stores each reference's link as an XML
attribute (`<ptr target="...">`), which a text-only extraction misses
entirely. Harvesting it recovers a URL for roughly 30% of references. Two
things follow: arXiv and `doi.org` links are promoted to exact ID lookups in
Phase 1, and everything else becomes a page fetch in Phase 5.8, where the
page's declared `citation_doi` (if any) is resolved against Crossref — turning
a URL into a hard ID-based verification. For a software project or an
organisational report, the cited page is a *more* authoritative source than
any academic database.

**Title matching.** `title_similarity()` blends `token_sort_ratio` (word-level,
robust to punctuation) with `partial_ratio` (substring-aware, robust to
subtitles), plus subtitle stripping and a content-token coverage check. Two
guards matter:

- Short titles can't match longer ones on a prefix ("Algorithms" →
  "Introduction to Algorithms" is rejected).
- When one title is fully contained in a much longer one, the score is capped
  below the verify threshold, because that usually indicates a *different*
  work that cites or discusses the reference. These land in
  `flawed_reference`.

The guard applies only when both sides are titles. Several phases score a
database title against the whole raw reference string, which legitimately
contains far more tokens (authors, venue, year, pages) — the guard is
explicitly disabled there.

**Reference parsing.** GROBID's full-document parser handles clean references.
For messy ones its `processCitation` endpoint re-parses the single citation
string. Note that the two parsers disagree, and neither dominates: the
full-document parse has document context and is preferred, while
`processCitation` is used as a fallback and as an additional Crossref query.

**DOI and arXiv extraction.** A line-break repair pass recovers DOIs split
across lines (`10.1145/3290605.\n3300823`). arXiv identifiers are normalised
to their bare form — GROBID emits values like `arXiv:2408.05534[cs.SE]` and
`arXiv:1810.04805v2`, and anything past the number makes the lookup malformed.

**Network reliability.** Under a parallel batch (up to
`MAX_REF_WORKERS × BATCH_PDF_WORKERS` threads) both Crossref and Semantic
Scholar return HTTP 429 for a meaningful share of requests. Both clients now
retry with backoff, and the S2 throttle is mutex-protected so concurrent
threads actually space their requests instead of bursting together. Before
this, run-to-run variance was large enough to be mistaken for a code
regression.

## Benchmarks

Development corpus: 125 PDFs, 4,207 references.

| Run | Change | verified | not_found | flawed | edition |
|-----|--------|---------:|----------:|-------:|--------:|
| baseline | Levenshtein matching, OpenAlex-first | 3620 | 574 | 0 | 0 |
| — | RapidFuzz matching, Crossref backstop | 3644 | 544 | 5 | 2 |
| — | Crossref-first reorder | 3690 | 442 | 48 | 15 |
| **current** | **+ cited URLs, retries, contained-title guard** | **3791** | **307** | 94 | 5 |

Verified rate 86.6% → **90.1%**. The editor review queue
(`not_found + flawed_reference + edition_mismatch`) fell from 551 to **406**.

Verified matches by source in the current run:

```
exact DOI / arXiv / standards   1774
Crossref                        1733
Semantic Scholar                 180
cited URL                         94
ISBN                               7
web search                         3
```

## Troubleshooting

**"Cannot reach GROBID"**
GROBID needs ~30–60 s after `docker compose up` for its Java + ML models to
load. Check `docker compose logs grobid`.

**GROBID crash-loops with `CgroupV2Subsystem.getInstance` NullPointerException**
A JDK/cgroup-v2 incompatibility on recent Docker releases, not a memory
problem. `docker-compose.yml` sets `JAVA_OPTS=-Xmx4g -XX:-UseContainerSupport`
to work around it. Symptom without the flag: the container restarts forever
and zero references are extracted.

**`docker ps` shows GROBID as `unhealthy`**
Cosmetic — it's the healthcheck's own curl failing inside the GROBID
container. If the app's `/api/health` reports `grobid_reachable: true`,
everything works.

**`/api/health` reports `cache_stats: {"total": 0}` after a big run**
`CACHE_DB` is relative, so it resolved inside the image rather than in the
mounted volume. Set `CACHE_DB=/data/cache/api_cache.db` in `.env`.

**Lots of `not_found` results that look right**
Check in this order:

1. Is `OPENALEX_EMAIL` set? Without it you're outside Crossref's polite pool.
2. Are the references grey literature — software, datasets, standards, org
   reports, blog posts? These aren't in any academic database. The cited-URL
   phase handles them when the reference carries a link; when it doesn't,
   there is currently no path.
3. **Enabling Ollama will probably not help.** It's a *parsing* aid, and by
   this point the failures are usually references whose works simply aren't
   indexed anywhere — better parsing doesn't put a wordlist or a curriculum
   document into Crossref. It also costs a ~4 GB model plus a daemon.

**Web-search phase returns nothing**
DuckDuckGo's HTML endpoint bot-blocks automated traffic, returning HTTP 202
and an anomaly page after a modest number of requests. The client detects
this and doesn't cache the empty result, but the phase yields little in a
large batch. Phase 5.8 (cited URL) doesn't depend on a search engine and is
unaffected.

**OpenAlex returns HTTP 401 vs HTTP 429**
`401` means a credential was seen and rejected — check the key in `.env`.
`429` means no key was used and you've hit the free tier's daily budget.

**Disk filling up during a large batch**
The persistent volume is `./data`. The cache DB is the biggest contributor and
is safe to delete (it rebuilds on the next run): `rm data/cache/api_cache.db`.
The results DB at `data/results/run1.db` is your actual output — back that up
first.

**Cleaning the cache without losing results**
```bash
docker compose exec app python -c "from cache import get_cache; get_cache().clear()"
```

## Files

```
reference_checker/
├── scripts/
│   ├── start.sh               # One-command launcher (macOS / Linux)
│   └── start.bat              # One-command launcher (Windows)
├── src/
│   ├── index.html             # Web UI
│   ├── app.py                 # Thin Flask wrapper
│   ├── cli.py                 # Batch CLI
│   ├── config.py              # Environment-based config
│   ├── cache.py               # SQLite API cache
│   ├── matching.py            # RapidFuzz title similarity + guards
│   ├── grobid_client.py       # GROBID full-doc + processCitation + URL/ID extraction
│   ├── sources.py             # Crossref / S2 / OpenAlex / WG21 / IETF / ISBN / web
│   ├── verifier.py            # check_single_reference orchestrator
│   └── pipeline.py            # Parallel batch driver
├── docker-compose.yml         # GROBID + app
├── Dockerfile                 # App image
├── env.example                # Configuration template
└── requirements.txt
```

## License

Same as the parent project.
