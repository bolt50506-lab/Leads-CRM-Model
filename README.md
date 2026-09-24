# LeadFlow CRM — Client Production Package

A full-stack lead CRM with a persistent relational database, role-based access control, agent accounts, pipeline management, activities, and append/upsert imports.

## Data safety / where data is stored

### Default Windows/single-server mode
If `DATABASE_URL` is not set, LeadFlow uses SQLite at:

`data/leadflow.db`

This file is created automatically and persists across application restarts. The application does **not** recreate or clear it on startup. Importing leads is an append/upsert operation and does not delete unrelated leads.

Back up this file with:

```bash
python backup_db.py
```

Backups are written to `backups/` with timestamps.

### Recommended company deployment
For a multi-agent company deployment, use PostgreSQL. Set `DATABASE_URL` to a PostgreSQL connection string, for example:

```text
postgresql+psycopg://leadflow:YOUR_PASSWORD@YOUR_DB_HOST:5432/leadflow
```

PostgreSQL is the recommended production database for concurrent users, centralized storage, backup/restore tooling, and future horizontal scaling.

The included `docker-compose.yml` provisions a PostgreSQL 17 database with a persistent Docker volume. Do not use the example database password in production.

## Access control

- Administrator sees all leads and manages users, imports, stages and tags.
- Each agent has a unique ID: `AGT-001`, `AGT-002`, etc.
- Agents can only query/update leads assigned to their own account.
- Backend authorization enforces the restriction; hiding UI elements is not the security mechanism.
- Agents cannot reassign leads, import, manage users, manage stages/tags, or delete leads.

## Initial administrator

Only the first database initialization creates the administrator account. Credentials come from environment variables:

- `ADMIN_EMAIL` (default: `admin@leadflow.local`)
- `ADMIN_PASSWORD` (default: `Admin@123`)

**Change these before production.** If the database already exists, changing the environment variables does not overwrite the existing admin password.

## Agent management

Administrator-only account editing is included: change an agent login email or set a new password without changing the stable Agent ID or existing lead assignments. Agents never receive Team, Import, or Export controls.


Administrator → Team → Create Agent.

The system creates the next Agent ID automatically and stores the login account in the database. Give the agent their email/password. The agent sees only their assigned leads after login.

## Lead imports

Administrator-only import supports Excel/CSV and ZIP packages containing multiple Excel/CSV files. Imports are append/upsert:

1. Existing leads are never deleted by an import.
2. Exact normalized email is matched first.
3. Exact normalized phone is matched second.
4. Email-only and phone-only leads are valid.
5. Rows without name, phone and email are rejected as invalid.
6. New rows are inserted; matching rows are updated without deleting unrelated records.

## Run on Windows

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python run.py
```

Open `http://localhost:8000`.

## Production notes

- Set a strong random `JWT_SECRET`.
- Set a real company admin email/password before the first production startup.
- Use PostgreSQL for a multi-agent deployment.
- Put the app behind HTTPS/reverse proxy in production.
- Back up PostgreSQL with `pg_dump` and verify restores.
- Do not commit `.env`, database files, or backups to source control.


## Deploy to Render (shared company instance)

This project includes `render.yaml` for a Render Blueprint deployment. It provisions one Python web service and a managed PostgreSQL database. The web service uses the database connection string from Render and listens on Render's injected `PORT`. No SQLite file or browser localStorage is used as the production source of truth.

### First deployment

1. Extract this project and create a **private** GitHub repository. Commit the project files (do not commit `.env`, database files, customer exports, or backups).
2. In Render, choose **New → Blueprint**, connect the private repository, and select the branch containing `render.yaml`.
3. Review the Blueprint resources and pricing. The supplied configuration uses paid service/database plans; do not switch the database to an expiring free database for live company data.
4. When prompted for `ADMIN_EMAIL` and `ADMIN_PASSWORD`, enter the client's real administrator email and a unique strong password. Render generates `JWT_SECRET`. Store all secrets only in Render environment settings, never in Git.
5. Deploy. Wait for the web service health check `/api/health` to pass, then open the `onrender.com` URL. Sign in with the admin credentials you configured and create agent accounts in Team.

### Shared data and user/device access

All users connect to the same deployed web service and PostgreSQL database. Each agent signs in with their own account on any device; the API scopes lead reads and updates to that agent's assigned leads. Admin-only account management, import, and export remain server-authorized. A user's data is not stored solely in that user's browser.

### Data retention and backups

Imports use append/upsert behavior: new records are added, matching email/phone records are updated, and unrelated existing leads are not deleted. Deploying a new app version does not replace the managed database. This does not mean accidental deletion is impossible: admins can still intentionally delete records in the app, and credentials/access can be compromised. Before live use, establish an owner-approved retention policy, schedule database exports/backups, and test restoring a backup. Render notes that free Postgres instances expire after 30 days and paid Postgres plans include continuous backups/PITR; verify the current plan and recovery window in Render's dashboard.

### Important before client go-live

- Confirm `ADMIN_EMAIL`, `ADMIN_PASSWORD`, and generated `JWT_SECRET` are set in Render.
- Use the paid managed Postgres service in this Blueprint for persistent company data.
- Test admin and agent access from separate browsers/devices and confirm an agent cannot fetch another agent's lead by direct API request.
- Test import with a small copy of the client's files first; inspect add/update/skipped/error totals.
- Set up and rehearse backups/restores, and agree who can delete/archive records.
- Treat this as a deployment-ready package, not a substitute for a security review, UAT, and a tested recovery procedure before production customer data is entered.
