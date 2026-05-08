# Reference Checker

A tool for journal editors and authors to verify the references in academic
PDFs against authoritative databases (OpenAlex, Crossref, Semantic Scholar,
WG21, IETF, OpenLibrary).

## What's new in this version

Three things, aimed at making the tool deployable, scalable, and accurate
enough for editorial use:

1. **One-command setup.** No more separate Docker + Ollama + Flask steps.
   `./scripts/start.sh` (or `scripts\start.bat` on Windows) brings up everything and waits
   for it to be ready. Editors don't install Python, GROBID, or Ollama —
   just Docker Desktop.
2. **Batch mode for hundreds of PDFs.** A `cli.py batch` command processes a
   directory of PDFs one at a time, writing results to SQLite as it goes.
   Fully resumable: kill the run, re-launch with `--resume`, it skips what's
   done.
3. **Better matching, fewer false positives.** RapidFuzz's `token_sort_ratio`
   + `partial_ratio` blend with subtitle-stripping and a length-aware
   coverage guard replaces the old hand-rolled Levenshtein. GROBID's
   `processCitation` endpoint replaces the Ollama LLM call for messy
   references — it's purpose-built for citation parsing, runs in the same
   container we already need, and removes the ~4 GB Ollama install.

---

## Prerequisites

- **Docker Desktop** ([download](https://www.docker.com/products/docker-desktop/)).
- That's it. No Python, no GROBID, no Ollama.

## Quick start

```bash
# macOS / Linux
./scripts/start.sh

# Windows
scripts\start.bat
```

On the first run the script:

1. Copies `.env.example` to `.env` and opens it for editing. Set
   `OPENALEX_EMAIL` to any valid email address (this puts you in OpenAlex's
   "polite pool" with faster rate limits) and re-run.
2. Builds the app image and pulls GROBID (~2 GB, one-time download).
3. Waits for GROBID's ML models to load (~30–60 s) and for the app's health
   check.

When you see the success banner, open <http://localhost:5000>.

To stop everything: `docker compose down`.

## Web UI

Visit <http://localhost:5000>. You can:

- Paste raw reference text and click **Verify** for a quick check.
- Upload a single PDF — references are auto-extracted by GROBID.
- Upload a batch of PDFs (typically ≤ 20 at a time through the browser).

For corpora larger than ~20 PDFs, use **batch mode** instead.

## Batch mode (CLI) — for 600 PDFs

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
cp /path/to/your/600/*.pdf data/pdfs/

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

### Inspecting a finished run

```bash
# Summary across all PDFs and references
docker compose exec app python cli.py stats --output /data/results/run1.db

# Export every reference as CSV
docker compose exec app python cli.py export \
    --output /data/results/run1.db \
    --csv    /data/results/run1.csv

# Export just the references that need editor review (NOT_FOUND, NOT_REFERENCE,
# FLAWED_REFERENCE) — usually the most useful slice
docker compose exec app python cli.py export \
    --output /data/results/run1.db \
    --csv    /data/results/run1-needs-review.csv \
    --not-found-only
```

The CSV columns are: `filename, bucket, original_text, matched_title,
matched_year, matched_url, matched_source, note`.

### Single-PDF check via CLI

Quick spot-check without the web UI:

```bash
docker compose exec app python cli.py check --pdf /data/pdfs/some_paper.pdf
```

## Configuration (`.env`)

All settings are environment variables. The supported ones:

| Variable               | Default                  | Purpose |
|------------------------|--------------------------|---------|
| `OPENALEX_EMAIL`       | _(empty)_                | Your email — required for the polite pool. |
| `S2_API_KEY`           | _(empty)_                | Optional Semantic Scholar key for higher limits. |
| `USE_OLLAMA_FALLBACK`  | `false`                  | Enable the LLM fallback for stubborn refs. |
| `OLLAMA_HOST`          | `http://localhost:11434` | Where the LLM lives (uncomment Ollama in `docker-compose.yml` first). |
| `OLLAMA_MODEL`         | `llama3`                 | Ollama model to use. |
| `MAX_REF_WORKERS`      | `6`                      | Parallel reference checks per PDF. |
| `BATCH_PDF_WORKERS`    | `2`                      | Parallel PDFs in the web batch endpoint. |
| `VERIFY_SCORE`         | `90`                     | Title-similarity threshold for VERIFIED. |
| `FLAWED_SCORE`         | `78`                     | Threshold for FLAWED_REFERENCE (editor review). |
| `MIN_CONSIDER_SCORE`   | `60`                     | Below this, candidate is rejected. |
| `CACHE_TTL_DAYS`       | `30`                     | How long to keep cached API responses. |

Edit `.env` and run `docker compose up -d` to pick up changes.

## How verification works

For each reference the verifier walks through phases in order, returning as
soon as one succeeds:

| Phase | What it tries                                       | Source          |
|-------|-----------------------------------------------------|-----------------|
| 0     | WG21 / IETF RFC / ISBN sniper                       | wg21.link, datatracker.ietf.org, OpenLibrary |
| 1     | DOI / arXiv exact lookup                            | OpenAlex → Crossref → S2 |
| 2     | GROBID-extracted title/author/year search           | OpenAlex |
| 3     | GROBID `processCitation` re-parse, then search      | GROBID + OpenAlex |
| 3.5   | Crossref bibliographic search                       | Crossref |
| 4     | Semantic Scholar search                             | Semantic Scholar |
| 4.5   | Optional Ollama LLM parse (if `USE_OLLAMA_FALLBACK=true`) | Ollama |
| 5     | Returns `NOT_FOUND` or `NOT_REFERENCE`              | — |

Every external HTTP call is wrapped in a SQLite cache (`data/cache/api_cache.db`,
30-day TTL). Re-runs are 3–5× faster, and you can tune the thresholds in
`.env` without re-paying for API calls.

### Result buckets

| Bucket               | Meaning                                                          |
|----------------------|------------------------------------------------------------------|
| `verified`           | Confidently matched in a database.                               |
| `edition_mismatch`   | Title matched but the year is off — likely a different edition.  |
| `flawed_reference`   | A near-match exists; the citation is probably correct but garbled. Editor should look. |
| `not_found`          | No match. Reference may be wrong, very recent, or in grey literature. |
| `not_reference`      | Heuristically not a reference (e.g. body-text caught by GROBID). |

The `flawed_reference` and `not_found` buckets are usually the most valuable
output for an editor.

## Accuracy improvements explained

**Title matching.** The previous Levenshtein metric was character-level and
brittle — it failed on hyphenation differences ("polynomial-time" vs
"polynomial time"), subtitle additions ("Title: An Introduction"), and
trailing metadata ("Title — Vaswani et al, NeurIPS 2017"). The new
`title_similarity()` blends `token_sort_ratio` (word-level, robust to
punctuation) with `partial_ratio` (substring-aware, robust to subtitles),
and adds a length-aware coverage guard that prevents short titles from
falsely matching longer ones (e.g. "Algorithms" → "Introduction to
Algorithms" is correctly rejected).

**Reference parsing.** GROBID's full-document parser already handled clean
references. For the messy ones — wrapped lines, missing punctuation, body
text mixed in — the original code went to Ollama. Replaced that with
GROBID's own `processCitation` endpoint, which is purpose-built for
single-citation parsing, runs in the container we already have, and is
~10× faster.

**DOI extraction.** Added a line-break repair pass: PDFs sometimes split
DOIs at line boundaries (`10.1145/3290605.\n3300823`). The new
`repair_doi_with_linebreaks()` finds these.

**Suspicion heuristic.** Relaxed the threshold for flagging a reference as
"probably not a reference at all" — the original 600-char limit was
catching legitimate long references. Now 900 chars, and prose markers
("we", "our", "is used") only trigger when no year is also present.

**Threshold retuning.** The verification cutoff dropped from 95
(Levenshtein) to 90 (RapidFuzz blend), with a flawed-reference cutoff at
78. These were tuned against representative cases including subtitle
variation, hyphenation, and edition-on-the-end metadata.

## Troubleshooting

**"Cannot reach GROBID"**
GROBID needs ~30–60 s after `docker compose up` for its Java + ML models
to load. Check `docker compose logs grobid`. If it never starts, raise
`JAVA_OPTS=-Xmx4g` to `-Xmx6g` (or higher) in `docker-compose.yml`.

**Health check shows `grobid_reachable: false`**
Same as above — usually a transient startup issue. Wait 60 s and reload.

**Lots of `not_found` results that look right**
First check that `OPENALEX_EMAIL` is set in `.env`; without it OpenAlex
falls back to public-pool rate limits and you may be silently throttled.
If that's not it, the references are likely too garbled for a structured
title to be extracted — enable Ollama as a last-resort fallback by
uncommenting the Ollama service in `docker-compose.yml` and setting
`USE_OLLAMA_FALLBACK=true` in `.env`.

**Disk filling up during a 600-PDF batch**
The persistent volume is `./data`. The cache DB is the biggest contributor
and is safe to delete (it'll just rebuild on the next run):
`rm data/cache/api_cache.db`. The results DB at `data/results/run1.db` is
your actual output — back that up first.

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
│   ├── matching.py            # RapidFuzz title similarity
│   ├── grobid_client.py       # GROBID full-doc + processCitation
│   ├── sources.py             # OpenAlex / Crossref / S2 / WG21 / IETF / ISBN
│   ├── verifier.py            # check_single_reference orchestrator
│   └── pipeline.py            # Parallel batch driver
├── docker-compose.yml         # GROBID + app
├── Dockerfile                 # App image
├── env.example                # Configuration template
└── requirements.txt
```

## License

Same as the parent project.