#!/bin/bash
# Runs once on a fresh Postgres volume: app DB (authz) + isolated test DB (authz_test).
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE authz_test"
for db in authz authz_test; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" -f /schema/schema.sql
done
