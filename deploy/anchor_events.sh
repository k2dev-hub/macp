#!/usr/bin/env bash
# Daily audit anchor: pin each project's event-chain head into
# `audit_anchors` and into an append-only, hash-chained file on the NAS.
#
#   ./deploy/anchor_events.sh            # write today's anchor
#   ./deploy/anchor_events.sh --verify   # re-check every stored anchor
#
# Why: the hash chain proves the events are internally consistent, but a
# rewrite of the whole chain (or of the DB file/volume) would still look
# consistent.  Pinning the head hash to a different medium, chained with the
# previous anchor, makes a silent rewrite detectable.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/.."
. "$DIR/lib/config.sh"
. "$DIR/lib/db.sh"

OUT_DIR="${MACP_ANCHOR_DIR:-$(cfg audit.anchor_dir "")}"
if [ -z "$OUT_DIR" ]; then
  # No external storage configured: keep the hash chain next to the state dir.
  OUT_DIR="$(cfg_path deployment.state_dir)/audit-anchors"
fi
if [ -f deploy/.env ]; then
  set -a; . deploy/.env; set +a
fi
DB_CONTAINER="macp-${MACP_DB_HOST:-db}"
MODE="${1:-write}"

# NOTE: no `-i` and an explicit </dev/null.  Under `while ... done <<<"$rows"` a
# `docker exec -i` steals the remaining here-string, so only the first anchor was
# ever checked and a broken second anchor still reported PASS.
q() { psql_db -Atc "$1" </dev/null; }

projects="$(q "select code from projects order by code")"
if [ -z "$projects" ]; then
  echo "no projects to anchor"
  exit 0
fi

if [ "$MODE" = "--verify" ]; then
  rc=0
  echo "== anchors stored in the database =="
  rows="$(q "select to_char(a.day,'YYYY-MM-DD'), p.code, a.last_seq, a.hash
             from audit_anchors a join projects p on p.id=a.project_id
             order by a.day, p.code")"
  while IFS='|' read -r day code seq hash; do
    [ -z "${day:-}" ] && continue
    live="$(q "select e.hash from events e join projects p on p.id=e.project_id
               where p.code='$code' and e.seq=$seq")"
    if [ "$live" = "$hash" ]; then
      echo "  OK    $day $code seq=$seq"
    else
      echo "  BROKEN $day $code seq=$seq expected=$hash live=${live:-<missing>}"
      rc=1
    fi
  done <<<"$rows"

  echo "== hash-chained anchor files =="
  for p in $projects; do
    file="$OUT_DIR/anchors-$p.log"
    [ -f "$file" ] || { echo "  (no file for $p)"; continue; }
    prev=""
    while IFS='|' read -r day code seq hash chain; do
      expect="$(printf '%s|%s|%s|%s|%s' "$day" "$code" "$seq" "$hash" "$prev" | sha256sum | cut -d' ' -f1)"
      if [ "$chain" != "$expect" ]; then
        echo "  BROKEN $p $day seq=$seq (chain mismatch)"
        rc=1
      fi
      prev="$chain"
    done <"$file"
    echo "  checked $p: $(wc -l <"$file") lines"
  done
  [ "$rc" = 0 ] && echo "anchor_verify=PASS" || echo "anchor_verify=FAIL"
  exit "$rc"
fi

mkdir -p "$OUT_DIR"
today="$(date -u +%F)"
rc=0
for p in $projects; do
  head="$(q "select e.seq || '|' || e.hash from events e
             join projects p on p.id=e.project_id
             where p.code='$p' order by e.seq desc limit 1")"
  if [ -z "$head" ]; then
    echo "$p: no events yet, skipping"
    continue
  fi
  seq="${head%%|*}"
  hash="${head##*|}"

  file="$OUT_DIR/anchors-$p.log"
  prev=""
  if [ -f "$file" ] && [ -s "$file" ]; then
    prev="$(tail -1 "$file" | cut -d'|' -f5)"
  fi
  chain="$(printf '%s|%s|%s|%s|%s' "$today" "$p" "$seq" "$hash" "$prev" | sha256sum | cut -d' ' -f1)"
  if [ -f "$file" ] && grep -q "^$today|$p|" "$file"; then
    # Re-running on the same day replaces that day's line (the head may have
    # moved).  NOTE: use grep -v, not `sed -i "\|...|d"` — the record separator
    # is `|`, which collides with sed's alternate address delimiter (this failed
    # the 2026-11-15 03:20 timer run with "extra characters after command").
    grep -v "^$today|$p|" "$file" >"$file.tmp" && mv "$file.tmp" "$file"
  fi
  printf '%s|%s|%s|%s|%s\n' "$today" "$p" "$seq" "$hash" "$chain" >>"$file"

  psql_db -c \
    "insert into audit_anchors (day, project_id, last_seq, hash, external_ref)
     values ('$today', (select id from projects where code='$p'), $seq, '$hash',
             '$file')
     on conflict (day, project_id) do update
       set last_seq=excluded.last_seq, hash=excluded.hash,
           external_ref=excluded.external_ref" >/dev/null
  echo "$p: anchored $today seq=$seq hash=${hash:0:22}... -> $file"
done

exit "$rc"
