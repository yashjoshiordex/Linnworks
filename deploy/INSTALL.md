# Installing lwsync on the Scripts server

Server: the Scripts EC2 (Ubuntu 18.04, user `ubuntu`, private IP 172.31.81.133).
The ShipStation scripts in `/home/ubuntu/scripts` and their cron jobs are not touched.

| What | Where |
|---|---|
| Code | `/home/ubuntu/lwsync` (later: a git clone in the same place) |
| Python 3.12 | managed by `uv` in `/home/ubuntu/.local/share/uv` (the system `python3` 3.6 stays as it is) |
| Virtual environment | `/home/ubuntu/lwsync/.venv` |
| Settings and secrets | `/home/ubuntu/lwsync/.env` (chmod 600, never in git) |
| Logs | `/home/ubuntu/lwsync-logs/` (`orders.log`, `catalog.log`, `health.log`, `sync.log`, `admin.log`, `cron.log`), rotated daily, kept 90 days |

Every command below runs as `ubuntu` unless it starts with `sudo`. Check the prompt says
`ubuntu@ip-172-31-81-133` before starting.

## 0. Before the install (once)

1. **Database version.** Run `SELECT VERSION();` on the RDS database and note it (the schema targets MariaDB 10.6+ / MySQL 8+;
   it was tested on MySQL, so tell us the version before step 2).
2. **Create the tables** with an admin login, into the existing production database. Only `lw_`
   tables are created; nothing existing is changed (`CREATE TABLE IF NOT EXISTS`):
   ```bash
   mysql -h <RDS host> -u <admin user> -p <database> < /home/ubuntu/lwsync/sql/001_create_tables.sql
   ```
3. **Create a database user for the sync** that can only touch the `lw_` tables (run as admin;
   replace the password and database name):
   ```sql
   CREATE USER 'lwsync'@'172.31.81.133' IDENTIFIED BY '<strong password>';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_products      TO 'lwsync'@'172.31.81.133';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_locations     TO 'lwsync'@'172.31.81.133';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_stock_levels  TO 'lwsync'@'172.31.81.133';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_orders        TO 'lwsync'@'172.31.81.133';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_order_items   TO 'lwsync'@'172.31.81.133';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_order_refunds TO 'lwsync'@'172.31.81.133';
   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.lw_sync_state    TO 'lwsync'@'172.31.81.133';
   ```
   Later schema changes (new columns) are applied by an admin with `python -m lwsync init-db`
   using admin credentials for that one command; the `lwsync` user never needs ALTER.
4. **RDS firewall.** The RDS security group should allow port 3306 only from the Scripts server
   (and the SFTP server for the pipeline later), not from the whole internet.

## 1. Python 3.12 (about 15 minutes, no reboot)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
```bash
~/.local/bin/uv python install 3.12
```
```bash
~/.local/bin/uv python find 3.12
```
The last command prints the path of the new Python; `python3 --version` must still say 3.6.9.

## 2. Code and virtual environment

Copy the project folder to `/home/ubuntu/lwsync` (without `.venv`, `.env`, `logs/`), then:
```bash
cd /home/ubuntu/lwsync && ~/.local/bin/uv venv --python 3.12 .venv
```
```bash
cd /home/ubuntu/lwsync && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
```
```bash
mkdir -p /home/ubuntu/lwsync-logs && chmod 750 /home/ubuntu/lwsync-logs
```

## 3. Settings

```bash
cd /home/ubuntu/lwsync && cp .env.example .env && chmod 600 .env
```
Edit `.env` (`nano .env`) and set at least:
```
LW_MODE=live
DB_HOST=<RDS host>
DB_PORT=3306
DB_NAME=<production database>
DB_USER=lwsync
DB_PASSWORD=<the lwsync user's password>
LW_US_APP_ID=...   LW_US_APP_SECRET=...   LW_US_TOKEN=...
LW_CA_APP_ID=...   LW_CA_APP_SECRET=...   LW_CA_TOKEN=...
LW_US_API_BASE_URL=https://us-api.linnworks.net
LW_CA_API_BASE_URL=https://eu-api.linnworks.net
LW_LOG_DIR=/home/ubuntu/lwsync-logs
```
(The CA account is hosted on Linnworks' EU servers, hence `eu-api` for CA.)

## 4. Checks

```bash
cd /home/ubuntu/lwsync && .venv/bin/python -m lwsync test-live
```
Both regions must show `[PASS]`.
```bash
cd /home/ubuntu/lwsync && .venv/bin/python -m lwsync run locations --region ALL --skip-db-init
```
Must end with `Run finished outcome=ok`.

## 5. First full run (about 20 minutes)

Loads the catalog and orders, looking back 35 days (the most Linnworks allows):
```bash
cd /home/ubuntu/lwsync && nohup .venv/bin/python -m lwsync run all --region ALL --days 35 --skip-db-init > /home/ubuntu/lwsync-logs/first-run.out 2>&1 &
```
Follow it with `tail -f /home/ubuntu/lwsync-logs/sync.log`. It must end with
`Run finished outcome=ok`.

## 6. Cron

```bash
crontab -l > /home/ubuntu/crontab-backup-$(date +%F).txt
```
```bash
crontab -e
```
Paste the lines from `deploy/crontab.txt` **below** the existing jobs, save, then check:
```bash
crontab -l
```

## 7. Log rotation (90 days)

```bash
sudo cp /home/ubuntu/lwsync/deploy/logrotate-lwsync /etc/logrotate.d/lwsync
```
```bash
sudo logrotate -d /etc/logrotate.d/lwsync
```
The second command is a dry run: it should list the log files with no errors.

## Reading the logs

| Question | Command |
|---|---|
| Did the last runs succeed? | `grep "Run finished" /home/ubuntu/lwsync-logs/orders.log \| tail -5` |
| Any errors today? | `grep -E "ERROR\|CRITICAL" /home/ubuntu/lwsync-logs/*.log` |
| Everything from one run | `grep "run=0615-7f3a" /home/ubuntu/lwsync-logs/orders.log` (run id from the "Run started" line) |
| Is everything healthy? | `cd /home/ubuntu/lwsync && .venv/bin/python -m lwsync health` (exit 0 = healthy) |
| More detail for one run | add `LW_LOG_LEVEL=DEBUG` in front of the command (every API page and request) |
| Older days | `zgrep "Run finished" /home/ubuntu/lwsync-logs/orders.log-2026*.gz` |

Exit codes: 0 ok, 1 a step failed or unhealthy, 2 configuration problem (message says which
setting), 130 interrupted.

## Stopping and starting

- Pause all lwsync jobs: `crontab -e` and put `#` in front of the three lwsync lines.
- A run in progress can be stopped with `kill <pid>`: it logs "Run interrupted", rolls back the
  open transaction and records it in `lw_sync_state`; the next run starts clean.

## Switching to git (later)

1. Pause the lwsync cron lines.
2. `mv /home/ubuntu/lwsync /home/ubuntu/lwsync-old`
3. `git clone <repo> /home/ubuntu/lwsync`
4. `cp /home/ubuntu/lwsync-old/.env /home/ubuntu/lwsync/ && chmod 600 /home/ubuntu/lwsync/.env`
5. Repeat step 2's two `uv` commands (virtual environment).
6. Run the step 4 checks, then un-pause the cron lines. Logs stay in `/home/ubuntu/lwsync-logs`.
7. Delete `/home/ubuntu/lwsync-old` after a few days.

Updates after that: pause cron, `git pull`, re-run the `uv pip install` command, un-pause.
