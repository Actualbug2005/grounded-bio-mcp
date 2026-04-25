# Session 8b — LXC deployment on pve2, full genome indexes, Caddy + systemd, evaluation harness

> **Scope:** Production deployment of `grounded-bio-mcp` to unprivileged LXC on Proxmox VE pve2. CRISPOR install + felCat9 + hg38 + mm39 indexes on the LXC. Caddy reverse proxy with bearer auth. systemd service. Evaluation harness per spec v3 §10.4. End-to-end verification via claude.ai connector.
>
> **Pre-requisites:** Sessions 8a and 8.5 complete. Codebase is `grounded-bio-mcp` v0.3.0. 19/19 smoke green on dev machine. CRISPOR install steps documented at `docs/crispor_install.md` from Session 8a.
>
> **Spec reference:** v3.0 §9 (deployment), §10 (testing + evaluation), §11.3 (clinical disclaimer).

---

## Pre-approval decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Unprivileged LXC on pve2 | Matches existing homelab pattern; spec §9.1 |
| 2 | Resources: 4 vCPU, 6 GB RAM, 30 GB root + 80 GB data mount | Spec §9.1; CRISPOR + genome indexes need ~10 GB; rest is headroom |
| 3 | Hostname: `grounded-bio-mcp`; DNS: `grounded-bio-mcp.devlin.lan` | Project identity; user's existing local-zone pattern |
| 4 | Caddy reverse proxy with bearer auth + `tls internal` | Spec §9.3; matches homelab self-signed-CA pattern |
| 5 | systemd hardening per spec §9.2 | `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `MemoryMax=4G` |
| 6 | Three genome downloads with download-gate per fetch | felCat9 (~1 GB), hg38 (~3.2 GB), mm39 (~2.8 GB); spec §9.1 step 8 |
| 7 | Evaluation harness: questions 1-5 + Q10 active in this session; Q6-9 deferred to Phase 4 sessions | v3 §10.4; Phase 4 tools not yet implemented; partial harness validates deployment, full harness lands as Phase 4 ships |
| 8 | Smoke test extended to run against deployed endpoint | Production verification; cron-scheduled post-soak per v3 §12 (30-day soak phase) |
| 9 | claude.ai connector configured by user via the UI; Claude provides exact URL + token instructions, user completes the configuration | Per `<user_privacy>` constraints — Claude doesn't enter credentials |

---

## Pre-work checklist

Before starting:

1. Confirm Sessions 8a + 8.5 complete; codebase is `grounded-bio-mcp` v0.3.0 with 19 tools live and Apache-2.0 licensed
2. Confirm pve2 has spare resources: 4 vCPU + 6 GB RAM + 110 GB storage available
3. Confirm vmbr0 has a free static IP in the homelab range
4. Confirm DNS server (UCG Max) can host the new hostname
5. Confirm you have shell access to pve2 (root or sudo)
6. Confirm the dev-machine genome download for felCat9 worked cleanly in 8a; the LXC fetch will follow the same pattern
7. Run smoke test on dev machine — must be 19/19 green pre-deployment
8. Verify `MCP_AUTH_TOKEN` strategy: generate via `openssl rand -hex 32` during deployment; surface to user once for claude.ai connector config

---

## Scope

### A. LXC provisioning (~30 minutes)

```bash
# On pve2:
pct create <CTID> /var/lib/vz/template/cache/debian-13-standard_*_amd64.tar.zst \
  --hostname grounded-bio-mcp \
  --cores 4 --memory 6144 --swap 1024 \
  --rootfs local-zfs:30 \
  --net0 name=eth0,bridge=vmbr0,ip=<IP>/24,gw=<GW> \
  --nameserver <DNS> --searchdomain devlin.lan \
  --features nesting=0,keyctl=1 \
  --unprivileged 1 \
  --onboot 1 --start 1
```

Mount data volume:
```bash
pvesm alloc local-zfs <CTID> vm-<CTID>-data 80G
pct set <CTID> -mp0 /mnt/pve/local-zfs/vm-<CTID>-data,mp=/var/lib/grounded_bio_mcp,backup=1
```

(Exact storage backend name depends on the user's pve2 setup; verify in pre-work and adjust the prompt if needed.)

Add DNS A record `grounded-bio-mcp.devlin.lan` → static IP on UCG Max.

**Commit (in repo `docs/`):** `docs(deploy): LXC provisioning record (CTID, IP, resources)`

### B. Base packages + system user (~15 minutes)

```bash
# On the LXC:
apt update && apt full-upgrade -y
apt install -y build-essential curl git python3.13-venv python3.13-dev \
  python3.11-venv python3.11-dev \
  bwa caddy gnupg ca-certificates

useradd --system --create-home --home-dir /opt/grounded_bio_mcp \
  --shell /bin/bash grounded-bio-mcp

mkdir -p /var/lib/grounded_bio_mcp/{genomes,cache,logs}
chown -R grounded-bio-mcp:grounded-bio-mcp /var/lib/grounded_bio_mcp
```

### C. Application install (~30 minutes)

```bash
sudo -u grounded-bio-mcp -i
cd /opt/grounded_bio_mcp
git clone https://github.com/<user>/grounded-bio-mcp.git app
cd app
python3.13 -m venv ../venv
../venv/bin/pip install -e ".[deploy]"
```

If `[deploy]` extra not yet defined in `pyproject.toml`, define it during this session — minimal: production runtime deps only, no test deps.

### D. CRISPOR install on LXC (~30 minutes + genome downloads)

Replicate `docs/crispor_install.md` from Session 8a. Adjust for Debian 13 paths (no homebrew; bwa is already installed via apt above).

```bash
git clone https://github.com/maximilianh/crisporWebsite /opt/crispor
cd /opt/crispor
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt
chown -R grounded-bio-mcp:grounded-bio-mcp /opt/crispor
```

### E. Genome index downloads — **THREE DOWNLOAD GATES**

Surface for user approval **one at a time**, in size order. URLs + compressed sizes verified live during Session 8a (2026-04-25); the legacy `crispor.tefor.net/genomes/*.tar.gz` bundles are dead, so the script (`scripts/fetch_genome.sh`) fetches UCSC's goldenPath FASTA and runs `bwa index` + `faToTwoBit` + `twoBitInfo` locally rather than expecting a pre-built bundle.

The pre-flight pass (script run without `CONFIRM_DOWNLOAD=1`) prints URL + Content-Length + target path; the gated pass (`CONFIRM_DOWNLOAD=1`) actually fetches.

#### E.1 felCat9 (smallest, sanity check)

- Source: `https://hgdownload.soe.ucsc.edu/goldenPath/felCat9/bigZips/felCat9.fa.gz`
- Compressed: **812 014 328 bytes (774.4 MiB)** — verified via HEAD request 2026-04-25
- Target: `/var/lib/grounded_bio_mcp/genomes/felCat9/`
- Estimated wall time: ~25 min (download + decompress + BWA-index)

Wait for user approval. Then `CONFIRM_DOWNLOAD=1 scripts/fetch_genome.sh felCat9`. Record provenance JSON in target dir if upstream publishes a checksum (UCSC does not at the bigZips path; capture SHA256 of the downloaded archive instead).

#### E.2 hg38 (largest)

- Source: `https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz`
- Compressed: **983 659 424 bytes (938.1 MiB)** — verified 2026-04-25
- Target: `/var/lib/grounded_bio_mcp/genomes/hg38/`
- Estimated wall time: ~35 min

Wait for user approval. `CONFIRM_DOWNLOAD=1 scripts/fetch_genome.sh hg38`. Provenance.

#### E.3 mm39

- Source: `https://hgdownload.soe.ucsc.edu/goldenPath/mm39/bigZips/mm39.fa.gz`
- Compressed: **870 543 764 bytes (830.2 MiB)** — verified 2026-04-25
- Target: `/var/lib/grounded_bio_mcp/genomes/mm39/`
- Estimated wall time: ~30 min

Wait for user approval. `CONFIRM_DOWNLOAD=1 scripts/fetch_genome.sh mm39`. Provenance.

**Total compressed:** 2.5 GiB across three genomes (smaller than the original prompt's 7 GB estimate — those numbers were for the deprecated `tefor.net` pre-built bundles which included BWA indexes; UCSC's FASTA-only files are smaller because indexing happens locally).
**Total disk after extraction + BWA-index:** ~20 GB. Fits in the 80 GB data mount.

#### E.4 segments.bed (locus_class classification — open question)

`scripts/fetch_genome.sh` does **not** produce `<genome>.segments.bed`. Without it, `bio_design_grna`'s `locus_class` field falls back to `"unknown"` for all off-targets in hg38/mm39/felCat9 — losing the CDS/intron/intergenic surface §4.7 promises. (sacCer3 ships its own segments.bed bundled with CRISPOR, so the yeast smoke is unaffected.)

Three forward paths (decide during 8b pre-work):

1. **Patch and run CRISPOR's deprecated `crisprAddGenome.old`** — Python 2-era webserver helper at `/opt/crispor/tools/usrLocalBin/crisprAddGenome.old`. Hardcoded paths to `/var/www/crispor/...` need surgery. Fragile but produces an authoritative segments.bed.
2. **Build segments.bed manually from a GFF** — `awk` / `bedtools` over RefSeq GFF using CRISPOR's segment-prefix conventions (`ig:G1|G2`, `ex:GENE`, `in:GENE`). Most work but most maintainable.
3. **Accept `locus_class="unknown"` for hg38 / mm39** — defer segments.bed to a follow-up session. Q10 evaluation harness still works structurally but the off-target classification surface is degraded.

Strong recommendation: option 2. Worth one focused session post-8b before declaring deployment "done"; option 3 leaves an honest gap in the spec output.

**Commit (after all three are verified working with CRISPOR):** `chore(deploy): genome indexes on pve2 LXC — felCat9 + hg38 + mm39 with provenance`

### F. Configuration — `/etc/grounded_bio_mcp/env` (~10 minutes)

```bash
mkdir -p /etc/grounded_bio_mcp
cat > /etc/grounded_bio_mcp/env <<EOF
EBI_EMAIL=<user's email>
NCBI_API_KEY=<optional, if user has one>
STRING_USER_EMAIL=<user's email>
MCP_AUTH_TOKEN=<generated via: openssl rand -hex 32>
MCP_HOST=127.0.0.1
MCP_PORT=8080
MCP_TRANSPORT=http
GROUNDED_BIO_MCP_DATA_DIR=/var/lib/grounded_bio_mcp
GROUNDED_BIO_MCP_GENOMES_DIR=/var/lib/grounded_bio_mcp/genomes
GROUNDED_BIO_MCP_CRISPOR_PATH=/opt/crispor
GROUNDED_BIO_MCP_CRISPOR_VENV=/opt/crispor/venv
EOF
chmod 640 /etc/grounded_bio_mcp/env
chown root:grounded-bio-mcp /etc/grounded_bio_mcp/env
```

`MCP_AUTH_TOKEN` value displayed once on stdout for user to copy into claude.ai connector config; the user is responsible for safekeeping. Spec §9.1 step 9.

### G. systemd service (~15 minutes)

Install service file per spec §9.2:

```bash
cat > /etc/systemd/system/grounded-bio-mcp.service <<'EOF'
[Unit]
Description=grounded-bio-mcp server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=grounded-bio-mcp
Group=grounded-bio-mcp
WorkingDirectory=/opt/grounded_bio_mcp/app
EnvironmentFile=/etc/grounded_bio_mcp/env
ExecStart=/opt/grounded_bio_mcp/venv/bin/python -m grounded_bio_mcp.server
Restart=on-failure
RestartSec=5

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/grounded_bio_mcp /tmp
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictRealtime=yes
LockPersonality=yes
SystemCallArchitectures=native
MemoryMax=4G
TasksMax=512

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now grounded-bio-mcp.service
systemctl status grounded-bio-mcp.service
journalctl -u grounded-bio-mcp.service -n 50
```

Verify the service starts cleanly. The `_forbid_public_bind` check in `config.Settings` should pass (binding 127.0.0.1).

### H. Caddy reverse proxy (~20 minutes)

Caddy config per spec §9.3:

```bash
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<'EOF'
{
    # Use Caddy's local CA for the homelab zone
}

grounded-bio-mcp.devlin.lan {
    tls internal

    @authorized header Authorization "Bearer {env.MCP_AUTH_TOKEN}"

    handle @authorized {
        reverse_proxy 127.0.0.1:8080 {
            transport http {
                read_buffer 64KB
            }
        }
    }

    handle {
        respond "Unauthorized" 401
    }

    log {
        output file /var/log/caddy/grounded-bio-mcp.log {
            roll_size 100MB
            roll_keep 10
        }
        format json
    }
}
EOF

# Caddy reads env vars via systemd EnvironmentFile, not Caddyfile
mkdir -p /etc/caddy
cat > /etc/caddy/env <<EOF
MCP_AUTH_TOKEN=$(grep ^MCP_AUTH_TOKEN /etc/grounded_bio_mcp/env | cut -d= -f2)
EOF
chmod 640 /etc/caddy/env
chown root:caddy /etc/caddy/env

# Patch Caddy systemd unit to read EnvironmentFile
mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/env.conf <<'EOF'
[Service]
EnvironmentFile=/etc/caddy/env
EOF

systemctl daemon-reload
systemctl restart caddy
systemctl status caddy
```

Verify Caddy serves on 443 with the local-CA cert. Verify bearer-token gate:

```bash
# Should 401:
curl -k https://grounded-bio-mcp.devlin.lan/

# Should 200 (with valid token):
curl -k -H "Authorization: Bearer $TOKEN" https://grounded-bio-mcp.devlin.lan/mcp
```

### I. claude.ai connector configuration (user action)

Provide the user with:

- **URL:** `https://grounded-bio-mcp.devlin.lan/mcp`
- **Header:** `Authorization: Bearer <MCP_AUTH_TOKEN>` (token displayed once during setup)
- **Transport:** Streamable HTTP

User configures via Settings → Connectors → Add custom connector. Per `<user_privacy>` constraints, Claude doesn't enter the credential — surface the values, let the user paste into the UI.

Verify connection: in claude.ai, list available tools; should see all 19 `bio_*` tools.

### J. Evaluation harness (~2 hours)

Implement per spec v3 §10.4 — partial harness covering the six tools live as of Session 8b:

| Q# | Tool | Question | Expected basis |
|---|---|---|---|
| 1 | `bio_fetch_sequence` | "What is the length and first 30 nt of NM_001301717?" | CCR7 mRNA; length and 5' from NCBI |
| 2 | `bio_fetch_uniprot` | "What is the signal peptide length and chain start of UniProt Q08758 (CD5L)?" | UniProt features for human CD5L |
| 3 | `bio_fetch_pdb` | "What is the resolution of PDB 1CRN?" | 1.5 Å (errata-corrected) |
| 4 | `bio_fetch_alphafold` | "What is the median pLDDT of the AlphaFold model for human BRCA1 (P38398)?" | AlphaFold DB model stats |
| 5 | `bio_fetch_paper_fulltext` | "Does the Sugisawa 2016 paper (PMC5071876) name specific residues mediating the IgM-CD5L interaction?" | Negative-verification: paper does not |
| 10 | `bio_design_grna` | "Design 5 SpCas9 guides for human BRCA1 exon 11; report top guide's CFD specificity and 0-1 mismatch off-target counts." | Real CRISPOR run on hg38 |

Implementation: `scripts/evaluation_harness.py` runs each question through the deployed endpoint, captures the response, validates against expected-answer assertions. Returns pass/fail per question + summary.

Each question is **self-contained and verifiable** — no LLM-judge subjectivity. Either the response contains the verifiable fact or it doesn't.

**Commits:**

1. `feat(eval): evaluation harness scaffold + question 1 (fetch_sequence/CCR7)`
2. `feat(eval): questions 2-5 (uniprot/pdb/alphafold/fulltext)`
3. `feat(eval): question 10 (design_grna against deployed CRISPOR)`
4. `docs(eval): evaluation-harness output recorded as deployment acceptance test`

### K. Cron-scheduled smoke test (~15 minutes)

For ongoing health monitoring during the 30-day soak phase:

```bash
sudo -u grounded-bio-mcp -i
crontab -e
# Add:
0 6 * * * /opt/grounded_bio_mcp/venv/bin/python /opt/grounded_bio_mcp/app/scripts/smoke_test_phase1a.py >> /var/lib/grounded_bio_mcp/logs/smoke.log 2>&1
```

**Commit:** `chore(deploy): cron-scheduled daily smoke test`

### L. Update README with deployment notes

Add a "Deployment" section pointing to spec §9 and noting the pve2 LXC is the reference deployment. Update tool count (19 currently) and indicate Phase 4 is forthcoming per spec v3 §5 + §12.

---

## Acceptance criteria

- [ ] LXC provisioned on pve2 with correct resources + DNS record
- [ ] Base packages + system user installed
- [ ] Application installed in venv; `grounded_bio_mcp.server` imports cleanly
- [ ] CRISPOR installed at `/opt/crispor` with bwa system dep
- [ ] Three genome indexes downloaded with download-gate per fetch + provenance JSON
- [ ] `/etc/grounded_bio_mcp/env` configured with all required env vars
- [ ] `grounded-bio-mcp.service` running cleanly, starts on boot
- [ ] Caddy fronts the service with bearer auth + local-CA TLS
- [ ] Bearer-auth gate verified via curl (401 without; 200 with)
- [ ] User configures claude.ai connector; tools list visible (19 tools)
- [ ] Evaluation harness runs against deployed endpoint; partial harness (Q1-5, Q10) passes
- [ ] Smoke test runs against deployed endpoint and is 19/19 green
- [ ] Cron-scheduled smoke test enabled
- [ ] README updated with deployment notes + tool count
- [ ] Memory entries: deployment record, evaluation-harness pattern, genome-index provenance pattern
- [ ] No regressions in dev-machine smoke test

---

## Failure modes to watch for

- **CRISPOR Python 3.11 venv** — if Trixie's `python3.11` package is missing or behaves differently, fall back to compiling from source or use `pyenv`. Document whichever approach works.
- **Caddy local CA + claude.ai** — claude.ai may not trust the local CA. If the connector fails with TLS errors, options are:
  - Install Caddy's local-CA root cert into the system trust store on pve2 + propagate via UCG Max if possible (limited; claude.ai runs in cloud, not on the LAN)
  - Use a real cert from Let's Encrypt with DNS-01 challenge if the homelab zone is internet-resolvable
  - Use a tunnel (Cloudflare Tunnel, Tailscale serve) to expose the endpoint with a real cert
- **Bearer-auth header forwarding** — verify Caddy doesn't strip the `Authorization` header during proxy; the upstream MCP server doesn't need it (auth is at the proxy) but some MCP client implementations expect headers to round-trip.
- **systemd ProtectSystem=strict** — `ReadWritePaths=/var/lib/grounded_bio_mcp /tmp` must include any other write paths the application uses (logs, cache, etc.). Verify by tail-following logs after startup.
- **Genome download size estimates may be stale** — actual sizes may have grown; surface the real size from `Content-Length` headers before approval.
- **Q10 in evaluation harness depends on hg38 index** — if hg38 download or extraction fails, Q10 fails; this is correct behaviour (test exposes infrastructure gaps), but flag clearly to user.

---

## Out of scope (deferred to subsequent sessions)

- 30-day production soak (no new work; observe failure modes; implicit "session" is calendar-based)
- Phase 4 tool implementations (Sessions 9-15)
- `bio_predict_stability` wedge tool (Session 9)
- mkdocs site setup (Session 15+)
- BioContextAI registry submission (post-Session 15)

---

## Pre-work report expected

1. pve2 resource availability check (CPU + RAM + storage)
2. DNS / IP plan (which IP, hostname A record where)
3. Caddy local-CA vs real-cert decision (any homelab ACME setup already in place?)
4. claude.ai connector connectivity test plan (reach the LXC from claude.ai cloud — what tunnel / ACME approach if local CA won't work?)
5. Genome URL verification (URLs still valid? sizes confirmed?)
6. Any spec errata or deployment-prompt errata noticed during pre-work

---

## Notes

This session is operationally complex but mostly mechanical given the spec is detailed and Sessions 8a + 8.5 have de-risked the application + identity work. The biggest unknown is the Caddy + claude.ai TLS path; if local CA doesn't work, the alternative tunnel options are well-known patterns and any of the three should be viable.

The evaluation harness is the most valuable artefact landing in this session — it's the regression-detection mechanism for the 30-day soak and beyond. Implement it carefully; the questions should fail loudly if anything regresses, not silently pass on partial output.
