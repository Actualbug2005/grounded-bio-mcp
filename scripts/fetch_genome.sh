#!/usr/bin/env bash
# scripts/fetch_genome.sh — install a CRISPOR genome layout for bio_design_grna.
#
# Two modes, dispatched on genome ID:
#
#   sacCer3
#       Copies from CRISPOR's bundled genomes.sample/sacCer3/ into
#       $GENOME_DIR/sacCer3/. No download. Idempotent — re-running is
#       a no-op once the layout is verified. Used as the dev-machine
#       smoke target for bio_design_grna's wrapper.
#
#   felCat9 | hg38 | mm39
#       Pre-flights the UCSC goldenPath FASTA URL, captures Content-Length,
#       and prints the verified URL + compressed size + target path.
#       Without CONFIRM_DOWNLOAD=1 the script exits there — gate fires
#       per Session 7's Kazusa pattern. With CONFIRM_DOWNLOAD=1 the
#       script downloads, decompresses, runs `bwa index`, and produces
#       the 2bit + sizes + minimal genomeInfo.tab files CRISPOR expects.
#       This fetch path is untested on dev (Apple Silicon cannot run
#       CRISPOR's bundled x86_64 binaries without Rosetta); the LXC in
#       Session 8b is the canonical exercise environment.
#
# Limitations of the fetch path:
#   - Does not produce <genome>.segments.bed. CRISPOR will still run
#     against the resulting layout, but off-target locus_class will
#     report "unknown" rather than CDS/intron/intergenic. Building
#     segments.bed requires a GFF annotation processed through
#     CRISPOR's gene-region pipeline; that step lands on the LXC in
#     Session 8b alongside the actual genome installs.
#
# Required env vars:
#   CRISPOR_PATH        Path to the CRISPOR clone (contains genomes.sample/,
#                       crispor.py, bin/).
#   GENOME_DIR          Where genome layouts land — typically
#                       $CRISPOR_PATH/genomes (dev) or
#                       /var/lib/grounded_bio_mcp/genomes (LXC).
#
# Optional env vars:
#   CONFIRM_DOWNLOAD    Set to 1 to actually fetch felCat9/hg38/mm39.
#                       Without it, only pre-flight runs.
#
# Exit codes:
#   0  success (or pre-flight only when CONFIRM_DOWNLOAD unset)
#   1  bad usage / unknown genome
#   2  CRISPOR_PATH missing required artefact (genomes.sample/<genome>)
#   3  layout verification failed after install
#   4  network pre-flight failed (URL unreachable / no Content-Length)
#   5  required tool missing (bwa / faToTwoBit / twoBitInfo)
#   6  fetch / decompress / index failed

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/fetch_genome.sh <genome>

Genomes:
  sacCer3   yeast — bundled with CRISPOR; copies from genomes.sample/
                   (idempotent, no download)
  felCat9   cat   — UCSC FASTA download + BWA index (gated)
  hg38      human — UCSC FASTA download + BWA index (gated)
  mm39      mouse — UCSC FASTA download + BWA index (gated)

Required env:
  CRISPOR_PATH     CRISPOR install directory
  GENOME_DIR       Where the genome layout lands

Optional env:
  CONFIRM_DOWNLOAD=1   actually fetch (otherwise pre-flight only)
USAGE
}

if [[ $# -ne 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

GENOME="$1"

: "${CRISPOR_PATH:?CRISPOR_PATH must be set (path to the CRISPOR clone)}"
: "${GENOME_DIR:?GENOME_DIR must be set (where genome layouts land)}"

DEST_DIR="$GENOME_DIR/$GENOME"

# CRISPOR's required layout files. check_layout returns 0 iff all present.
required_files=(
  "$GENOME.2bit"
  "$GENOME.fa.amb"
  "$GENOME.fa.ann"
  "$GENOME.fa.bwt"
  "$GENOME.fa.pac"
  "$GENOME.fa.sa"
  "$GENOME.sizes"
  "genomeInfo.tab"
)
# segments.bed is strongly recommended (drives locus_class classification)
# but not strictly required — CRISPOR runs without it, falling back to
# "unknown" locus class. We list it separately so check_layout doesn't
# fail for genomes that haven't had segments.bed built yet.

check_layout() {
  local dir="$1"
  for f in "${required_files[@]}"; do
    if [[ ! -e "$dir/$f" ]]; then
      return 1
    fi
  done
  return 0
}

# ----------------------------------------------------------------------
# sacCer3 — no-op install from CRISPOR's bundled sample
# ----------------------------------------------------------------------
if [[ "$GENOME" == "sacCer3" ]]; then
  SAMPLE_DIR="$CRISPOR_PATH/genomes.sample/sacCer3"
  if [[ ! -d "$SAMPLE_DIR" ]]; then
    echo "ERROR: sacCer3 sample not found at $SAMPLE_DIR" >&2
    echo "       Verify CRISPOR_PATH points at a working CRISPOR clone." >&2
    exit 2
  fi

  if [[ -d "$DEST_DIR" ]] && check_layout "$DEST_DIR"; then
    echo "sacCer3 already installed at $DEST_DIR — no-op"
    exit 0
  fi

  echo "Installing sacCer3 from $SAMPLE_DIR -> $DEST_DIR"
  mkdir -p "$GENOME_DIR"
  cp -R "$SAMPLE_DIR" "$DEST_DIR"
  if check_layout "$DEST_DIR"; then
    echo "sacCer3 installed; layout verified"
    exit 0
  else
    echo "ERROR: sacCer3 install incomplete; missing files" >&2
    exit 3
  fi
fi

# ----------------------------------------------------------------------
# felCat9 / hg38 / mm39 — pre-flight + gated fetch from UCSC goldenPath
# ----------------------------------------------------------------------
case "$GENOME" in
  felCat9) URL="https://hgdownload.soe.ucsc.edu/goldenPath/felCat9/bigZips/felCat9.fa.gz"
           DESC="ucscFelCat9	Felis catus	cat	UCSC felCat9 (GCF_000181335.3)" ;;
  hg38)    URL="https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz"
           DESC="ucscHg38	Homo sapiens	human	UCSC hg38 (GRCh38.p14)" ;;
  mm39)    URL="https://hgdownload.soe.ucsc.edu/goldenPath/mm39/bigZips/mm39.fa.gz"
           DESC="ucscMm39	Mus musculus	mouse	UCSC mm39 (GRCm39)" ;;
  *)
    echo "ERROR: unsupported genome '$GENOME'" >&2
    usage
    exit 1
    ;;
esac

# Pre-flight HEAD request — capture status + Content-Length.
echo "Pre-flighting $URL"
headers="$(curl -sIL --max-time 30 "$URL")" || {
  echo "ERROR: HEAD request failed for $URL" >&2
  exit 4
}
status="$(echo "$headers" | grep -E '^HTTP/' | tail -1 | tr -d '\r\n')"
size="$(echo "$headers" | grep -i '^Content-Length:' | tail -1 | awk '{print $2}' | tr -d '\r\n')"

if [[ -z "$size" ]]; then
  echo "ERROR: could not retrieve Content-Length from $URL" >&2
  echo "       HEAD response status: $status" >&2
  exit 4
fi

# Best-effort human-readable size — pure awk, no GNU coreutils dependency.
human="$(awk -v b="$size" 'BEGIN { s=int(b); split("B KiB MiB GiB", u, " "); i=1; while (s>1024 && i<4) { s=s/1024; i=i+1 } printf "%.1f %s", s, u[i] }')"

cat <<INFO
  Status:           $status
  URL:              $URL
  Compressed size:  $size bytes ($human)
  Target:           $DEST_DIR
INFO

if [[ "${CONFIRM_DOWNLOAD:-0}" != "1" ]]; then
  cat <<INFO

Pre-flight only. Set CONFIRM_DOWNLOAD=1 to fetch + index.
Estimated wall time: ~25-40 minutes ($GENOME download + decompress + BWA-index).
This path is untested on Apple Silicon dev (Rosetta required for bundled
x86_64 binaries); the Session 8b LXC exercises the live fetch.
INFO
  exit 0
fi

# ----------------------------------------------------------------------
# Real fetch + index — exercised on the LXC during Session 8b.
# ----------------------------------------------------------------------
for tool in bwa curl gunzip; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required tool '$tool' not on PATH" >&2
    exit 5
  fi
done

# CRISPOR ships UCSC kent-tools binaries per platform under bin/<arch>.
# Pick the right subdir; we only attempt the fetch path on architectures
# where CRISPOR's bundled binaries are runnable.
arch_dir=""
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)   arch_dir="$CRISPOR_PATH/bin/Linux-x86_64" ;;
  Linux-aarch64)  arch_dir="$CRISPOR_PATH/bin/Linux-aarch64" ;;
  Darwin-x86_64)  arch_dir="$CRISPOR_PATH/bin/Darwin" ;;
  *)
    echo "ERROR: no CRISPOR bin dir for $(uname -s)-$(uname -m)" >&2
    echo "       Bundled binaries needed: faToTwoBit, twoBitInfo." >&2
    exit 5
    ;;
esac

for tool in faToTwoBit twoBitInfo; do
  if [[ ! -x "$arch_dir/$tool" ]]; then
    echo "ERROR: $tool not executable at $arch_dir/$tool" >&2
    exit 5
  fi
done

mkdir -p "$DEST_DIR"
cd "$DEST_DIR"

echo "Fetching $URL"
curl -L --progress-bar -o "$GENOME.fa.gz" "$URL"

echo "Decompressing"
gunzip -f "$GENOME.fa.gz"

echo "Building BWA index"
bwa index -p "$GENOME.fa" "$GENOME.fa"

echo "Building 2bit + sizes"
"$arch_dir/faToTwoBit" "$GENOME.fa" "$GENOME.2bit"
"$arch_dir/twoBitInfo" "$GENOME.2bit" "$GENOME.sizes"

echo "Writing minimal genomeInfo.tab"
{
  printf "#name\tdescription\tnibPath\torganism\tdefaultPos\tactive\torderKey\tgenome\tscientificName\thtmlPath\thgNearOk\thgPbOk\tsourceName\ttaxId\tserver\n"
  IFS=$'\t' read -r name org common src <<<"$DESC"
  printf "%s\t%s (UCSC)\t/gbdb/%s\t%s\tchr1:1-1000\t1\t1\t%s\t%s\t/gbdb/%s/html/description.html\t0\t0\t%s\t0\tucsc\n" \
    "$GENOME" "$src" "$GENOME" "$common" "$common" "$org" "$GENOME" "$src"
} > genomeInfo.tab

if check_layout "$DEST_DIR"; then
  echo "$GENOME installed at $DEST_DIR; layout verified"
  echo "Note: segments.bed not produced by this script — locus_class will report"
  echo "      'unknown' until segments.bed is added (deferred to Session 8b)."
  exit 0
else
  echo "ERROR: $GENOME install incomplete after fetch + index" >&2
  exit 6
fi
