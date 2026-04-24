# bioinformatics-mcp

A Model Context Protocol server that grounds molecular-biology answers in real
fetches from primary databases — NCBI, UniProt, EBI, Ensembl, AlphaFold DB,
RCSB PDB, ChEMBL, PubChem, Europe PMC, Reactome, STRING — instead of letting a
model pattern-match from training data.

**Status:** Phase 1 scaffolding. Shared infrastructure is in; no tools are
implemented yet. See [`bioinformatics-mcp-spec.md`](./bioinformatics-mcp-spec.md)
for the full specification.

---

## Quick start (local development)

Prerequisites: **Python ≥ 3.11**, a C toolchain (for ViennaRNA), and `libxml2` /
`libxslt` headers (for `lxml`).

```bash
# Clone and enter
cd /path/to/bioinformatics

# Create an isolated environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install the package and dev extras
pip install -e ".[dev]"

# Copy the environment template and fill it in
cp .env.example .env
$EDITOR .env

# Run the unit tests (no network needed)
pytest

# Run integration tests against live upstream APIs (opt-in)
RUN_INTEGRATION=1 pytest -m integration
```

Once tools are implemented, the server is launched via:

```bash
bioinformatics-mcp        # streamable-HTTP on MCP_BIND_HOST:MCP_BIND_PORT
```

or via the FastMCP CLI for stdio / MCP Inspector testing:

```bash
fastmcp dev src/bioinformatics_mcp/server.py
```

---

## Project layout

See spec §5 for the canonical tree. Top-level overview:

```
src/bioinformatics_mcp/
  server.py          # FastMCP app + tool registration (TODO)
  config.py          # env var loading via pydantic-settings
  clients/           # one module per upstream API
  tools/             # one module per `bio_{action}_{resource}` tool
  models/            # shared Pydantic input/output schemas
  utils/             # rate limiting, error helpers, formatting
scripts/             # genome fetch, health check, evaluation runner
tests/               # pytest suites mirroring src/ layout
deploy/              # systemd unit, Caddyfile, LXC provisioning
eval/                # evaluation.xml for mcp-builder style Q/A runs
```

---

## Deployment

Production target is a **Debian 13 (Trixie) LXC** on Proxmox (pve2) behind a
Caddy reverse proxy. See spec §9 for the full provisioning recipe. Summary:

1. Unprivileged LXC, 4 vCPU / 6 GB RAM / 30 GB root + 80 GB `/var/lib/bioinformatics_mcp` mount.
2. Service runs under the `bio-mcp` system user, `systemd` unit at
   `/etc/systemd/system/bioinformatics-mcp.service`.
3. Server binds to `127.0.0.1:8080`. Caddy (`bio-mcp.devlin.lan`) terminates
   TLS and requires `Authorization: Bearer $MCP_AUTH_TOKEN` before proxying.
4. CRISPOR lives in its own venv at `/opt/crispor`; genome indexes under
   `/var/lib/bioinformatics_mcp/genomes/`.

### Python version policy

- **MCP server venv:** Python 3.13 (the Trixie default; `python3 -m venv`).
- **`pyproject.toml`:** `requires-python = ">=3.11"` — flexibility for
  contributors on older systems and to keep wheels available.
- **CRISPOR venv:** attempt Python 3.13 first. If its older codebase trips on
  removed stdlib modules, rebuild the venv with `python3.11` (kept available
  on Trixie as a versioned package) — CRISPOR and the MCP server don't share
  an interpreter, so the fallback is local to `/opt/crispor/venv`.
- **PEP 668:** Trixie enforces it. Every pip install in this project runs
  inside a venv; never `pip install` at the system level.

**Auth note:** the bearer-at-proxy design is **intentional for self-hosted
use**. It would not pass Anthropic Directory submission — the directory
requires OAuth (CIMD or DCR) rather than a user-pasted static bearer. If this
server ever needs public listing, auth has to move to OAuth per spec 2025-11-25.

---

## Claude connection

### Claude.ai
Settings → Connectors → *Add custom connector*. URL
`https://bio-mcp.devlin.lan/mcp`, transport **Streamable HTTP**, auth
**Bearer token** (paste the `MCP_AUTH_TOKEN` value).

### Claude Code
```bash
claude mcp add --transport http bioinformatics https://bio-mcp.devlin.lan/mcp \
  --scope user \
  --header "Authorization: Bearer ${MCP_AUTH_TOKEN}"
```

---

## Deviations from spec

These are conscious, approved departures from
[`bioinformatics-mcp-spec_1.md`](./bioinformatics-mcp-spec_1.md). They exist
so future sessions don't re-litigate them.

### MCP framework: `fastmcp` (jlowin's standalone) instead of `mcp[cli]`
The spec pins `mcp[cli]>=1.2.0`, which bundles **FastMCP 1.0 — now frozen
upstream**. The Claude Code `mcp-server-dev:build-mcp-server` plugin
explicitly recommends the standalone `fastmcp` package for new servers:

- Actively developed (1.0 is feature-frozen).
- First-class streamable-HTTP transport with `mcp.run(transport="http", …)`
  — directly relevant to our remote deployment (spec §2.2, §9.2–9.4).
- Compatible import surface; tool bodies are near-identical.

Pinned as `fastmcp>=2.0,<4.0` in `pyproject.toml`.

### Tool annotations: added `title` and `idempotentHint` defaults
Spec §4 mandates `readOnlyHint: true, destructiveHint: false,
openWorldHint: true` for every MVP read tool. Plugin guidance additionally
requires `title` for Anthropic Directory compliance and recommends
`idempotentHint`. Defaults adopted:

| Annotation         | Default for this server |
|--------------------|-------------------------|
| `readOnlyHint`     | `true`  |
| `destructiveHint`  | `false` |
| `openWorldHint`    | `true`  |
| `idempotentHint`   | `true`  |
| `title`            | Human-readable per tool (e.g. `bio_fetch_sequence` → `"Fetch NCBI Sequence"`) |

Tools that must override `idempotentHint: false` because their upstream
corpus changes over time:

- `bio_blast_search` (NCBI databases grow)
- `bio_fetch_bioactivity` (ChEMBL adds new assays)
- `bio_search_literature` (Europe PMC corpus grows)
- `bio_fetch_variant`, `bio_predict_variant_effect` (Ensembl releases update)

Comments in each tool module record the override rationale.

### Skill reference in spec §14
The spec's closing instructions reference `/mnt/skills/examples/mcp-builder/`.
That path is a **claude.ai-specific** skill location. The Claude Code
equivalent — and authoritative reference for this project — is the
`mcp-server-dev:build-mcp-server` plugin shipped with
`claude-plugins-official`. Files actually read during design:

- `build-mcp-server/SKILL.md`
- `references/tool-design.md`
- `references/server-capabilities.md`
- `references/remote-http-scaffold.md`
- `references/auth.md`

---

## Language / style

British English in docstrings, comments, prose (optimise, behaviour, colour,
analyse). Code identifiers keep their original conventions.
