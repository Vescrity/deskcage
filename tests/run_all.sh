#!/bin/bash
# Run every deskcage test.  Each test gets its own temp XDG dirs and its own
# private session bus, so the developer's session is never touched.
set -u
cd "$(dirname "$0")"

status=0
for test in test_*.py; do
    printf '%-24s ' "$test"
    if timeout 240 python3 "$test" >"/tmp/opencode/deskcage-$test.log" 2>&1; then
        echo PASS
    else
        echo FAIL
        sed 's/^/    /' "/tmp/opencode/deskcage-$test.log" | tail -60
        status=1
    fi
done
exit "$status"
