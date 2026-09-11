---
name: accidental-data-loss-prevention
description: |
  **STOP AND VERIFY**: ZERO TRUST DELETION POLICY. Before executing ANY command, tool, or script that results in data loss, deletion, or destructive modification (local, remote, or infrastructure), you MUST obtain explicit user consent.
  When in doubt, ask. It is better to wait for confirmation than to accidentally delete production data or critical project assets.
license: Apache-2.0
metadata:
  version: v3
  publisher: google
  projects: [all]
  category: safety
---

# Accidental Data Loss Prevention (ADLP) - ZERO TRUST POLICY

> [!CAUTION]
>
> **STOP AND VERIFY**: Before running ANY command or tool that results in
> irreversible data loss, you **MUST** obtain explicit user consent
> immediately prior to the action.

## Scope of Restriction
This policy applies to **ALL** destructive actions, including but not limited to:
- **Local Filesystem**: Deleting files or directories (`rm -rf`, `delete_file`, overwriting files with empty content).
- **SQL / Databases**: `DROP`, `TRUNCATE`, `DELETE` (especially without `WHERE`), `alembic downgrade`.
- **Docker / Containers**: `docker system prune`, `docker compose down -v`, removing Docker volumes.
- **Cloud / Remote Storage**: Deleting buckets, files, or objects (e.g., `gsutil rm`, Cloudflare R2 deletions).
- **ESP32 Flash / NVS**: `idf.py erase-flash`, `./flash.ps1 erase`, erasing NVS partitions (causes irreversible loss of device identity tokens and calibration data).
- **Infrastructure**: Deleting GCP/AWS resources, KMS key destruction, Spanner/BigQuery resource deletion.
- **Version Control**: `git reset --hard`, `git clean -fd`, force-push to protected branches.

## Anti-Loophole Directives (NEVER BYPASS):
- **NO ASSUMED PRIOR CONSENT**: Even if the user said "Clean everything up" or "Fix all errors", you MUST still stop and ask for specific confirmation before executing the destructive command.
- **NO DATA CRITICALITY ASSUMPTIONS**: You are strictly forbidden from assuming data is "non-critical", "scratch", or "development data". ALL data is subject to this rule.

## Mandatory Procedure

1.  **Halt Execution**: Do **not** execute the command.
2.  **Request Consent**: Explain clearly to the user using the `ask_question` tool:
    -   The **exact command** you intend to run.
    -   The **exact impact** of this action (what data will be lost).
    -   **Why** you believe this is necessary.
    -   A request for their **explicit approval** to proceed.
    -   **Fallback**: If the `ask_question` tool is unavailable (e.g., you are a subagent), you MUST halt and output a visible text message asking the user, or return control to your parent agent. Never proceed silently.
3.  **Wait**: Only proceed if the user provides clear, affirmative consent directly responding to your specific request.
