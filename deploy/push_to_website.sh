#!/bin/bash
# Push the rendered calendar image to the public Apache host.
# Target comes from config/push.env: WEBSITE_HOST + WEBSITE_DEST.
set -e
cd /opt/eink-calendar-work
[ -f config/push.env ] && . config/push.env
: "${WEBSITE_HOST:?set WEBSITE_HOST in config/push.env}"
: "${WEBSITE_DEST:?set WEBSITE_DEST in config/push.env}"
KEY="$HOME/.ssh/website_push"
OPTS="-i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes"
DIR="$(dirname "$WEBSITE_DEST")"
# Ensure dir + disable inherited rewrite rules for this subtree (fixes the
# internal-redirect 500), copy the image, make it world-readable for Apache.
ssh $OPTS "$WEBSITE_HOST" "mkdir -p '$DIR' && chmod 755 '$DIR' && printf 'RewriteEngine Off\nOptions -Indexes\n' > '$DIR/.htaccess'"
scp -q $OPTS config/render.png "$WEBSITE_HOST:$WEBSITE_DEST"
ssh $OPTS "$WEBSITE_HOST" "chmod 644 '$WEBSITE_DEST'"
echo "pushed $(date -Is) -> $WEBSITE_HOST:$WEBSITE_DEST"
