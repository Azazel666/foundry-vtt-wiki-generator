#!/bin/sh
# Generates the crontab from WIKI_SCHEDULE and hands over to supercronic.
set -eu

crontab=/tmp/wikigen.crontab
schedule="${WIKI_SCHEDULE:-0 1 * * *}"
if [ "$schedule" = "off" ]; then
  echo "# WIKI_SCHEDULE=off: runs are started by hand" > "$crontab"
else
  echo "$schedule wikigen run" > "$crontab"
fi

case "$(echo "${WIKI_RUN_ON_START:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) wikigen run & ;;
esac

exec supercronic -quiet "$crontab"
