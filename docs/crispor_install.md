# CRISPOR install — dev machine + LXC reference

> **Audience:** anyone setting up `bio_design_grna` for the first time on a new host. Two paths documented in parallel: macOS dev machine (Apple Silicon) for wrapper development, and Debian Trixie LXC for the production deployment in Session 8b. Both paths share the same Python env + clone steps; they diverge on architecture-specific binaries.

## Conclusion first

**On dev:** clone CRISPOR, install bwa + python@3.11 via Homebrew, create the venv, copy `genomes.sample/sacCer3/` into the configured `GENOME_DIR`. Live CRISPOR exec stays gated behind `CRISPOR_LIVE=1` because the bundled x86_64 binaries need Rosetta to run on Apple Silicon — wrapper testing happens against fixtures + mocked subprocesses.

**On LXC:** apt-install bwa + liblmdb-dev (system deps), use uv-managed Python 3.10 (not 3.11 — see below), create the venv with `uv venv`, install CRISPOR's pinned runtime deps from `scripts/crispor_runtime_requirements.txt`, copy `scripts/crispor_sitecustomize.py` into the venv's `site-packages/` to handle the Azimuth-2.0 random-state compatibility shim, then run `scripts/fetch_genome.sh` for each of sacCer3 / felCat9 / hg38 / mm39 (sacCer3 no-op, the others gated behind `CONFIRM_DOWNLOAD=1`). Bundled `bin/Linux-x86_64/` binaries run natively. Live CRISPOR exec runs unconditionally.

**Why Python 3.10 specifically on LXC:** CRISPOR's bundled Azimuth-2.0 (Doench '16 fusi scoring) and rs3 ML models were built against scikit-learn 1.0.2, numpy 1.22.4, and lightgbm 3.3.5. None of those library versions have cp311 wheels on PyPI, so they can't be installed binary-only on Python 3.11. Python 3.10 has wheels for the entire pinned stack and is what CRISPOR's upstream `requirements.txt` was last cut against. The grounded-bio-mcp main app stays on Python 3.11; only CRISPOR's subvenv flips to 3.10.

## Required environment

These env vars must be set before invoking `bio_design_grna` or `scripts/fetch_genome.sh`. Defaults in `config.Settings` target the LXC layout; dev overrides via `.env`:

| Var | Dev | LXC |
|---|---|---|
| `CRISPOR_PATH` | `~/opt/crispor` | `/opt/crispor` |
| `CRISPOR_PYTHON` | `~/opt/crispor/venv/bin/python` | `/opt/crispor/venv/bin/python` |
| `GENOME_DIR` | `~/opt/crispor/genomes` | `/var/lib/grounded-bio-mcp/genomes` (post-rename) |

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
apt update && apt install -y bwa build-essential git curl liblmdb-dev
```

- `bwa` from Trixie's package archive is what CRISPOR shells out to for off-target alignment.
- `build-essential` is needed because two of CRISPOR's pinned deps (`twobitreader` and `pytabix`) are sdist-only on PyPI and source-build during install.
- `liblmdb-dev` is a fallback for the `lmdb` Python binding. Recent `lmdb` wheels bundle their own LMDB so this isn't strictly required for `lmdb==1.3.0`, but keeping it installed protects against future bumps that drop the bundled library.

The bundled `bin/Linux-x86_64/` directory inside the CRISPOR clone contains UCSC kent-tools (faToTwoBit, twoBitInfo, bedClip etc.) that the script + CRISPOR rely on; those run natively on x86_64 Linux without translation.

Trixie's apt has no `python3.10` package, so we use uv to manage Python 3.10 directly. uv is installed system-wide at `/usr/local/bin/uv` per the Session 8b LXC bootstrap.

### 2. Clone + venv + pinned deps + compat shim

```bash
# Clone CRISPOR into the service-user namespace
git clone --depth 1 https://github.com/maximilianh/crisporWebsite /opt/grounded-bio-mcp/crispor
cd /opt/grounded-bio-mcp/crispor

# Install Python 3.10 toolchain via uv (one-time download, ~30 MB)
uv python install 3.10

# Create the venv on Python 3.10
uv venv venv --python 3.10

# Install CRISPOR's pinned runtime deps. DO NOT add --only-binary=:all: —
# twobitreader and pytabix are sdist-only and source-build in seconds.
uv pip install --python venv/bin/python --reinstall \
    -r /opt/grounded-bio-mcp/app/scripts/crispor_runtime_requirements.txt

# Copy the random-state compatibility shim into the venv. This is what
# enables the bundled Azimuth-2.0 model to load under newer numpy.
cp /opt/grounded-bio-mcp/app/scripts/crispor_sitecustomize.py \
    venv/lib/python3.10/site-packages/sitecustomize.py
```

#### Why the pinned versions matter

CRISPOR's bundled scoring models (Azimuth-2.0 for fusi/Doench '16 and rs3 for the Listgarten Group's score) were trained and serialised with specific library versions. Newer versions break their predict paths in non-obvious ways:

| Library | Pin | Why |
|---|---|---|
| scikit-learn | 1.0.2 | 1.2+ removes `sklearn.ensemble._gb_losses` (Azimuth load errors with `ModuleNotFoundError`); 1.1+ restructures `GradientBoostingRegressor` so 1.0.x serialised models predict-time-fail with `AttributeError: '_loss'` |
| numpy | 1.22.4 | 1.21+ simplified `__randomstate_ctor` signature, breaking the random-state portion of the Azimuth model (the sitecustomize shim handles this; the pin keeps the rest of the ABI stable) |
| lightgbm | 3.3.5 | 4.0+ changes `_n_classes` initialisation so rs3's serialised model crashes during predict() |
| Plus 12 others | (see file) | Compatible transitives that satisfy the above without re-resolving them away |

The full set is captured in [`scripts/crispor_runtime_requirements.txt`](../scripts/crispor_runtime_requirements.txt). Use `--reinstall` (broad, not `--reinstall-package`) when running this against an existing venv so uv re-resolves the entire graph in one atomic pass — anything less risks evicting pins during transitive resolution.

### 3. Verify the venv

```bash
/opt/grounded-bio-mcp/crispor/venv/bin/python -c "
import sklearn, numpy, scipy, pandas, matplotlib, lightgbm, rs3, Bio, lmdb, lmdbm
print(f'sklearn {sklearn.__version__}, numpy {numpy.__version__}')
print(f'scipy {scipy.__version__}, pandas {pandas.__version__}')
print(f'matplotlib {matplotlib.__version__}, lightgbm {lightgbm.__version__}')
print(f'rs3 {rs3.__version__}, Bio {Bio.__version__}, lmdb {lmdb.__version__}')
import sklearn.ensemble._gb_losses
import sitecustomize
from numpy.random import _pickle as p
print('shim loaded; ctor =', p.__randomstate_ctor)
"
```

Expected: sklearn 1.0.2, numpy 1.22.4, scipy 1.8.1, pandas 1.4.2, matplotlib 3.5.2, lightgbm 3.3.5, rs3 0.0.15, Bio 1.79, lmdb 1.3.0. The `_compat_ctor` function reference confirms the shim is active.

### 3. Genome indexes (gated downloads)

```bash
export CRISPOR_PATH=/opt/crispor
export GENOME_DIR=/var/lib/grounded-bio-mcp/genomes  # post-rename in 8.5
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

Per the Session 8b prompt, the systemd unit's `EnvironmentFile=/etc/grounded-bio-mcp/env` carries `CRISPOR_PATH`, `CRISPOR_PYTHON`, and `GENOME_DIR`. The unit runs as the `grounded-bio-mcp` system user; `chown -R grounded-bio-mcp:grounded-bio-mcp /opt/crispor` after install so the user can read CRISPOR + write its temp dirs (CRISPOR uses `/tmp` by default).

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

**`crispor.py` DeprecationWarning on `cgi` / `pipes`** — cosmetic, ignore. CRISPOR's older codebase uses stdlib modules removed in 3.13+; we run under Python 3.10.x where they still work.

**`ModuleNotFoundError: No module named 'sklearn.ensemble._gb_losses'`** — installed sklearn is >=1.2 (which removed that internal module). The bundled Azimuth-2.0 model needs sklearn <1.2. Reinstall from [`scripts/crispor_runtime_requirements.txt`](../scripts/crispor_runtime_requirements.txt) which pins sklearn 1.0.2.

**`AttributeError: 'GradientBoostingRegressor' object has no attribute '_loss'`** — installed sklearn is in the 1.1.x range (which restructured the loss attribute internally). The Azimuth model was built against sklearn 1.0.x. Same fix as above: pin to sklearn 1.0.2.

**`TypeError: __randomstate_ctor() takes from 0 to 1 positional arguments but 2 were given`** — the random-state compatibility shim is missing from the venv's `site-packages/`. Copy [`scripts/crispor_sitecustomize.py`](../scripts/crispor_sitecustomize.py) into `<venv>/lib/python3.10/site-packages/sitecustomize.py` and re-run.

**rs3 predict crash with `_n_classes=None`** — installed lightgbm is >=4.0 (which changed the internal class-count initialisation). The rs3 model needs lightgbm 3.x. Reinstall from the requirements file which pins lightgbm 3.3.5.

**`ModuleNotFoundError: No module named 'lmdbm'`** (or `lmdb`) — the LMDB-backed outcome cache deps weren't installed. Both are pinned in the requirements file (lmdb 1.3.0, lmdbm 0.0.5); a fresh install from the requirements file picks them up.

**Compiled stack disappears after a `--reinstall <single package>` command** — uv's `--reinstall` re-resolves the dependency graph and may evict pinned packages whose `>=` constraints from the new package's transitives let it pick newer or differently-resolved alternatives. Always reinstall the full requirements file (`uv pip install --reinstall -r scripts/crispor_runtime_requirements.txt`) rather than touching individual packages, so all 15 pins are negotiated as a single atomic resolution pass.

## What this document doesn't cover

- LXC provisioning (CTID, IP, resources) — see `prompts/session-8b-deployment.md`
- Caddy reverse proxy + bearer auth — see spec §9.3
- Evaluation harness (Q10 depends on a working CRISPOR install) — see spec §10.4
