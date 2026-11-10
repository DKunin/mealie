#!/usr/bin/env bash
set -euo pipefail

domain="meal.kunini.ru"
expected_image="${MEALIE_IMAGE:-ghcr.io/mealie-recipes/mealie:v3.30.0@sha256:90f83da6f3ab0a421efc827118a556168573930105f13fa3321768f42163d784}"
temporary_dir="$(mktemp -d)"

cleanup() {
    find "${temporary_dir}" -depth -delete
}
trap cleanup EXIT

curl --noproxy '*' --fail --silent --show-error \
    http://127.0.0.1:9001/api/app/about >"${temporary_dir}/about.json"
jq -e '.version | contains("3.30.0")' "${temporary_dir}/about.json" >/dev/null
jq -e '.enableOidc == true and .oidcRedirect == true and .oidcProviderName == "Kunini" and .allowPasswordLogin == false' \
    "${temporary_dir}/about.json" >/dev/null

health="$(docker inspect mealie --format '{{.State.Health.Status}}')"
published_port="$(docker port mealie 9000/tcp)"
configured_image="$(docker inspect mealie --format '{{.Config.Image}}')"
data_mount_rw="$(docker inspect mealie --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.RW}}{{end}}{{end}}')"
no_new_privileges="$(docker inspect mealie --format '{{.HostConfig.SecurityOpt}}')"

if [[ "${health}" != "healthy" || "${published_port}" != "127.0.0.1:9001" \
    || "${configured_image}" != "${expected_image}" || "${data_mount_rw}" != "true" \
    || "${no_new_privileges}" != *no-new-privileges:true* ]]; then
    echo "Container health, listener, image, data mount, or privilege verification failed." >&2
    exit 1
fi

docker exec mealie python -c \
    'from mealie.lang.locale_config import LOCALE_CONFIG; assert "ru-RU" in LOCALE_CONFIG' >/dev/null

http_status="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    --resolve "${domain}:80:127.0.0.1" "http://${domain}/")"
https_status="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    --resolve "${domain}:443:127.0.0.1" "https://${domain}/")"
anonymous_status="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    --resolve "${domain}:443:127.0.0.1" "https://${domain}/api/users/self")"
locale_cookie="$(curl --noproxy '*' --silent --head --resolve "${domain}:443:127.0.0.1" \
    "https://${domain}/" | tr -d '\r' | awk 'tolower($1) == "set-cookie:" && $2 ~ /^i18n_redirected=ru-RU/ { print $2 }')"
curl --noproxy '*' --silent --show-error --dump-header "${temporary_dir}/oidc-headers.txt" --output /dev/null \
    http://127.0.0.1:9001/api/auth/oauth
oidc_location="$(tr -d '\r' <"${temporary_dir}/oidc-headers.txt" | sed -n 's/^[Ll]ocation: //p' | tail -n 1)"

openssl s_client -connect 127.0.0.1:443 -servername "${domain}" </dev/null 2>/dev/null \
    | openssl x509 -noout -ext subjectAltName >"${temporary_dir}/certificate.txt"

if [[ "${http_status}" != "301" || "${https_status}" != "200" \
    || "${anonymous_status}" != "401" || -z "${locale_cookie}" \
    || "${oidc_location}" != https://auth.kunini.ru/oidc/authorize* \
    || "${oidc_location}" != *"client_id=mealie"* \
    || "${oidc_location}" != *"redirect_uri=https%3A%2F%2Fmeal.kunini.ru%2Flogin"* ]] \
    || ! grep -Fq "DNS:${domain}" "${temporary_dir}/certificate.txt"; then
    echo "Public redirect, TLS, native authentication, or Russian-locale verification failed." >&2
    exit 1
fi

printf 'Verified %s: Mealie 3.30.0, TLS, Kunini OIDC-only auth, Russian default, pinned image, loopback listener, and persistent data.\n' \
    "${domain}"
