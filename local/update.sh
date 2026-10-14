#!/usr/bin/env bash
# Back up the database, pull newer upstream images, restart, verify.
# This is the whole update procedure: no merge, no build.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/invidious}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/invidious-$STAMP.sql.gz"

[ -f .env ] || { echo "missing .env (copy .env.example)" >&2; exit 1; }
mkdir -p "$BACKUP_DIR"

echo "== backing up postgres -> $OUT"
docker compose exec -T invidious-db pg_dump -U invidious -d invidious | gzip > "$OUT"

# A gzipped dump that expands to nothing means pg_dump failed with a truncated
# pipeline, so do not treat the file as a backup.
BYTES="$(gzip -dc "$OUT" | wc -c)"
if [ "${BYTES:-0}" -lt 100 ]; then
	echo "backup expanded to ${BYTES} bytes; aborting before touching the stack" >&2
	exit 1
fi
echo "   ${BYTES} bytes of SQL"

echo "== pulling images"
docker compose pull invidious companion

echo "== restarting"
docker compose up -d

echo "== waiting for the stack"
ok=""
for _ in $(seq 1 45); do
	if curl -fsS -o /dev/null http://127.0.0.1:3080/css/default.css; then
		ok=1
		break
	fi
	sleep 2
done
if [ -z "$ok" ]; then
	echo "Invidious did not answer on 127.0.0.1:3080" >&2
	docker compose ps
	exit 1
fi

echo "== health"
curl -fsS http://127.0.0.1:3080/x/health
echo
docker compose ps
echo "done. backup: $OUT"
