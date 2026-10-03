#!/bin/sh
# Creates the application role. It is deliberately NOT a superuser and has NO BYPASSRLS,
# so Postgres row-level security applies to every query the app makes.
set -eu
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE replay LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE replay OWNER replay;
SQL
