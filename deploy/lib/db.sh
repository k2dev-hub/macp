#!/usr/bin/env bash
# Database access shared by the deploy scripts.
#
# Works whether PostgreSQL runs as a local compose container or on another
# machine (the split deployment: app here, database there, reached through an
# SSH tunnel forwarded onto the compose network gateway).
#
# Requires lib/config.sh to be sourced first, and deploy/.env to be loaded when
# the caller wants the rendered MACP_DB_* values.
#
#   . lib/config.sh
#   . lib/db.sh
#   psql_db -Atc 'select 1'          # psql, same arguments as the real client
#   pg_dump_db > backup.dump         # pg_dump -Fc on stdout

db_name() { printf '%s' "${MACP_DB_NAME:-macp}"; }
db_user() { printf '%s' "${MACP_DB_USER:-macp}"; }

# Name of the compose container that would host the database locally.
db_container() { printf 'macp-%s' "${MACP_DB_HOST:-$(cfg database.host db)}"; }

# True when this host does not run the database container itself.
db_is_remote() { ! docker inspect "$(db_container)" >/dev/null 2>&1; }

db_host() { printf '%s' "${MACP_DB_HOST:-$(cfg database.host db)}"; }
db_port() { printf '%s' "${MACP_DB_PORT:-$(cfg database.port 5432)}"; }

# Client container used for a remote database.  It joins the compose network so
# it can reach the tunnel's gateway address.
db_client_image() { printf '%s' "${MACP_PG_IMAGE:-postgres:16-alpine}"; }
db_client_network() { printf '%s' "${MACP_DB_NETWORK:-macp-net}"; }

psql_db() {
  if db_is_remote; then
    docker run --rm -i --network "$(db_client_network)" "$(db_client_image)" \
      psql -h "$(db_host)" -p "$(db_port)" -U "$(db_user)" -d "$(db_name)" "$@"
  else
    docker exec -i "$(db_container)" psql -U "$(db_user)" -d "$(db_name)" "$@"
  fi
}

# pg_dump to stdout in custom format.
pg_dump_db() {
  if db_is_remote; then
    docker run --rm --network "$(db_client_network)" "$(db_client_image)" \
      pg_dump -h "$(db_host)" -p "$(db_port)" -U "$(db_user)" -d "$(db_name)" -Fc
  else
    docker exec "$(db_container)" pg_dump -U "$(db_user)" -d "$(db_name)" -Fc
  fi
}
