"""SQL migration files for the task queue.

Files are named `NNN_<description>.sql` and applied in lexicographic
order by `agents.queue.migrate.ensure_schema`. Use `CREATE * IF NOT
EXISTS` / `ALTER * IF NOT EXISTS` so re-application is idempotent.
"""
