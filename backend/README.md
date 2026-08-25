# Match Systems Admin API

FastAPI + SQLAlchemy (asyncpg) service backing the admin panel and the two
landing pages.

## 1. Prerequisites

- Python 3.10 or newer
- A running PostgreSQL 13+ instance and an empty database

```bash
createdb matchsystems      # or: psql -c "CREATE DATABASE matchsystems;"
```

## 2. Environment

```bash
cd backend
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1
# Windows Git Bash
source .venv/Scripts/activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env          # then edit PGUSER / PGPASSWORD / PGDATABASE
```

## 3. Create the tables

The API runs `db/schema.sql` on startup while `AUTO_MIGRATE=true`. To run it by
hand:

```bash
python init_db.py
```

It is idempotent and inserts **no records** — the database starts empty and is
filled entirely through the admin panel. Then create the first login:

```bash
python create_admin.py
```

## 4. Run

```bash
uvicorn app.main:app --reload --port 8000
```

- API root: <http://localhost:8000/>
- Interactive docs: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/v1/health>

### Port conflict

The `.dc.html` pages were being served on port 8000. Both cannot use it. Either
serve the pages elsewhere (VS Code Live Server defaults to 5500, which is
already in `CORS_ORIGINS`), or run the API on another port:

```bash
uvicorn app.main:app --reload --port 8001
```

If you change the API port, update `DB_CONFIG.BASE_URL` at the top of the
script block in `Match Systems Admin.dc.html`.

## 5. Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/health` | Connection probe (503 when the DB is unreachable) |
| `POST` | `/v1/auth/login` | Issue a JWT (`{"email":…,"password":…}`) |
| `GET` | `/v1/{table}?q=&page=&per=&sort=&dir=` | Paged, searchable, sorted list |
| `POST` | `/v1/{table}` | Create |
| `PATCH` | `/v1/{table}/{id}` | Partial update |
| `DELETE` | `/v1/{table}/{id}` | Delete |

`{table}` is one of: `clients`, `feedback`, `users`, `portals`, `stats`,
`subscriptions`, `modules`, `logs` (read only).

List responses are `{ "items": [...], "total": n, "page": p, "per": k }`.
Errors are `{ "message": "...", "status": n, "errors": [...] }`.

### Natural keys

The UI sends human-readable references and the API resolves them to foreign
keys, so the front end never has to know row ids:

- `users.company_name` / `subscriptions.company_name` → `client_id`
- `feedback.company_name` (or a direct `client_id`) → `client_id`

`feedback.sector` is set by a database trigger from the
parent client, so a testimonial can never drift into the wrong sector.

## 6. Auth

Login validates against the `users` table only -- there are no hardcoded
credentials. Create the first account before you can sign in:

```bash
python create_admin.py                       # prompts
python create_admin.py you@co.com "Name" "pw12345678"
```

`AUTH_REQUIRED=true` (the default) requires a Bearer token for every write;
GET requests stay public so the landing pages can read. Writes are attributed
to the token subject in the audit log. Sign in with:

```bash
curl -X POST http://localhost:8000/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@co.com","password":"pw12345678"}'
```

Then in the browser console on the admin page:

```js
localStorage.setItem('ms_admin_jwt', '<access_token>');
```

Passwords are bcrypt-hashed; `hashed_password` is never returned by the API.
Change `JWT_SECRET` before exposing this
service beyond localhost.
