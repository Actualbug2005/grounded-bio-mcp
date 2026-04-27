# Session 8b — LXC deployment on pve2, full genome indexes, Cloudflare Tunnel + Caddy + systemd, evaluation harness

> **Scope:** Production deployment of `grounded-bio-mcp` to unprivileged LXC on Proxmox VE pve2. CRISPOR install + felCat9 + hg38 + mm39 indexes on the LXC. Cloudflare Tunnel for public exposure. Caddy reverse proxy with bearer auth. systemd service. Evaluation harness per spec v3 §10.4. End-to-end verification via claude.ai connector.
>
> **Pre-requisites:** Sessions 8a and 8.5 complete. Codebase is `grounded-bio-mcp` v0.3.0. 19/19 smoke green on dev machine. CRISPOR install steps documented at `docs/crispor_install.md` from Session 8a.
>
> **Spec reference:** v3.0 §9 (deployment), §10 (testing + evaluation), §11.3 (clinical disclaimer).

---

## Execution notes (from initial run, 2026-04-26)

This prompt was originally written before the deployment was attempted. The initial-run execution surfaced 17 substantive corrections — some are environment-specific decisions, others are factual corrections to assumptions in the original prompt. The corrections are integrated inline below; this section captures the decisions and reasons.

### Environment-specific decisions (Devlin's pve2 homelab)

| Decision | Value | Reasoning |
|---|---|---|
| Storage backend | `local-lvm` (LVM-thin) | Pre-existing pve2 layout |
| Network VLAN | VLAN 1 native (`10.2.1.0/24`) | Originally considered VPN-egress VLAN 3 (`10.2.3.0/24`); rejected because VPN egress hurts upstream API latency, exposes shared exit IPs to upstream rate-limit accounting, and weakens the project's provenance / auditability story |
| LXC static IP | `10.2.1.12/24` | Free address in user's static-service range |
| Gateway / DNS | `10.2.1.1` (UCG Max forwarding to Cloudflare 1.1.1.1) | Standard UniFi default |
| IPv6 | IPv4-only on `eth0` | SLAAC still auto-configures IPv6 link-local for free; no harm |
| TLS / public exposure | Cloudflare Tunnel via existing Cloudflare Access infra | User already runs Cloudflare; tunnels avoid port-forwards entirely |
| Public hostname | `grounded-bio-mcp.harkernetwork.com` | User's owned domain |
| Auth model | MCP bearer token only at Caddy (no Cloudflare Access in front) | Cloudflare Access requires `CF-Access-*` headers that claude.ai's connector UI cannot send; bypassing Access for this hostname is the only way to allow MCP traffic |
| Token-handling model | Soft-managed public (private token, share on request) | Public-reachable for utility (BioContextAI registry submission, contributors testing); manageable abuse surface; can publish later if desired |
| Cloudflare rate limits | 150 req/min per hostname (free tier) | Tight enough for soft-managed scale; Bot Fight Mode + Browser Integrity Check **OFF** for this hostname (would block claude.ai's automated traffic) |
| Logging | Caddy local logs only (no Logpush) | Cloudflare Logpush is Business tier (~£200/mo); for soft-managed scale, Caddy + journalctl is sufficient |

### Factual corrections to original prompt

These are mistakes / outdated assumptions in the prompt as originally written, corrected in the body below:

1. **LXC features must include `nesting=1`** — Debian 13 ships systemd 257, which can't initialise pid 1 in unprivileged containers without nesting. Symptom: container appears started but console is unresponsive. Set `nesting=1` (and `keyctl=1`) at LXC creation time.

2. **VLAN Tag in Proxmox network config should be blank**, not the network's VLAN ID — UniFi treats VLAN 1 as untagged/native on trunk ports; tagging frames with VLAN 1 from Proxmox causes the switch to drop them. Symptom: gateway unreachable, `Destination Host Unreachable` on every IPv4 ping. Leave the VLAN Tag field blank for native-VLAN traffic.

3. **Path convention is hyphenated**, not underscored — `/var/lib/grounded-bio-mcp/`, `/etc/grounded-bio-mcp/env`, system user `grounded-bio-mcp`. The Python package import path `grounded_bio_mcp` (underscores) is the only underscored variant. The original prompt mixed conventions; the v3 spec §9.1 step 7 also has `grounded_bio_mcp` mixed in — both are erroneous and corrected here.

4. **Python 3.11 is not in Debian 13 apt repos.** Trixie ships Python 3.13 only; CRISPOR requires 3.11 (`cgi` module removed in 3.13). Use `uv` to manage the Python 3.11 install. Original prompt's `apt install python3.11-venv python3.11-dev` fails with "Unable to locate package".

5. **`sudo` is not installed by default** on minimal Debian LXC images. Use `su -` to switch users, or install sudo separately if preferred. Either install sudo (`apt install sudo`) or rewrite all `sudo -u <user> -i` commands to `su - <user>`.

6. **`uv` install scope: system-wide via `UV_INSTALL_DIR=/usr/local/bin`**. Default install puts uv in `~/.local/bin/` per-user, which means `grounded-bio-mcp` user can't see root's uv. System-wide install via env var `UV_INSTALL_DIR=/usr/local/bin` makes it visible to all users.

7. **`[deploy]` extra missing from `pyproject.toml`** as of v0.3.0. Add as empty array (placeholder for future monitoring/metrics deps); commit on dev, push, pull on LXC. Lets `uv pip install -e ".[deploy]"` resolve cleanly without forcing immediate dep choices.

8. **Caddy local-CA approach (`tls internal`) does not work with claude.ai cloud** — claude.ai cannot trust local CAs without a workflow Anthropic doesn't expose. Cloudflare Tunnel is the only option that gives a real publicly-trusted cert without homelab port-forwarding. Original prompt §H Caddy `tls internal` block is wrong for this use case.

9. **LVM thin pool autoextend hygiene**: pve2's default LVM config has `thin_pool_autoextend_threshold = 100` (effectively disabled). Set to 80 (and `_percent = 20`) before any genome downloads. With overcommitted thin pools, no autoextend = first overcommitted write fails catastrophically.

10. **Cloudflare Tunnel changes the deployment topology** — no port-forwarding on UCG Max, no inbound 443 to the LXC, no DNS A record (Cloudflare handles DNS as CNAME-to-tunnel automatically). The Caddy stanza serves on `127.0.0.1:8080` only; cloudflared connects out to Cloudflare and routes inbound traffic through the established tunnel. Architecturally cleaner than the original Caddy-with-`tls internal` design.

### Errata to v3 spec captured during execution

For incorporation into v3.1 / v3.0.1 errata:

- §9.1 step 7 mixes `/var/lib/grounded_bio_mcp/` (underscores) and `grounded-bio-mcp` (hyphens) — should be hyphens consistently
- §9.4 `tls internal` recommendation is unworkable with claude.ai cloud
- §9.2 systemd service file `User=grounded-bio-mcp` is correct as written
- §11.4 ViennaRNA "GPL-bound" wording was already fixed during 8.5 — confirmed correct in v0.3.0

---

## Pre-approval decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Unprivileged LXC on pve2 with `nesting=1` + `keyctl=1` | Spec §9.1 + Debian 13 systemd 257 requirement |
| 2 | Resources: 4 vCPU, 6 GB RAM, 30 GB root + 80 GB data mount on local-lvm | Spec §9.1; CRISPOR + genome indexes need ~10 GB; rest is headroom |
| 3 | Hostname: `grounded-bio-mcp`; public DNS: `grounded-bio-mcp.harkernetwork.com` via Cloudflare Tunnel | Project identity; user's domain |
| 4 | Cloudflare Tunnel for public exposure; bearer-auth at Caddy as auth boundary | Replaces original `tls internal` approach which doesn't work with claude.ai |
| 5 | systemd hardening per spec §9.2 | `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `MemoryMax=4G` |
| 6 | Three genome downloads with download-gate per fetch | felCat9 (~775 MiB compressed), hg38 (~938 MiB), mm39 (~830 MiB); spec §9.1 step 8 |
| 7 | Evaluation harness Q1-5 + Q10 active in this session; Q6-9 deferred | v3 §10.4; Phase 4 tools not yet implemented |
| 8 | Smoke test extended to run against deployed endpoint | Production verification; cron-scheduled post-soak per v3 §12 |
| 9 | claude.ai connector configured by user via the UI; Claude provides exact URL + token instructions, user completes the configuration | Per `<user_privacy>` constraints — Claude doesn't enter credentials |
| 10 | uv installed system-wide on LXC at `/usr/local/bin/uv` via `UV_INSTALL_DIR` env var | Visible to all users, no per-user duplication |

---

## Pre-work checklist

Before starting:

1. Confirm Sessions 8a + 8.5 complete; codebase is `grounded-bio-mcp` v0.3.0 with 19 tools live and Apache-2.0 licensed
2. Confirm pve2 has spare resources: 4 vCPU + 6 GB RAM + 110 GB storage available on local-lvm
3. Confirm vmbr0 is VLAN-aware on pve2; identify the trunk port's native VLAN
4. Confirm a free static IP on the main VLAN
5. Confirm Cloudflare account is active and the relevant zone is managed
6. Check LVM thin pool actual usage (`lvs -a` on pve2) — if `Data%` > 75%, free space before proceeding
7. Run smoke test on dev machine — must be 18 passed + 1 loud-skipped of 19 (CRISPOR Rosetta gate)
8. Verify `MCP_AUTH_TOKEN` strategy: generate via `openssl rand -hex 32` during deployment; surface to user once for claude.ai connector config

---

## Scope

### A. LXC provisioning (~30 minutes)

**Via Proxmox UI** (cleaner than CLI for first-time provisioning):

1. **General** tab:
   - Hostname: `grounded-bio-mcp`
   - Unprivileged container: ✅ checked
   - Set strong root password + paste dev machine SSH public key

2. **Template** tab: `debian-13-standard_*_amd64.tar.zst`

3. **Disks** tab: storage `local-lvm`, size `30 GB`

4. **CPU** tab: 4 cores

5. **Memory** tab: 6144 MiB RAM, 1024 MiB swap

6. **Network** tab:
   - Bridge: `vmbr0`
   - **VLAN Tag: blank** (native VLAN on UniFi trunk)
   - IPv4: Static, gateway as appropriate for the VLAN
   - IPv6: Static (no address) — IPv4-only operation

7. **DNS** tab: nameserver as appropriate (e.g. UCG Max LAN address)

8. **Confirm** tab: **uncheck "Start after created"**

After creation, before first boot:
- **Resources** → Add Mount Point: storage `local-lvm`, size 80 GiB, path `/var/lib/grounded-bio-mcp` (hyphens, not underscores)
- **Options** → Features → ✅ keyctl, ✅ **nesting** (required for Debian 13 systemd 257)

Then start.

**LVM thin pool hygiene** — once on pve2 SSH (before genome downloads):

```bash
# Append autoextend threshold/percent to /etc/lvm/lvm.conf
cat >> /etc/lvm/lvm.conf << 'EOF'

# grounded-bio-mcp deployment hygiene
activation/thin_pool_autoextend_threshold = 80
activation/thin_pool_autoextend_percent = 20
EOF

# Confirm dmeventd monitoring is enabled
systemctl status lvm2-monitor.service  # should be active (exited)
```

**Commit (in repo `docs/`):** `docs(deploy): LXC provisioning record (CTID, IP, resources, LVM hygiene)`

### B. Initial network + base packages (~20 minutes)

After first boot, console into the LXC (`pct enter <CTID>` from pve2):

```bash
# Verify network
ip addr show eth0
ping -c 3 <gateway>
ping -c 3 1.1.1.1
ping -c 3 cloudflare.com
```

All four should succeed. Common failure modes:

- **Gateway unreachable** → VLAN Tag misconfiguration; stop LXC, set VLAN Tag to blank, restart
- **DNS fails but ping by IP works** → DNS resolver wrong; check `cat /etc/resolv.conf`

```bash
# System update
apt update
apt full-upgrade -y

# Base packages (note: NOT python3.11-venv/dev — those don't exist in Trixie)
apt install -y \
  build-essential \
  curl \
  git \
  python3.13-venv \
  python3.13-dev \
  bwa \
  caddy \
  gnupg \
  ca-certificates

# Optionally: configure needrestart for non-interactive operation
cat >> /etc/needrestart/needrestart.conf << 'EOF'

# grounded-bio-mcp: non-interactive defaults
$nrconf{restart} = 'a';
$nrconf{kernelhints} = 0;
EOF

# Install uv system-wide (visible to all users)
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Verify
which uv
uv --version
```

### C. System user + data directories (~10 minutes)

```bash
useradd \
  --system \
  --create-home \
  --home-dir /opt/grounded-bio-mcp \
  --shell /bin/bash \
  grounded-bio-mcp

# Verify
id grounded-bio-mcp
ls -ld /opt/grounded-bio-mcp

# Set ownership on data mount + create subdirs
chown -R grounded-bio-mcp:grounded-bio-mcp /var/lib/grounded-bio-mcp
mkdir -p /var/lib/grounded-bio-mcp/{genomes,cache,logs}
chown -R grounded-bio-mcp:grounded-bio-mcp /var/lib/grounded-bio-mcp
```

Note: `chown -R` will print "Permission denied" for `lost+found` — this is normal ext4 behaviour, harmless, leave alone.

### D. Application install (~20 minutes)

Switch to application user:

```bash
su - grounded-bio-mcp
```

(Note: `sudo -u grounded-bio-mcp -i` won't work — sudo not installed by default on minimal Trixie LXC.)

Clone repository:

```bash
cd /opt/grounded-bio-mcp
git clone https://github.com/Actualbug2005/grounded-bio-mcp.git app
cd app
git rev-parse HEAD  # confirm SHA at v0.3.0
git tag -l 'v*'      # confirm v0.3.0 exists
```

Verify `[deploy]` extra exists in `pyproject.toml`. If not (it didn't in v0.3.0 initial), add empty extra on dev machine, push, pull:

```toml
# In pyproject.toml [project.optional-dependencies] section
deploy = [
    # Production-only dependencies. Currently empty; reserved for future
    # monitoring/metrics/observability additions.
]
```

Commit on dev as `chore(deploy): scaffold empty [deploy] extra for production install`, push, then on LXC: `git pull origin main`.

Create venv with uv:

```bash
uv venv .venv --python 3.13
source .venv/bin/activate
uv pip install -e ".[deploy]"
```

Verify:

```bash
which python
python --version
python -c "import grounded_bio_mcp; print(grounded_bio_mcp.__file__)"
python -c "from grounded_bio_mcp.server import mcp; print(f'tools registered: {len(mcp._tool_manager._tools)}')"
```

Should show 19 tools registered.

### E. CRISPOR install on LXC (~30 minutes)

Per `docs/crispor_install.md` from Session 8a, LXC path. Adjusted for Trixie:

```bash
# (still as grounded-bio-mcp user)

# Install Python 3.11 via uv (per-user)
uv python install 3.11

# Clone CRISPOR
cd /opt/grounded-bio-mcp
git clone https://github.com/maximilianh/crisporWebsite crispor
cd crispor

# Create venv with Python 3.11
uv venv venv --python 3.11
source venv/bin/activate

# Install CRISPOR Python deps
uv pip install biopython numpy pandas scikit-learn twobitreader pytabix matplotlib xlwt

# Verify CRISPOR runs
python crispor.py --help

# Bundled sacCer3 should already be present at genomes.sample/sacCer3/
ls genomes.sample/sacCer3/

# Symlink to canonical genomes path for runtime
mkdir -p /var/lib/grounded-bio-mcp/genomes
ln -s /opt/grounded-bio-mcp/crispor/genomes.sample/sacCer3 /var/lib/grounded-bio-mcp/genomes/sacCer3
```

`bwa` is already installed system-wide via apt (step B).

### F. Genome index downloads — **THREE DOWNLOAD GATES**

Surface for user approval **one at a time**, in size order. Use `crisprAddGenome` from CRISPOR (`/opt/grounded-bio-mcp/crispor/bin/crisprAddGenome`) for index orchestration since Session 8a's pre-work showed pre-built bundles are no longer published.

#### F.1 felCat9 (smallest, sanity check)

Pre-flight:

```bash
curl -sI https://hgdownload.soe.ucsc.edu/goldenPath/felCat9/bigZips/felCat9.fa.gz | grep -i content-length
```

Expected: ~775 MiB. Then surface for approval:

- URL: `https://hgdownload.soe.ucsc.edu/goldenPath/felCat9/bigZips/felCat9.fa.gz`
- Size: real `Content-Length` from above
- Target: `/var/lib/grounded-bio-mcp/genomes/felCat9/`
- Wall time: ~25-40 min (download + decompress + BWA-index + 2bit + segments)

Wait for explicit user approval. On approval:

```bash
mkdir -p /var/lib/grounded-bio-mcp/genomes/felCat9
cd /var/lib/grounded-bio-mcp/genomes/felCat9
curl -L --progress-bar -O https://hgdownload.soe.ucsc.edu/goldenPath/felCat9/bigZips/felCat9.fa.gz
gunzip felCat9.fa.gz

# Run crisprAddGenome
/opt/grounded-bio-mcp/crispor/bin/crisprAddGenome fasta felCat9.fa --baseDir /var/lib/grounded-bio-mcp/genomes/felCat9
```

After completion, write provenance JSON with source URL, fetch timestamp, FASTA SHA256, tool version.

If felCat9 download or `crisprAddGenome` fails, **abort the chain**.

#### F.2 mm39

Same pattern with: `https://hgdownload.soe.ucsc.edu/goldenPath/mm39/bigZips/mm39.fa.gz` (~830 MiB)

#### F.3 hg38

Same pattern with: `https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz` (~938 MiB)

**Total disk used post-extraction:** ~20 GB across three genomes.

**Commit:** `chore(deploy): genome indexes on pve2 LXC — felCat9 + hg38 + mm39 with provenance`

### G. Configuration — `/etc/grounded-bio-mcp/env` (~10 minutes)

(As root, exit the grounded-bio-mcp shell first.)

```bash
mkdir -p /etc/grounded-bio-mcp
cat > /etc/grounded-bio-mcp/env <<EOF
EBI_EMAIL=<user's email>
NCBI_API_KEY=<optional, if user has one>
STRING_USER_EMAIL=<user's email>
MCP_AUTH_TOKEN=<generated via: openssl rand -hex 32>
MCP_HOST=127.0.0.1
MCP_PORT=8081
MCP_TRANSPORT=http
GROUNDED_BIO_MCP_DATA_DIR=/var/lib/grounded-bio-mcp
GROUNDED_BIO_MCP_GENOMES_DIR=/var/lib/grounded-bio-mcp/genomes
GROUNDED_BIO_MCP_CRISPOR_PATH=/opt/grounded-bio-mcp/crispor
GROUNDED_BIO_MCP_CRISPOR_VENV=/opt/grounded-bio-mcp/crispor/venv
EOF
chmod 640 /etc/grounded-bio-mcp/env
chown root:grounded-bio-mcp /etc/grounded-bio-mcp/env
```

`MCP_AUTH_TOKEN` displayed once on stdout for user to copy into claude.ai connector config.

Note: `MCP_PORT=8081` rather than 8080 — Caddy listens on 8080 and proxies to the MCP server on 8081.

### H. systemd service (~15 minutes)

Per spec §9.2:

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
WorkingDirectory=/opt/grounded-bio-mcp/app
EnvironmentFile=/etc/grounded-bio-mcp/env
ExecStart=/opt/grounded-bio-mcp/app/.venv/bin/python -m grounded_bio_mcp.server
Restart=on-failure
RestartSec=5

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/grounded-bio-mcp /tmp
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

The `_forbid_public_bind` check in `config.Settings` should pass (binding 127.0.0.1).

### I. Caddy reverse proxy (no TLS — Cloudflare Tunnel handles cert)

Cloudflare Tunnel provides the publicly-trusted cert and TLS termination at Cloudflare's edge. Caddy inside the LXC just does HTTP-with-bearer-auth on `127.0.0.1:8080`:

```bash
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<'EOF'
:8080 {
    bind 127.0.0.1

    @authorized header Authorization "Bearer {env.MCP_AUTH_TOKEN}"

    handle @authorized {
        reverse_proxy 127.0.0.1:8081 {
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
```

Caddy reads env vars via systemd `EnvironmentFile`:

```bash
cat > /etc/caddy/env <<EOF
MCP_AUTH_TOKEN=$(grep ^MCP_AUTH_TOKEN /etc/grounded-bio-mcp/env | cut -d= -f2)
EOF
chmod 640 /etc/caddy/env
chown root:caddy /etc/caddy/env

mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/env.conf <<'EOF'
[Service]
EnvironmentFile=/etc/caddy/env
EOF

systemctl daemon-reload
systemctl restart caddy
systemctl status caddy
```

Verify locally:

```bash
# Should 401:
curl http://127.0.0.1:8080/

# Should 200 (with valid token):
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/mcp
```

### J. Cloudflare Tunnel (~30 minutes)

Architecture: cloudflared on the LXC makes outbound HTTPS to Cloudflare's edge; Cloudflare receives inbound at the public hostname; routes through tunnel back to cloudflared; forwards to Caddy on `127.0.0.1:8080`; Caddy validates bearer + forwards to MCP server on `127.0.0.1:8081`.

#### J.1 Install cloudflared

```bash
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/cloudflared.list

apt update
apt install -y cloudflared
```

#### J.2 Create tunnel in Cloudflare dashboard

1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → Create a tunnel
2. Connector type: `Cloudflared`
3. Tunnel name: `grounded-bio-mcp`
4. Save → copy the install token
5. Public hostname: the chosen subdomain → Service: `HTTP` → URL: `127.0.0.1:8080`

Cloudflare auto-creates a CNAME record. No manual A record needed.

#### J.3 Install cloudflared as systemd service with the token

```bash
cloudflared service install <token-from-dashboard>
systemctl status cloudflared
journalctl -u cloudflared -n 30
```

Should show "Connection registered" entries — confirms tunnel is up.

#### J.4 Cloudflare Access bypass for this hostname

In Cloudflare dashboard → Zero Trust → Access → Applications:

- If user has existing Access policies covering `*.<domain>` patterns, add an explicit bypass / "no policy" application for the MCP hostname
- This hostname must not require Access auth (claude.ai's MCP connector can't send `CF-Access-*` headers)
- The MCP bearer token is the auth boundary

#### J.5 Cloudflare rate limiting + WAF settings

Dashboard → Security → WAF → Rate limiting rules:

- Rule: 150 requests per minute per IP, hostname matches the MCP subdomain
- Action: Block

Dashboard → Security → Settings, for the MCP hostname:

- Bot Fight Mode: **OFF**
- Browser Integrity Check: **OFF**
- Security Level: Medium (default)

These are essential — claude.ai's traffic is automated; bot challenges break MCP entirely.

### K. claude.ai connector configuration (user action)

Provide the user with:

- **URL:** `https://<public-hostname>/mcp`
- **Header:** `Authorization: Bearer <MCP_AUTH_TOKEN>`
- **Transport:** Streamable HTTP

User configures via Settings → Connectors → Add custom connector. Per `<user_privacy>` constraints, Claude doesn't enter the credential.

Verify connection: in claude.ai, list available tools; should see all 19 `bio_*` tools.

### L. Evaluation harness (~2 hours)

Per spec v3 §10.4 — partial harness covering the six tools live as of Session 8b:

| Q# | Tool | Question | Expected basis |
|---|---|---|---|
| 1 | `bio_fetch_sequence` | "What is the length and first 30 nt of NM_001301717?" | CCR7 mRNA; length and 5' from NCBI |
| 2 | `bio_fetch_uniprot` | "What is the signal peptide length and chain start of UniProt Q08758 (CD5L)?" | UniProt features for human CD5L |
| 3 | `bio_fetch_pdb` | "What is the resolution of PDB 1CRN?" | 1.5 Å (errata-corrected) |
| 4 | `bio_fetch_alphafold` | "What is the median pLDDT of the AlphaFold model for human BRCA1 (P38398)?" | AlphaFold DB model stats |
| 5 | `bio_fetch_paper_fulltext` | "Does the Sugisawa 2016 paper (PMC5071876) name specific residues mediating the IgM-CD5L interaction?" | Negative-verification: paper does not |
| 10 | `bio_design_grna` | "Design 5 SpCas9 guides for human BRCA1 exon 11; report top guide's CFD specificity and 0-1 mismatch off-target counts." | Real CRISPOR run on hg38 |

Implementation: `scripts/evaluation_harness.py` runs each question through the deployed endpoint, captures the response, validates against expected-answer assertions. Returns pass/fail per question + summary.

Each question is **self-contained and verifiable** — no LLM-judge subjectivity.

**Commits:**

1. `feat(eval): evaluation harness scaffold + question 1 (fetch_sequence/CCR7)`
2. `feat(eval): questions 2-5 (uniprot/pdb/alphafold/fulltext)`
3. `feat(eval): question 10 (design_grna against deployed CRISPOR)`
4. `docs(eval): evaluation-harness output recorded as deployment acceptance test`

### M. Cron-scheduled smoke test (~15 minutes)

For ongoing health monitoring during the 30-day soak phase:

```bash
su - grounded-bio-mcp
crontab -e
# Add:
0 6 * * * /opt/grounded-bio-mcp/app/.venv/bin/python /opt/grounded-bio-mcp/app/scripts/smoke_test_phase1a.py >> /var/lib/grounded-bio-mcp/logs/smoke.log 2>&1
```

**Commit:** `chore(deploy): cron-scheduled daily smoke test`

### N. Update README with deployment notes

Add a "Deployment" section pointing to spec §9 and noting the pve2 LXC is the reference deployment. Note the public endpoint is soft-managed — token granted on request via GitHub issue or email.

---

## Acceptance criteria

- [ ] LXC provisioned on pve2 with correct resources + nesting=1 + keyctl=1
- [ ] Network: dual-stack working, gateway reachable, DNS resolves
- [ ] LVM autoextend hygiene applied
- [ ] Base packages installed (no python3.11 apt packages — uv-managed)
- [ ] uv installed system-wide at `/usr/local/bin/uv`
- [ ] Application installed in venv via `uv pip install -e ".[deploy]"`; 19 tools registered
- [ ] CRISPOR installed at `/opt/grounded-bio-mcp/crispor` with bwa system dep
- [ ] Three genome indexes downloaded with download-gate per fetch + provenance JSON
- [ ] `/etc/grounded-bio-mcp/env` configured with all required env vars (including STRING_USER_EMAIL populated)
- [ ] `grounded-bio-mcp.service` running cleanly, starts on boot
- [ ] Caddy fronts the service on `127.0.0.1:8080` with bearer auth
- [ ] Cloudflare Tunnel established; `cloudflared.service` running
- [ ] Public hostname resolves via Cloudflare CNAME
- [ ] Cloudflare rate limit (150/min) + Bot Fight OFF + Browser Integrity OFF configured
- [ ] Bearer-auth gate verified: 401 without token, 200 with token
- [ ] User configures claude.ai connector; tools list visible (19 tools)
- [ ] Evaluation harness runs against deployed endpoint; partial harness (Q1-5, Q10) passes
- [ ] Smoke test runs against deployed endpoint and is 19/19 green (CRISPOR live on x86_64 Linux)
- [ ] Cron-scheduled smoke test enabled
- [ ] README updated with deployment notes
- [ ] Memory entries: deployment record, evaluation-harness pattern, genome-index provenance pattern, cloudflared pattern
- [ ] No regressions in dev-machine smoke test

---

## Failure modes to watch for

- **VLAN tagging mismatch on Proxmox bridge** → "Destination Host Unreachable" from LXC. Set Network tab VLAN Tag to blank for native VLAN.
- **Console unresponsive after first start** → `nesting=1` not set. Stop LXC, set in Options → Features, restart.
- **Python 3.11 apt install fails** → Trixie doesn't have python3.11 in repos. Use `uv python install 3.11` instead.
- **`sudo: command not found`** → minimal LXC, no sudo. Use `su -` or `apt install sudo`.
- **uv shadowed warning during install** → another uv exists on PATH; remove the older copy.
- **`[deploy]` extra not found** → not yet defined in pyproject.toml; add empty array on dev, push, pull on LXC.
- **`crisprAddGenome` failure on LXC** — Session 8a deferred this entirely to LXC because dev (Apple Silicon) couldn't run it; this session is its first live exercise. Failure modes well-bounded but real.
- **Cloudflare Tunnel + Cloudflare Access conflict** — if user has org-level Access policy covering the parent zone, the MCP hostname needs an explicit bypass.
- **Bot Fight Mode enabled by default at zone level** — if previously enabled, MCP traffic from claude.ai gets challenged. Must turn OFF for this hostname.
- **Q10 in evaluation harness depends on hg38 index** — if hg38 download or `crisprAddGenome` fails, Q10 fails. Correct behaviour (test exposes infrastructure gaps), but flag clearly to user.

---

## Out of scope (deferred to subsequent sessions)

- 30-day production soak (no new work; observe failure modes; calendar-based)
- Phase 4 tool implementations (Sessions 9-15)
- `bio_predict_stability` wedge tool (Session 9)
- mkdocs site setup (Session 15+)
- BioContextAI registry submission (post-Session 15)
- `segments.bed` build for non-sacCer3 genomes — currently `locus_class="unknown"` for hg38/mm39/felCat9; three forward paths captured but deferred

---

## Pre-work report expected

1. pve2 resource availability check (CPU + RAM + storage on local-lvm)
2. LVM thin pool actual usage check (`lvs -a` Data%)
3. DNS / IP plan
4. Cloudflare Tunnel readiness (account active, zone managed, any existing Access policies that need bypass for the new hostname)
5. claude.ai connector connectivity test plan
6. Genome URL verification (URLs still valid? sizes confirmed via `curl -sI`?)
7. `[deploy]` extra confirmation in pyproject.toml — exists or needs adding?
8. Any spec errata or deployment-prompt errata noticed during pre-work

---

## Notes

This session is operationally complex but mostly mechanical given the spec is detailed and the corrections from initial-run execution are now baked in. The biggest remaining unknown is the Cloudflare Tunnel + Access interaction with claude.ai's MCP connector — specifically whether the bypass-Access pattern works as expected on first try.

The evaluation harness is the most valuable artefact landing in this session — it's the regression-detection mechanism for the 30-day soak and beyond. Implement it carefully; the questions should fail loudly if anything regresses, not silently pass on partial output.

When this prompt is followed end-to-end, the deployment should land in 4-6 hours of focused work assuming no surprises.
