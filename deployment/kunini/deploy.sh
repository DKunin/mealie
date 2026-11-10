#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(git -C "${app_dir}" rev-parse --show-toplevel)"
data_dir="${MEALIE_DATA_DIR:-${repo_dir}/data}"

for command in curl docker git; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        printf 'Required command is missing: %s\n' "${command}" >&2
        exit 1
    fi
done

install -d -m 0700 "${data_dir}"
export MEALIE_DATA_DIR="${data_dir}"

cd "${app_dir}"
docker compose config --quiet
docker compose pull --quiet mealie
docker compose up -d --remove-orphans

for _ in $(seq 1 90); do
    health="$(docker inspect mealie --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || true)"
    if [[ "${health}" == "healthy" ]]; then
        curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:9001/api/app/about >/dev/null
        printf 'Mealie is healthy on 127.0.0.1:9001.\n'
        exit 0
    fi
    sleep 2
done

docker compose ps
docker compose logs --tail 100 mealie >&2
echo "Mealie did not become healthy in time." >&2
exit 1
