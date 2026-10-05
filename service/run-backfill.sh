#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ -f "$ROOT/.env" ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            ''|'#'*) continue ;;
            *[!A-Za-z0-9_]*)
                echo "Invalid environment key in $ROOT/.env: $key" >&2
                exit 1
                ;;
        esac
        export "$key=$value"
    done < "$ROOT/.env"
fi

if [ -z "${LLM_API_KEY:-}" ]; then
    echo "LLM_API_KEY is not set in $ROOT/.env" >&2
    exit 1
fi

mkdir -p "$ROOT/var"
docker compose -f "$ROOT/docker-compose.yml" up -d cognee

attempt=0
until curl -fsS --max-time 10 "${COGNEE_API_URL:-http://localhost:8010}/health" >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "Cognee did not become healthy within five minutes" >&2
        exit 1
    fi
    sleep 10
done

exec python3 "$ROOT/backfill.py" \
    --dataset "${COGNEE_DATASET:-knowledge_base}" \
    --concurrency "${DISTILL_CONCURRENCY:-4}" \
    --add-batch-size "${COGNEE_ADD_BATCH_SIZE:-100}" \
    --data-per-batch "${COGNEE_DATA_PER_BATCH:-4}" \
    --chunks-per-batch "${COGNEE_CHUNKS_PER_BATCH:-12}" \
    --improve
