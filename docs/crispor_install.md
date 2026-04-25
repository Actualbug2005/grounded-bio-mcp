# CRISPOR install — dev machine + LXC reference

> **Audience:** anyone setting up `bio_design_grna` for the first time on a new host. Two paths documented in parallel: macOS dev machine (Apple Silicon) for wrapper development, and Debian Trixie LXC for the production deployment in Session 8b. Both paths share the same Python env + clone steps; they diverge on architecture-specific binaries.

## Conclusion first

**On dev:** clone CRISPOR, install bwa + python@3.11 via Homebrew, create the venv, copy `genomes.sample/sacCer3/` into the configured `GENOME_DIR`. Live CRISPOR exec stays gated behind `CRISPOR_LIVE=1` because the bundled x86_64 binaries need Rosetta to run on Apple Silicon — wrapper testing happens against fixtures + mocked subprocesses.

**On LXC:** apt-install bwa + python3.11, clone CRISPOR, create the venv, run `scripts/fetch_genome.sh` for each of sacCer3 / felCat9 / hg38 / mm39 (sacCer3 no-op, the others gated behind `CONFIRM_DOWNLOAD=1`). Bundled `bin/Linux-x86_64/` binaries run natively. Live CRISPOR exec runs unconditionally.

## Required environment

These env vars must be set before invoking `bio_design_grna` or `scripts/fetch_genome.sh`. Defaults in `config.Settings` target the LXC layout; dev overrides via `.env`:

| Var | Dev | LXC |
|---|---|---|
| `CRISPOR_PATH` | `~/opt/crispor` | `/opt/crispor` |
| `CRISPOR_PYTHON` | `~/opt/crispor/venv/bin/python` | `/opt/crispor/venv/bin/python` |
| `GENOME_DIR` | `~/opt/crispor/genomes` | `/var/lib/grounded_bio_mcp/genomes` (post-rename) |

`config.Settings` reads them via pydantic-settings; `bio_design_grna`'s runner factory in `server.py` consumes the settings rather than the env vars directly.

## Path A — dev machine (macOS, Apple Silicon)

### 1. System dependencies via Homebrew

```bash
brew install bwa python@3.11
```

`bwa` is the BWA aligner CRISPOR uses for off-target search. `python@3.11` is the version CRISPOR's `INSTALL.md` recommends (Python 3.9+ since CRISPOR 5.2; 3.11 is what's currently shipped on Homebrew). Default `python3` on a modern Mac may be 3.13 or 3.14; CRISPOR's older code triggers deprecation warnings under 3.13+.

### 2. Clone CRISPOR

```bash
mkdir -p ~/opt
git clone --depth 1 https://github.com/maximilianh/crisporWebsite ~/opt/crispor
```

### 3. Create the venv + install lightweight deps

```bash
cd ~/opt/crispor
python3.11 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install biopython numpy pandas scikit-learn twobitreader pytabix matplotlib xlwt
```

CRISPOR's full `requirements.txt` includes Keras + TensorFlow for Cpf1 scoring — not needed for the dev wrapper-development path. The lightweight install above is enough for `crispor.py --help` to run cleanly and is what the integration test exercises.

If `tensorflow` / `keras` are needed (full scoring on hg38 etc.), install separately on the LXC where the dependency footprint is acceptable.

### 4. Verify

```bash
~/opt/crispor/venv/bin/python ~/opt/crispor/crispor.py --help
```

Two `DeprecationWarning` lines about `cgi` and `pipes` are expected; ignore them. Help text following indicates the install is functional.

### 5. sacCer3 genome (no-op install from bundled sample)

```bash
export CRISPOR_PATH=~/opt/crispor
export GENOME_DIR=~/opt/crispor/genomes
scripts/fetch_genome.sh sacCer3
```

Idempotent — re-running is a no-op once the layout is verified. The script copies `~/opt/crispor/genomes.sample/sacCer3/` into `~/opt/crispor/genomes/sacCer3/`, which the wrapper code's `_check_genome_layout` then validates.

### 6. Live CRISPOR exec (Apple Silicon limitation)

CRISPOR's bundled `bin/Darwin/bwa` is x86_64 Mach-O. Apple Silicon runs it via Rosetta. Without Rosetta installed (`softwareupdate --install-rosetta --agree-to-license`), the live path errors with "Bad CPU type in executable" on every subprocess invocation. Two options:

- **Recommended:** keep `CRISPOR_LIVE` unset on dev. Tests that need live exec stay skipped; the wrapper is exercised against TSV fixtures + mocked subprocesses. The LXC in Session 8b is the canonical live-exec environment.
- **If needed on dev:** `softwareupdate --install-rosetta --agree-to-license`. Reversible system change but persistent. After install, `CRISPOR_LIVE=1` enables the gated tests.

## Path B — LXC (Debian Trixie)

### 1. System dependencies via apt

```bash
apt update && apt install -y bwa python3.11 python3.11-venv git curl
```

`bwa` from Trixie's package archive replaces CRISPOR's bundled binary on dev. The bundled `bin/Linux-x86_64/` directory contains UCSC kent-tools (faToTwoBit, twoBitInfo, bedClip etc) that the script + CRISPOR rely on; those run natively on x86_64 Linux without translation.

### 2. Clone + venv + deps

Same as dev:

```bash
git clone --depth 1 https://github.com/maximilianh/crisporWebsite /opt/crispor
cd /opt/crispor
python3.11 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install biopython numpy pandas scikit-learn twobitreader pytabix matplotlib xlwt
```

For full scoring on hg38 / mm39 (Doench '16 + Azimuth via Keras / TensorFlow), additionally:

```bash
venv/bin/pip install keras tensorflow h5py
```

This adds ~1.5 GB of dependencies; gate the install behind a deliberate decision in 8b.

### 3. Genome indexes (gated downloads)

```bash
export CRISPOR_PATH=/opt/crispor
export GENOME_DIR=/var/lib/grounded_bio_mcp/genomes  # post-rename in 8.5
mkdir -p "$GENOME_DIR"
chown grounded-bio-mcp:grounded-bio-mcp "$GENOME_DIR"

# sacCer3 — no-op install from bundled
scripts/fetch_genome.sh sacCer3

# felCat9 / hg38 / mm39 — pre-flight first
scripts/fetch_genome.sh felCat9
scripts/fetch_genome.sh hg38
scripts/fetch_genome.sh mm39

# Once sizes + URLs surface, fire the gated fetch one at a time:
CONFIRM_DOWNLOAD=1 scripts/fetch_genome.sh felCat9
CONFIRM_DOWNLOAD=1 scripts/fetch_genome.sh hg38
CONFIRM_DOWNLOAD=1 scripts/fetch_genome.sh mm39
```

Verified URLs + compressed sizes (probed 2026-04-25):

| Genome | URL | Compressed | Estimated wall time |
|---|---|---|---|
| felCat9 | `https://hgdownload.soe.ucsc.edu/goldenPath/felCat9/bigZips/felCat9.fa.gz` | 774.4 MiB | ~25 min |
| hg38 | `https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz` | 938.1 MiB | ~35 min |
| mm39 | `https://hgdownload.soe.ucsc.edu/goldenPath/mm39/bigZips/mm39.fa.gz` | 830.2 MiB | ~30 min |

Total disk after extraction + BWA-index: ~20 GB across all three. Fits comfortably in the LXC's 80 GB data mount.

### 4. segments.bed (locus_class classification)

`scripts/fetch_genome.sh` does **not** produce `<genome>.segments.bed`. CRISPOR runs without it, but `bio_design_grna`'s `locus_class` field falls back to `"unknown"` for all off-targets — losing the CDS / intron / intergenic surface the spec promises.

To produce `segments.bed`, CRISPOR's webserver helper consumes a GFF gene annotation and writes the BED. That tooling has hardcoded paths to a webserver layout (`/var/www/crispor/...`) that we don't replicate. Three forward paths, all 8b decisions:

- Run CRISPOR's deprecated `crisprAddGenome.old` script after patching its hardcoded paths
- Build segments.bed manually from a GFF using `awk`/`bedtools` per CRISPOR's segment.bed format (`ig:G1|G2`, `ex:GENE`, `in:GENE` prefix conventions)
- Accept `locus_class="unknown"` for hg38/mm39 in the initial deployment, add segments.bed in a follow-up session

The user's deployment-acceptance call decides between these. sacCer3 ships with its own `segments.bed` so locus_class works out-of-the-box on the yeast smoke.

### 5. systemd integration

Per the Session 8b prompt, the systemd unit's `EnvironmentFile=/etc/grounded_bio_mcp/env` carries `CRISPOR_PATH`, `CRISPOR_PYTHON`, and `GENOME_DIR`. The unit runs as the `grounded-bio-mcp` system user; `chown -R grounded-bio-mcp:grounded-bio-mcp /opt/crispor` after install so the user can read CRISPOR + write its temp dirs (CRISPOR uses `/tmp` by default).

## Verification

Once installed, the wrapper smoke runs through the in-process FastMCP client. On dev (no `CRISPOR_LIVE`):

```bash
RUN_INTEGRATION=1 pytest tests/test_tools/test_design_grna.py
# ↷ test_bio_design_grna_live_against_sacCer3 SKIPPED (CRISPOR_LIVE=1 to enable)
```

On LXC (or a host with bundled binaries runnable):

```bash
RUN_INTEGRATION=1 CRISPOR_LIVE=1 pytest tests/test_tools/test_design_grna.py::test_bio_design_grna_live_against_sacCer3
```

Successful live run: candidate guides found, top guide spacer 20 nt with PAM stripped, `locus_class` populated from `sacCer3.segments.bed`, provenance carries `source: "CRISPOR"` and `genome: "sacCer3"`.

## Troubleshooting

**"Bad CPU type in executable"** — Apple Silicon without Rosetta. See "Live CRISPOR exec" above. Either install Rosetta or keep `CRISPOR_LIVE` unset.

**"CRISPOR genome 'sacCer3' index not found"** — `GENOME_DIR/sacCer3/` is missing or incomplete. Run `scripts/fetch_genome.sh sacCer3` to install.

**"sacCer3.fa.bwt missing"** — partial install. Delete `GENOME_DIR/sacCer3/` and re-run `scripts/fetch_genome.sh sacCer3`.

**`crispor.py` ImportError on `keras` / `tensorflow`** — full-scoring deps not installed. Either install them (`pip install keras tensorflow h5py`) or run with `--noEffScores`. The wrapper currently invokes the full pipeline; if scoring deps are missing, the subprocess exits non-zero and the wrapper surfaces `CrisporRunFailed`.

**`crispor.py` DeprecationWarning on `cgi` / `pipes`** — cosmetic, ignore. CRISPOR's older codebase uses stdlib modules removed in 3.13+; we run under 3.11.15 where they still work.

## What this document doesn't cover

- LXC provisioning (CTID, IP, resources) — see `prompts/session-8b-deployment.md`
- Caddy reverse proxy + bearer auth — see spec §9.3
- Evaluation harness (Q10 depends on a working CRISPOR install) — see spec §10.4
