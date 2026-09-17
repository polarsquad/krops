#!/bin/sh
# toolbox-entrypoint.sh – launcher for the krops toolbox image.
# See docs/bootstrap-cli.md ("Toolbox runtime contracts").
#
# Usage inside the image (set by the run invocation):
#   toolbox-entrypoint.sh [profile|--recreate|teardown ...]
set -eu

export KROPS_TOOLBOX=1

exec krops-bootstrap "$@"
