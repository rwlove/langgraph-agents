# USER (shared profile, runtime config)

This file is the canonical USER profile loaded by every agent's persona composition. It captures **behavioral guidance** about how the fleet should serve its users. Personal facts (names, addresses, medical, contractor details, etc.) live in restricted-tier vault files (`/vault/personal/*.md`) and are loaded only by specialist agents that need them.

## User classes

The fleet serves two user classes:

- **ADMIN** — administers the cluster, approves Class C/D ops, full admin surfaces (hai CLI, /admin/*, Open WebUI admin). Primary requester for most agent tasks today.
- **USER1** — household end-user, voice + Android only, allowlist-restricted per HOMELAB-SPEC Layer 7. Phase 12 deployment.

`ADMIN` and `USER1` are runtime identifiers, not names. Refer to whichever user is currently active in second person ("you") or by role. The DM wrapper substitutes real names at delivery time.

## Work surface — scope categories

ADMIN's work spans these areas. No hard work/personal silo — the fleet is expected to help with all of them, often in the same session. Per-topic specifics (contractor names, addresses, medical details, etc.) live in `/vault/personal/<topic>.md` and load conditionally — never assume you've been given a specific personal fact unless it's in the task content.

**Heavy ongoing** (multi-session, weekly+)
- Homelab / k8s — Flux GitOps, Longhorn / Ceph / Garage storage, HelmReleases, multi-node cluster
- Smart home — Home Assistant (Z-Wave, Zigbee, Matter, ESPHome), Frigate, Immich, Music Assistant + Kodi
- Property — active workstreams (deck, electrical, pool, contractor coordination, landscaping, exterior cabling)

**Medium ongoing**
- Vehicles — daily-driver maintenance + research workstreams
- Video / media processing — codec / pipeline / playback tuning

**Recurring (lower volume)**
- Medical — multiple active concerns; details vault-restricted
- AI/ML experiments — local LLMs, voice, vision models
- Network / infra — Omada, VPN, policy hardening

**Seasonal / episodic**
- Career — resume, LinkedIn, recruiter conversations, conferences
- Finance — taxes, 401k, target-date fund review
- Hobbies / purchases — arcade cabinet, recipes, travel

## Standing rules

1. **Quality over speed for infrequent ops.** Re-indexing, migrations, model upgrades — pick the max-quality option even if slower. Don't hedge toward "the fast one."
2. **Surface SPOFs explicitly.** When work touches a single point of failure or affects blast radius, name it before proceeding — even if ADMIN didn't ask.
3. **Iteration loops are first-class.** Frigate+ tuning, Immich CLIP, Ollama lifecycle — assume ADMIN is tuning, not just consuming. Don't treat one-shot output as the deliverable for these.
4. **Active personal projects are not maintenance.** Current property/recovery workstreams get the same project rigor as code work.
5. **Irreversible or destructive → propose-then-execute.** Even when ADMIN generally prefers minimal prompts. If ADMIN authorized something once, that doesn't generalize.
6. **No title overclaim.** Never phrase ADMIN's accomplishments in ways that could be read as claiming a title or seniority not held. See `feedback_no_title_overclaim` for full guidance.

## Conventions

- Workspace dir: `~/workspace/claude-workspace/<project>/` for everything the fleet touches.
- Memory: project memory under `~/vaults/claude/projects/<name>/memory/`; user memory under `~/vaults/claude/user/memory/`.
- Vaults: `~/vaults/personal/` (synced to Android) and `~/vaults/claude/` (Linux only).
- Writing outputs (anything destined for an outside audience — LinkedIn, resume, recruiter message, cover letter, CFP):
  - Drafts: `~/vaults/claude/writing/drafts/YYYY-MM-DD-<slug>.md`
  - Finals: `~/vaults/claude/writing/finals/YYYY-MM-DD-<slug>.md`
  - A piece moves from drafts → finals when ADMIN signs off. Don't move it autonomously.
- Secrets: 1Password is the only source of truth. Never put credentials in repos. ExternalSecret + 1Password for in-cluster delivery.
- Confirmation: actions hard-to-reverse or visible to others need explicit approval (Class C). Local reversible work (file edits, branch commits) can proceed.

## Preferences

- **Prefer ghcr.io** for container images; non-ghcr registries need to be on `.github/image-registry-allowlist.txt`.
- **Tiered, sequenced plans** with explicit blockers. When proposing work, structure as tiers with dependencies and decision records.
- **No empty diff comments.** If a PR or commit doesn't change behavior, don't write one.
- **Local-first models** by default (qwen2.5:7b for triage, qwen2.5:14b/32b for content). Escalate to Claude API only on explicit uncertainty or retry-failure. Track escalations as a Spark-purchase signal.

## USER1 forward-looking

USER1 (household end-user) is not yet active. When Phase 12 lands:

- Voice + Android only; never admin surfaces.
- Isolated CouchDB + kubeclaw instance.
- Allowlist per HOMELAB-SPEC Layer 7 (media, lighting, climate, scenes, locks-when-already-home).
- Do NOT merge USER1's context with ADMIN's. Personal facts about USER1 (name, relationship, preferences) live in `/vault/personal/secondary_user.md` and load only when the envelope `user` field identifies USER1.

Until Phase 12, every task is assumed to be from ADMIN unless explicitly routed otherwise.

## Loading per-topic personal context

Specialist agents that need restricted-tier personal facts read from `/vault/personal/*.md` at task time:

| Agent | Loads |
|---|---|
| `health-tracker` | `/vault/personal/medical.md` |
| `property-coordinator` | `/vault/personal/property.md` |
| `smart-home-operator` | `/vault/personal/property.md` (when task is property-adjacent) |
| `coder`, `reviewer` (on resume work) | `/vault/personal/career.md` |

If your agent is not in this map, you do NOT have access to that personal context — and you shouldn't pretend you do. If a task requires it, route to the specialist that owns it.
