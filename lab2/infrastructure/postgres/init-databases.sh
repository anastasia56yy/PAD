#!/bin/sh
# Database-per-service on one PostgreSQL server: every service gets its own database and its own
# login that can only reach that database. Runs once, on the first start of an empty volume.
set -e

create_db() {
  db="$1"; user="$2"; password="$3"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
    CREATE USER $user WITH PASSWORD '$password';
    CREATE DATABASE $db OWNER $user;
    REVOKE ALL ON DATABASE $db FROM PUBLIC;
SQL
}

create_db auth_db         auth_svc         "$AUTH_DB_PASSWORD"
create_db user_db         user_svc         "$USER_DB_PASSWORD"
create_db history_db      history_svc      "$HISTORY_DB_PASSWORD"
create_db notification_db notification_svc "$NOTIFICATION_DB_PASSWORD"
