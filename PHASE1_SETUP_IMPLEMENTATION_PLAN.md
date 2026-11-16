# Phase 1: Setup — Implementation and Completion Plan

Prepared: November 16, 2026

## 1. Purpose and scope

This plan follows the colleague's **Phase 1: Setup** checklist:

1. Linnworks API connection for US and CA: app authorization, configuration, logging, rate limits, and error alerts.
2. EC2 (`t4g.small`) and OpenSSH SFTP: jailed users for US 3PL, CA 3PL, and Linnworks; folders; IP allowlisting; SES email.
3. Database schema: eight tables, indexes, and creation scripts.

This is an implementation plan, not evidence of deployment. Files and commands marked **proposed** do not exist yet. No AWS resources or server configuration were changed when creating this document.

The completed catalog/inventory synchronization phase in `DEVELOPMENT_PLAN_DIAGRAMS.md` has a different scope. SKU swapping and warehouse dispatch belong to the later 3PL pipeline and are outside this setup phase.

## 2. Current repository findings

- **API foundation implemented:** `lwsync/client/auth.py`, `lwsync/client/live.py`, `lwsync/config.py`, and `lwsync/logger.py` provide regional credentials, authentication, retries, rate limiting, and logs. Deployment acceptance still requires successful US and CA probes.
- **Error emails missing:** failures are logged, but there is no SES sender or integrated email delivery test.
- **Infrastructure unverified:** the repository describes EC2/SFTP, but contains no repeatable provisioning implementation or deployment evidence. Inspect any colleague-managed resources before creating replacements.
- **Schema implemented:** `sql/001_create_tables.sql` defines eight tables and indexes. `lwsync/db.py` applies creation statements. Production schema verification is still required.
- **Connection-test logging needs a fix:** `test_live_connection()` currently logs a session-token prefix. Remove it entirely.
- **Startup failure handling needs a fix:** the CLI logs database initialization failure but can continue into `run`. Required initialization failures must stop execution.
- **Schema checks need a fix:** a missing SQL file currently produces a warning and returns; `CREATE TABLE IF NOT EXISTS` also does not validate an existing table's columns or indexes.
- **Production database transport needs implementation:** the current PyMySQL connection setup does not configure certificate-verified TLS.

## 3. Inputs needed before deployment

Collect these values in deployment configuration, without committing secrets:

- AWS account, AWS region, VPC, subnet, approved ARM64 Ubuntu image, disk size, and EC2 SSH administration key.
- Existing infrastructure identifiers, if a colleague has already provisioned resources.
- Approved administrator, Linnworks, US 3PL, and CA 3PL source IP ranges.
- Public SSH keys for `linnworks`, `3pl_us`, and `3pl_ca`.
- RDS endpoint, database name, engine/version, credentials, and CA certificate bundle.
- SES sender identity, approved recipients, SES region, and sandbox/production status.
- US and CA Linnworks application credentials and verified API hosting origins.

Business region and API hosting region are separate: retain the repository's explicit `LW_US_API_BASE_URL` and `LW_CA_API_BASE_URL` settings.

## 4. Proposed files and changes

Keep the Python application. Add Terraform for AWS resources and Ansible for server configuration so setup can be reviewed and repeated.

```text
infra/terraform/
  versions.tf               # Provider constraints and committed provider lock file
  variables.tf              # Account-specific inputs; no credentials
  main.tf                   # EC2, security groups, instance role/profile
  outputs.tf                # Instance and security-group identifiers
  terraform.tfvars.example  # Placeholder input values
infra/ansible/
  inventory.example.ini
  site.yml                  # Packages, app user, SFTP users, folders, permissions
  templates/sftp.conf.j2
  templates/lwsync.service.j2
lwsync/alerts.py             # SES sender and sanitized alert formatting
lwsync/health.py             # Configuration and read-only dependency checks
scripts/verify_schema.py     # Compare deployed schema against expected definitions
tests/offline/test_alerts.py
tests/offline/test_health.py
tests/offline/test_cli_failures.py
docs/PHASE1_DEPLOYMENT_EVIDENCE.md
```

Modify `lwsync/config.py`, `lwsync/__main__.py`, `lwsync/db.py`, `.env.example`, `requirements.txt`, `.gitignore`, and `README.md` as described below. Pin and validate deployment-tool versions when implementing. Add Terraform state, real variable files, inventories containing private details, and generated secrets to ignore rules; keep remote state access restricted.

## 5. Work package A: API configuration and failure handling

### Configuration validation

- Validate `LW_MODE`, required regional credentials, API origins, positive rate limits, retry settings, and required database settings before starting a job.
- Do not require live credentials for offline fixture tests.
- Validate only the dependencies required by a command: a connection probe should not require database initialization.
- Preserve existing per-region authentication, 401 refresh, 429 cooldown, timeout, and retry behavior.
- Remove all token values, including prefixes, from logs. Redact authorization headers and sensitive URL parameters in errors.
- Include a run ID, region, entity, timestamp, and outcome in operational logs. Do not include customer addresses or full order payloads in alerts.

### Failure behavior

- Introduce one job-boundary failure handler for sync, database initialization, and health commands. Avoid sending the same alert from both worker and CLI layers.
- Stop with a nonzero exit code when required initialization or a job fails.
- Record sync errors where the database is reachable; database error recording must not replace the original exception if it fails too.
- Attempt an email after a terminal failure, rather than for every retry.
- If email delivery fails, log that secondary failure and preserve the original job failure and exit code.
- Catch runtime configuration errors inside a controlled startup path; import-time parsing errors must not bypass error reporting unintentionally.

Acceptance: both regional probes pass, secrets are absent from logs, and simulated failures reliably produce a nonzero exit code.

## 6. Work package B: SES error alerts

Implement `lwsync/alerts.py` using `boto3` and an EC2 instance role. Do not put AWS access keys in application configuration.

Proposed configuration additions:

```dotenv
LW_ALERTS_ENABLED=false
AWS_REGION=
LW_ALERT_FROM=
LW_ALERT_TO=
LW_ENVIRONMENT=development
DB_SSL_CA=
DB_SSL_VERIFY_IDENTITY=true
```

`LW_ALERT_TO` should support a documented comma-separated recipient list. Production deployment must explicitly enable alerts and validate the sender, recipients, and AWS region. For RDS, require a CA bundle and certificate/hostname verification; retain an explicit local-development connection mode.

Alert content should contain the environment, run ID, region, entity, sanitized error category/message, UTC timestamp, and log location. Use bounded SDK retries and timeouts. Start with one terminal alert per job invocation; document the scheduler's maximum frequency and add persistent deduplication if repeat failures would overwhelm recipients.

Create a least-privilege IAM policy allowing the chosen SES send operation from the approved sender identity. Verify the identity in the selected SES region. While SES remains in the sandbox, recipients also require verification unless using the mailbox simulator. See [SES verified identities](https://docs.aws.amazon.com/ses/latest/dg/verify-addresses-and-domains.html) and [production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html).

Add a proposed `python -m lwsync test-alert` command. It must be invoked explicitly and return failure if SES rejects the message. An SES message ID confirms API acceptance; an operator must also confirm inbox delivery for acceptance.

## 7. Work package C: EC2 and network provisioning

Terraform should create or reference the agreed resources:

1. An ARM64-compatible Ubuntu EC2 instance of type `t4g.small`, with encrypted storage and an instance profile for SES.
2. A stable SFTP endpoint, such as an Elastic IP and documented DNS name, if external partners require one.
3. A security group allowing TCP 22 only from approved administrator and partner CIDRs. Do not default to public access.
4. RDS access on TCP 3306 from the application's EC2 security group. Keep RDS private; do not expose MySQL to partner SFTP clients.
5. Required outbound connectivity for Linnworks HTTPS, SES HTTPS, DNS, and the selected package/deployment path.

Terraform must accept existing VPC/RDS identifiers rather than silently creating a second environment. Import existing managed resources where appropriate. Review the plan before applying it and record the resulting resource identifiers in deployment evidence.

See [RDS connection security](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/securing-mysql-connections.html).

## 8. Work package D: OpenSSH SFTP configuration

Use three separate accounts and jails. Proposed host layout:

```text
/sftp/linnworks/orders/
/sftp/linnworks/archive/
/sftp/linnworks/failed/
/sftp/3pl_us/inbound_orders/
/sftp/3pl_us/archive/
/sftp/3pl_us/failed/
/sftp/3pl_ca/inbound_orders/
/sftp/3pl_ca/archive/
/sftp/3pl_ca/failed/
```

Inside the Linnworks jail, the client sees `/orders`, not `/sftp/linnworks/orders`. Each 3PL sees its own `/inbound_orders`.

Ansible should:

- Create a dedicated SFTP group and the three users, with public-key authentication and no interactive shell access.
- Keep `/sftp` and each jail root owned by root and non-writable by the SFTP users. Grant write access only to designated child folders.
- Store authorized keys in a root-managed location, with documented key rotation and host-key fingerprints.
- Configure `ChrootDirectory /sftp/%u`, `ForceCommand internal-sftp`, password authentication disabled, forwarding disabled, and TTY access disabled in a scoped `Match Group` block.
- Ensure the jail's initial working directory exists. Define each account's upload/download rights explicitly; use read-only SFTP for warehouse accounts if they only download orders.
- Give the separate application service account access to required child folders using scoped groups or ACLs, without making jail roots writable or allowing partners into each other's folders.
- Validate the full SSH configuration with `sshd -t` before reloading it, while preserving administrator access.
- Be repeatable without replacing existing keys or resetting folder contents.

OpenSSH requires safe ownership and permissions throughout the chroot path. Refer to the [OpenSSH configuration manual](https://man.openbsd.org/sshd_config) and validate against the installed server version.

Acceptance must exercise all three real accounts: allowed transfers succeed; shell/forwarding attempts fail; users cannot escape their jail or read another partner's files; an unapproved source IP cannot connect.

## 9. Work package E: Database completion

Retain these existing tables:

1. `lw_products`
2. `lw_locations`
3. `lw_stock_levels`
4. `lw_orders`
5. `lw_order_items`
6. `lw_order_refunds`
7. `lw_pipeline_log`
8. `lw_sync_state`

Implementation tasks:

- Add certificate-verified TLS configuration to both PyMySQL connection paths in `lwsync/db.py`. Use the RDS endpoint hostname for identity verification. See [RDS MySQL TLS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.SSLSupport.html).
- Make a missing schema script an explicit initialization error.
- Validate database identifiers before interpolating them into DDL. The SQL currently uses fixed `lw_` names: either enforce that prefix or implement prefix support consistently; do not suggest arbitrary prefixes already work.
- Use a separate migration identity for DDL. Run regular sync with a restricted identity and `--skip-db-init` after successful deployment verification.
- Implement a read-only schema verifier using `information_schema` to check expected columns, types, nullability, unique keys, and indexes. Check the eight named tables rather than requiring the entire database to contain only eight tables.
- Use new versioned SQL migrations for schema changes. Never rely on `CREATE TABLE IF NOT EXISTS` to repair an existing incompatible schema, and do not drop tables to make verification pass.
- Test creation on an empty disposable database and a second initialization on the same database. Compare schema and existing row counts before and after.

Acceptance: all eight tables and expected indexes exist on the target database; TLS verification succeeds; a deliberately invalid CA fails; the runtime user can sync but cannot create/drop tables.

## 10. Deployment and verification sequence

1. Confirm existing AWS/server resources and fill in the deployment inputs.
2. Implement configuration validation, redaction, terminal-failure handling, SES support, and database TLS/schema verification.
3. Add mocked tests for SES success/failure, missing configuration, database startup failure, redaction, and preserving the original error if alert delivery fails. Keep offline tests unable to reach real networks.
4. Review the Terraform plan; provision or adopt the agreed infrastructure. Run the Ansible configuration and SSH validation.
5. Install Python and dependencies in an ARM64-compatible virtual environment; deploy the app as a separate non-root service account. Keep secrets outside source control in a restricted environment file or managed secret store.
6. Apply the existing schema using migration credentials, then run the proposed schema verifier. Switch to runtime credentials.
7. Run the existing `python -m lwsync test-live` command for US and CA. It authenticates and reads locations; it should not alter orders or stock.
8. Run the proposed `test-alert` command with an approved recipient and confirm delivery.
9. Test SFTP login, permissions, transfers, and IP restrictions with each partner. Use clearly named test files, not dispatchable order files.
10. Run one controlled job with a simulated failure; confirm its log, nonzero exit code, and received email. Do not trigger real customer/order failures for this check.
11. Configure the systemd job and agreed schedule, preventing overlapping runs. Validate the actual service environment and permissions, not only an administrator's shell.
12. Save sanitized evidence and update project status only after all acceptance checks pass.

The existing offline command is:

```powershell
.venv\Scripts\python.exe tests/run_offline.py
```

Extend that runner to include the new offline test modules. Existing database integration tests write data; run them against a disposable test database, not production.

## 11. Completion checklist and evidence

Record date, operator, environment, release commit, result, and a sanitized evidence reference for every item in `docs/PHASE1_DEPLOYMENT_EVIDENCE.md` when that file is created.

- [ ] US authentication and read-only API probe pass.
- [ ] CA authentication and read-only API probe pass.
- [ ] Retry/rate-limit tests pass; logs contain no credentials or token fragments.
- [ ] Required initialization failures stop the job with a nonzero exit code.
- [ ] EC2 instance, stable SFTP endpoint, and source-IP restrictions are verified.
- [ ] All three SFTP accounts pass transfer, isolation, and no-shell checks.
- [ ] Application user has the required folder access without weakening jail permissions.
- [ ] SES sender/recipient requirements and instance-role permissions are verified.
- [ ] Explicit alert test and a controlled terminal-failure alert reach the inbox.
- [ ] All eight database tables, expected columns, and indexes pass verification.
- [ ] Database TLS and runtime/migration privilege separation are verified.
- [ ] Setup can be rerun without losing data or replacing partner keys.
- [ ] Service execution, scheduling, and overlap prevention are verified.
- [ ] README and deployment evidence are updated for the actual environment.

**Mark Phase 1 complete only when every applicable check passes. Code completion alone does not confirm EC2, SFTP, SES, or production database readiness.**

## 12. Recovery instructions to include in the deployment runbook

- Preserve a known-good SSH configuration and administrator session until new access is verified; restore and revalidate the previous configuration if partner access fails.
- Deploy application releases separately so the previous release can be restored without changing SFTP folders.
- Preserve uploads, archives, keys, and logs during configuration reruns or application rollback.
- Back up the database before migrations; use a reviewed migration recovery procedure rather than dropping tables.
- If alerts fail, keep the job visibly failed and retain local logs while correcting SES configuration.

After this checklist is complete, implement the separate 3PL pipeline: approved SKU swaps, order holds, durable duplicate protection, and warehouse CSV dispatch.
