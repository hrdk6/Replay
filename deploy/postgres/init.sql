-- Runs once when the local Postgres volume is created.
-- The application role is deliberately NOT a superuser and has NO BYPASSRLS:
-- superusers silently bypass row-level security, which would hide isolation bugs.
CREATE ROLE replay LOGIN PASSWORD 'replay' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE replay OWNER replay;
CREATE DATABASE replay_test OWNER replay;
