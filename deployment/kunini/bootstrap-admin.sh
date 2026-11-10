#!/usr/bin/env bash
set -euo pipefail

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(git -C "${app_dir}" rev-parse --show-toplevel)"
secrets_dir="${repo_dir}/secrets"
password_file="${secrets_dir}/admin-password"
base_url="http://127.0.0.1:9001"
admin_username="${MEALIE_ADMIN_USERNAME:-dkunin}"
admin_email="${MEALIE_ADMIN_EMAIL:-dkunin@kunini.ru}"
admin_full_name="${MEALIE_ADMIN_FULL_NAME:-Dmitri Kunin}"
temporary_dir="$(mktemp -d)"

cleanup() {
    find "${temporary_dir}" -depth -delete
}
trap cleanup EXIT

for command in curl jq openssl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        printf 'Required command is missing: %s\n' "${command}" >&2
        exit 1
    fi
done

install -d -m 0700 "${secrets_dir}"
if [[ ! -s "${password_file}" ]]; then
    umask 077
    openssl rand -base64 36 | tr -d '\n' >"${password_file}"
    printf '\n' >>"${password_file}"
fi
chmod 0600 "${password_file}"

new_password="$(tr -d '\r\n' <"${password_file}")"
token_file="${temporary_dir}/token.json"

login() {
    local username="$1"
    local password="$2"
    local status
    status="$(curl --noproxy '*' --silent --show-error --output "${token_file}" --write-out '%{http_code}' \
        --request POST \
        --data-urlencode "username=${username}" \
        --data-urlencode "password=${password}" \
        --data-urlencode 'remember_me=false' \
        "${base_url}/api/auth/token")"
    [[ "${status}" == "200" ]]
}

if login "${admin_username}" "${new_password}"; then
    printf 'Mealie administrator is already initialized; credentials remain in %s.\n' "${password_file}"
    exit 0
fi

password_needs_change="false"
if login 'changeme@example.com' "${new_password}"; then
    :
elif login 'changeme@example.com' 'MyPassword'; then
    password_needs_change="true"
else
    echo "Default Mealie credentials are unavailable and the stored administrator credentials did not work." >&2
    exit 1
fi

token="$(jq -er '.access_token' "${token_file}")"
auth_header="Authorization: Bearer ${token}"
self_file="${temporary_dir}/self.json"
update_file="${temporary_dir}/update.json"
password_change_file="${temporary_dir}/password-change.json"

curl --noproxy '*' --fail --silent --show-error --header "${auth_header}" \
    "${base_url}/api/users/self" >"${self_file}"

jq --arg username "${admin_username}" \
    --arg email "${admin_email}" \
    --arg full_name "${admin_full_name}" \
    '{
        id: .id,
        username: $username,
        fullName: $full_name,
        email: $email,
        authMethod: (.authMethod // "Mealie"),
        admin: true,
        group: .group,
        household: .household,
        advanced: true,
        showAnnouncements: (.showAnnouncements // true),
        lastReadAnnouncement: .lastReadAnnouncement,
        canInvite: (.canInvite // false),
        canManage: (.canManage // false),
        canManageHousehold: (.canManageHousehold // false),
        canOrganize: (.canOrganize // false)
    }' "${self_file}" >"${update_file}"

user_id="$(jq -er '.id' "${self_file}")"
if [[ "${password_needs_change}" == "true" ]]; then
    jq -n --arg current_password 'MyPassword' --arg new_password "${new_password}" \
        '{currentPassword: $current_password, newPassword: $new_password}' >"${password_change_file}"
    curl --noproxy '*' --fail --silent --show-error --request PUT \
        --header "${auth_header}" \
        --header 'Content-Type: application/json' \
        --data-binary "@${password_change_file}" \
        "${base_url}/api/users/password" >/dev/null

    if ! login 'changeme@example.com' "${new_password}"; then
        echo "The new administrator password could not be verified." >&2
        exit 1
    fi
    token="$(jq -er '.access_token' "${token_file}")"
    auth_header="Authorization: Bearer ${token}"
fi

curl --noproxy '*' --fail --silent --show-error --request PUT \
    --header "${auth_header}" \
    --header 'Content-Type: application/json' \
    --data-binary "@${update_file}" \
    "${base_url}/api/users/${user_id}" >/dev/null

if ! login "${admin_username}" "${new_password}"; then
    echo "The new administrator credentials could not be verified." >&2
    exit 1
fi

default_status="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    --request POST \
    --data-urlencode 'username=changeme@example.com' \
    --data-urlencode 'password=MyPassword' \
    --data-urlencode 'remember_me=false' \
    "${base_url}/api/auth/token")"
if [[ "${default_status}" == "200" ]]; then
    echo "The default Mealie credentials are still active." >&2
    exit 1
fi

printf 'Initialized administrator %s with Russian household defaults; credentials are stored in %s.\n' \
    "${admin_username}" "${password_file}"
