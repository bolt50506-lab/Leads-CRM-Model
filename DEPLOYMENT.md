# LeadFlow CRM — Client Deployment Checklist

## Before handover
- Set `ADMIN_EMAIL` to the client's real administrator email.
- Set a strong `ADMIN_PASSWORD` on first database initialization.
- Set a long random `JWT_SECRET`.
- For multi-user production, set `DATABASE_URL` to PostgreSQL.
- Enable HTTPS behind the company's reverse proxy.
- Configure automated database backups and test a restore.

## Access model
- Administrator: all leads, agents, stages/tags, import and export.
- Agent: only leads assigned to their own account.
- Agent cannot import/export/reassign or manage other accounts.

## Data safety
- Import is append/upsert; it never deletes unrelated leads.
- Match priority: normalized email, then normalized phone.
- Email-only and phone-only leads are valid.
- SQLite default path: `data/leadflow.db`.
- PostgreSQL is recommended for production.
