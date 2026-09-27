#!/bin/sh
# Runs once, when the postgres volume is first initialized.
# Creates the database used by backend integration tests next to the app database.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE DATABASE "${POSTGRES_DB}_test" OWNER "${POSTGRES_USER}";
EOSQL
