#!/bin/sh
set -eu

exec /usr/bin/tini -s -- /usr/local/bin/jenkins.sh "$@"
