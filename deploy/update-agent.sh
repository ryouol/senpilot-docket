#!/usr/bin/env bash
set -euo pipefail
umask 077

image=ghcr.io/ryouol/senpilot-docket
state=/var/lib/docket-deploy
data=/var/lib/docket
env_file=/etc/docket/docket.env
[[ $EUID == 0 && $(stat -c '%u:%a' "$env_file") == 0:600 ]] || exit 1
install -d -m 700 "$state"
exec 9>"$state/update.lock"
flock -n 9 || exit 0

exists() { docker container inspect "$1" >/dev/null 2>&1; }
running() { [[ $(docker inspect --format '{{.State.Running}}' "$1") == true ]]; }
previous_run=0
previous_digest=
if [[ -f $state/current ]]; then
    read -r previous_run previous_digest < "$state/current"
    [[ $previous_run =~ ^[1-9][0-9]{0,9}$ ]] || exit 1
    [[ $previous_digest =~ ^ghcr\.io/ryouol/senpilot-docket@sha256:[a-f0-9]{64}$ ]] || exit 1
fi
# Recover a replacement interrupted before its successful poll was recorded.
if exists docket-previous; then
    if exists docket; then
        if [[ $(docker inspect --format '{{.Config.Image}}' docket) == "$previous_digest" ]]; then
            if running docket-previous; then exit 1; fi
            docker rm docket-previous
        else
            docker stop --time 240 docket >/dev/null
            docker rm docket >/dev/null
        fi
    fi
    if exists docket-previous; then
        docker rename docket-previous docket
        docker start docket >/dev/null
    fi
fi
if exists docket-next; then
    if running docket-next; then exit 1; fi
    docker rm docket-next
fi

docker pull --platform linux/amd64 "$image:current"
digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image:current")
[[ $digest =~ ^ghcr\.io/ryouol/senpilot-docket@sha256:[a-f0-9]{64}$ ]] || exit 1
source=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$digest")
revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$digest")
run=$(docker image inspect --format '{{index .Config.Labels "io.docket.run"}}' "$digest")
[[ $source == https://github.com/ryouol/senpilot-docket ]] || exit 1
[[ $revision =~ ^[a-f0-9]{40}$ && $run =~ ^[1-9][0-9]{0,9}$ ]] || exit 1
if [[ $digest == "$previous_digest" || $run -le $previous_run ]]; then
    exit 0
fi

docker run --rm --network none --user 1001:1001 "$digest" python -c '
import sys
from pathlib import Path
import docket, docket.agent
from docket.main import main
installed = Path(docket.__file__).parent
for source in Path("/app/src/docket").glob("*.py"):
    assert source.read_bytes() == (installed / source.name).read_bytes(), source.name
sys.argv = ["docket", "--help"]
main()
'

install -d -m 700 -o 1001 -g 1001 "$data"
docker create --name docket-next --restart unless-stopped \
    --stop-timeout 240 --memory 768m --memory-swap 1536m --shm-size 128m \
    --log-opt max-size=10m --log-opt max-file=3 \
    --env-file "$env_file" --env DATA_DIR=/data \
    --mount "type=bind,source=$data,target=/data" "$digest" >/dev/null

switched=false
# Invoked by the EXIT trap.
# shellcheck disable=SC2317,SC2329
rollback() {
    code=$?
    if [[ $code != 0 ]]; then
        if $switched && exists docket; then
            docker stop --time 240 docket >/dev/null || true
            docker rm docket >/dev/null || true
        fi
        if exists docket-previous; then
            docker rename docket-previous docket
            docker start docket >/dev/null
        elif ! $switched && exists docket; then
            docker start docket >/dev/null
        fi
        echo 'Deployment failed; retained the previous worker where available.' >&2
    fi
}
trap rollback EXIT
if exists docket; then
    docker stop --time 240 docket >/dev/null
    docker rename docket docket-previous
fi
docker rename docket-next docket
switched=true
started=$(date +%s.%N)
docker start docket >/dev/null
deadline=$(($(date +%s) + 300))
while running docket; do
    if python3 - "$data/heartbeat.json" "$started" <<'PY'
import json
import sys
from datetime import datetime
try:
    with open(sys.argv[1]) as stream:
        heartbeat = json.load(stream)
    assert all(datetime.fromisoformat(heartbeat[key]).timestamp() >= float(sys.argv[2])
               for key in ("at", "last_poll"))
except (OSError, ValueError, KeyError, TypeError, AssertionError):
    sys.exit(1)
PY
    then
        printf '%s %s\n' "$run" "$digest" > "$state/current.tmp"
        mv "$state/current.tmp" "$state/current"
        trap - EXIT
        if exists docket-previous; then docker rm docket-previous >/dev/null; fi
        docker image prune --force --all --filter "label=org.opencontainers.image.source=$source" --filter until=24h
        echo "Deployed $revision ($digest)."
        exit 0
    fi
    [[ $(date +%s) -lt $deadline ]] || break
    sleep 5
done
exit 1
