#!/usr/bin/env bash
set -euo pipefail

BASE="${1:?usage: restore_verify.sh <backup-dir>}"
cd "$BASE"

sha256sum -c SHA256SUMS

cleanup() {
  docker rm -f macp-restore-test >/dev/null 2>&1 || true
  docker volume rm macp-restore-test-data >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
docker volume create macp-restore-test-data >/dev/null

docker run -d --name macp-restore-test \
  -v macp-restore-test-data:/var/lib/postgresql/data \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB=macp \
  postgres:16-alpine >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec macp-restore-test psql -U postgres -d macp -Atc "select 1" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  docker logs --tail 50 macp-restore-test
  echo "restore_verify=FAIL postgres did not become ready"
  exit 1
fi

docker exec -i macp-restore-test pg_restore -U postgres -d macp --no-owner < macp.dump
echo "restored tasks/events:"
docker exec macp-restore-test psql -U postgres -d macp -Atc \
  "select 'tasks='||count(*) from tasks; select 'events='||count(*) from events; select 'work_orders='||count(*) from work_orders;"

echo "bundle refs:"
docker run --rm -v "$PWD/bundles.tgz:/bundles.tgz:ro" macp-api \
  sh -c 'mkdir -p /tmp/bundles && tar -xzf /bundles.tgz -C /tmp/bundles && for f in /tmp/bundles/*.bundle; do n=$(basename "$f"); mkdir -p "/tmp/check-$n"; git -C "/tmp/check-$n" init -q; (cd "/tmp/check-$n" && git bundle verify "$f" >/dev/null && echo "$n ok"); done'

echo "workspace files:"
docker run --rm -v "$PWD/workspaces.tgz:/workspaces.tgz:ro" macp-api \
  sh -c 'tar -tzf /workspaces.tgz | wc -l'

echo "restore_verify=PASS"
