# CRISPOR output format — `bio_design_grna` wrapper reference

> **Source:** `https://github.com/maximilianh/crisporWebsite` at commit `ed47b7e856010ad0f9f1660872563ef9f736e76c` (2026-04-14). Probed 2026-04-25 against bundled `sampleFiles/out/*.tsv` + `crispor.py --help` under Python 3.11.15.
>
> **Why this document exists.** Spec §4.7 describes the expected `bio_design_grna` output but does not pin CRISPOR's own command-line output format. The wrapper translates CRISPOR's TSV to spec output, so the column set and value conventions are load-bearing. Capturing them here means future contributors don't have to re-derive when CRISPOR ships a new release.

## Conclusion first

CRISPOR's command-line output is **two TSV files** — guides and off-targets — written to paths the caller supplies. The guides TSV header begins with `#`; the off-targets TSV header does not. Column sets are **not fixed across genomes** — at minimum hg19 emits an extra `CCTop-Score` column that sacCer3 does not. **Parse defensively from the header row, not by position.** Score fields can carry the literal string `NotEnoughFlankSeq` per-guide when CRISPOR couldn't extract enough flanking sequence to compute a score; that is information ("score not computable for this guide"), not failure, and the wrapper surfaces it as a null with `score_unavailable_reason` rather than a tool-level error.

## Invocation

```
crispor.py [options] <org> <inFasta> <guidesOut> [-o <offtargetsOut>] [--genomeDir <dir>]
```

- `<org>`: subdirectory name under `--genomeDir` (default `./genomes`); must contain the BWA index, 2bit, sizes, segments.bed, and genomeInfo.tab files. CRISPOR's `genomes.sample/sacCer3/` is the canonical reference layout — see [audit_session_8a.md](./audit_session_8a.md) for the file inventory.
- `<inFasta>`: input sequence(s) to design guides against. Multi-FASTA is supported; CRISPOR concatenates with `N` separators internally.
- `<guidesOut>`: tab-separated guides table — see "Guides TSV" below.
- `-o <offtargetsOut>`: optional; tab-separated off-targets table — see "Off-targets TSV" below. Without `-o`, off-targets are not emitted at all.
- `--noEffScores`: skips Doench / Azimuth / Moreno-Mateos / etc. scoring — output keeps the columns but every score field is empty. Useful when scoring deps (keras/tensorflow) are unavailable.
- Default PAM is `NGG` (SpCas9). `-p TTTN` switches to Cpf1 mode with PAM 5' of guide.
- Default mismatch ceiling for off-target search is 4; configurable via `--mm`.

## Guides TSV

**Header (with `#` prefix):**

```
#seqId  guideId  targetSeq  mitSpecScore  offtargetCount  targetGenomeGeneLocus
Doench '16-Score  Doench '16-Old-Score  Chari-Score  Xu-Score  Doench '14-Score
Wang-Score  Moreno-Mateos-Score  Azimuth in-vitro-Score  [CCTop-Score]
Out-of-Frame-Score
```

`CCTop-Score` appears for hg19's bundled `hg19.guides.tsv` but not sacCer3's `sample.sacCer.tsv`. The rest of the column set is stable across both samples. The wrapper reads the header row and indexes by name — never by position.

**Per-guide row fields:**

| Field | Type | Notes |
|---|---|---|
| `seqId` | string | Echoes the input FASTA record id (or BED-derived label like `hg19_dna range=chr7:5564967-5565467 5'pad=0 ...` for BED input) |
| `guideId` | string | `<position><strand>` e.g. `41forw`, `80rev` — position is 1-based start within the input sequence |
| `targetSeq` | string | 23 nt: 20 nt guide + 3 nt PAM, in DNA alphabet, on the strand named by `guideId`'s suffix |
| `mitSpecScore` | int 0-100 | MIT specificity score (Hsu 2013); higher is more specific |
| `offtargetCount` | int | Total off-targets at the configured mismatch ceiling |
| `targetGenomeGeneLocus` | string | locusDesc-style classification of the on-target site — see "locusDesc format" below |
| `Doench '16-Score`, `Doench '16-Old-Score`, `Chari-Score`, `Xu-Score`, `Doench '14-Score`, `Wang-Score`, `Moreno-Mateos-Score`, `Azimuth in-vitro-Score`, `CCTop-Score` (when present), `Out-of-Frame-Score` | int / float / `NotEnoughFlankSeq` | Per-model efficiency scores. `NotEnoughFlankSeq` means CRISPOR couldn't extract enough flanking sequence (typically ~30 nt either side) to compute that score — surface as null with reason in spec output. Empty string means `--noEffScores` was passed. |

## Off-targets TSV

**Header (no `#` prefix):**

```
seqId  guideId  guideSeq  offtargetSeq  mismatchPos  mismatchCount
mitOfftargetScore  cfdOfftargetScore  chrom  start  end  strand  locusDesc
```

The header on this TSV does **not** carry the `#` that the guides TSV uses — minor inconsistency in CRISPOR's output, easy parser landmine. Wrapper handles both with the same header-row read regardless of `#`.

**Per-row fields:**

| Field | Type | Notes |
|---|---|---|
| `seqId`, `guideId` | string | Match the parent guide's keys in the guides TSV — joining is by `(seqId, guideId)` |
| `guideSeq` | string | 23 nt — the parent guide's targetSeq, repeated per off-target row for self-contained reading |
| `offtargetSeq` | string | 23 nt — the off-target site sequence as found in the genome, on the strand given by the `strand` column |
| `mismatchPos` | string | 20-character mask string: `.` = match at that position, `*` = mismatch. Position 1 is leftmost (PAM-distal). Length always 20 (PAM excluded). |
| `mismatchCount` | int | Should equal the count of `*` in `mismatchPos` |
| `mitOfftargetScore`, `cfdOfftargetScore` | float | Per-off-target MIT (Hsu 2013) and CFD (Doench 2016) scores. Higher = more likely a real off-target effect. CFD is the more recent / generally preferred metric. |
| `chrom`, `start`, `end` | string, int, int | Genomic coordinates of the off-target. 0-based, half-open (UCSC BED convention). |
| `strand` | `+` / `-` | Orientation of the off-target match |
| `locusDesc` | string | locusDesc-style classification — see below |

## locusDesc format

A single field encoding both the **classification** and the **gene name(s) involved**. Three patterns observed:

| Pattern | Meaning | Examples from samples |
|---|---|---|
| `exon:<gene>` | Off-target falls inside a coding exon of the named gene | `exon:YAL069W/YAL068W-A` (overlapping yeast genes) |
| `intron:<gene>` | Off-target falls inside an intron of the named gene | `intron:IQCH`, `intron:SPI1`, `intron:MRPS27` |
| `intergenic:<gene>-<gene>` | Off-target falls between named flanking genes | `intergenic:RN7SKP167-FGFR2`, `intergenic:FBXL18-ACTB`, `intergenic:YAL069W/YAL068W-A-PAU8` |

Slashes inside gene names indicate overlapping annotations (CRISPOR concatenates names of all overlapping features); the wrapper preserves the slash form rather than picking one. The `-` separator between genes in the `intergenic:` form is unambiguous because gene names from CRISPOR's `segments.bed` use no hyphens (only slashes for overlap).

The spec §4.7 output names this surface "CDS/intron/intergenic classification" — `exon` maps cleanly to "CDS"; `intron` and `intergenic` are direct.

## `NotEnoughFlankSeq` semantics

**What it means.** CRISPOR computes efficiency scores against a window of flanking sequence around the protospacer (typically ~30 nt on each side, depending on the scoring model). When the input FASTA is too short or the guide sits near the edge, that window is truncated and the score cannot be computed. CRISPOR writes the literal string `NotEnoughFlankSeq` in those cells rather than emitting `0`, an empty cell, or NA.

**Wrapper handling.** Per-guide null with an accompanying `score_unavailable_reason: "insufficient flanking sequence"` (or per-score equivalent) — never silently zero, never tool-level error. Models reading the output need to distinguish "score is 0" from "score not computable for this guide", so the schema makes both representable.

**When it happens.** sacCer3's bundled sample input (`sample.sacCer3.fa`, ~180 nt with guides at positions 34-200) reproduces it on every guide because the input is much shorter than what scoring needs. Real-world inputs of 500+ nt rarely trigger it for interior guides; edge guides may still trip.

## What the wrapper relies on

Going-in assumptions for the wrapper code, all verified against the bundled samples:

1. Both TSVs are tab-separated and well-formed (no embedded tabs in fields). Verified across both samples.
2. The guides TSV header is the first non-empty line and begins with `#`. The off-targets TSV header is the first non-empty line and does not.
3. `(seqId, guideId)` is a unique key in the guides TSV and a join key into the off-targets TSV.
4. Score columns are either floats, ints, the literal `NotEnoughFlankSeq`, or empty (when `--noEffScores`). No other sentinel values observed.
5. `mismatchPos` is always exactly 20 characters of `.` / `*`. PAM-distal first.
6. `locusDesc` always matches one of the three patterns above. No empty values observed; the wrapper still treats absence as "intergenic:unknown" rather than crashing — defensive parse.

## Where this format breaks down

Edges to watch for in CRISPOR upgrades:

1. **Column-set drift.** New scoring models can land any time; the wrapper needs to tolerate unknown extra columns gracefully (preserve them in a `additional_scores` dict or drop with a log warning).
2. **Header convention drift.** If CRISPOR's authors normalise the `#`-prefix difference between guides and off-targets, wrapper still works because we read by header-name not by line-prefix.
3. **`NotEnoughFlankSeq` rename.** Possible — track `_NULL_SENTINELS` constant in the wrapper so adding new sentinels (e.g. `NoModel`, `OutOfRange`) is one-line.

## Verification artefacts

The sample TSVs probed live in `~/opt/crispor/sampleFiles/out/`:

- `sample.sacCer.tsv` — sacCer3 guides, demonstrates `NotEnoughFlankSeq` across all scoring columns
- `hg19.guides.tsv` — hg19 guides with full scoring populated, demonstrates the `CCTop-Score` extra column
- `hg19.offs.tsv` — hg19 off-targets, demonstrates all three locusDesc patterns

These remain in the upstream repo; the wrapper's test fixtures (under `tests/fixtures/crispor/` once added) are derivatives that capture the format conventions without copying CRISPOR's sample data, keeping the licence boundary clean (CRISPOR is academic-free / commercial-paid; our project is Apache-2.0 from Session 8.5 onward).
