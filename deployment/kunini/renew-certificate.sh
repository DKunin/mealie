#!/usr/bin/env bash
set -euo pipefail

certbot renew "$@" \
    --cert-name meal.kunini.ru \
    --config-dir /home/dekunin/letsencrypt \
    --work-dir /home/dekunin/letsencrypt-work \
    --logs-dir /home/dekunin/letsencrypt-logs \
    --deploy-hook 'sudo -n /usr/local/sbin/codex-nginx-apply meal.kunini.ru.conf'
