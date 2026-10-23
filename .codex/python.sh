#!/usr/bin/env bash
set -eu
export PYTHONDONTWRITEBYTECODE=1
project_root="$(cd "$(dirname "$0")/.." && pwd)"
lock_key="$(shasum -a 256 "$project_root/requirements-dev.lock" | cut -c 1-16)"
shared_environment="${XDG_CACHE_HOME:-$HOME/.cache}/oa-minimal/python311-$(uname -m)-$lock_key"
if [ "${1:-}" = setup ]; then
    if [ ! -x "$shared_environment/bin/python" ]; then
        uv venv --python 3.11 "$shared_environment"
    fi
    uv pip sync --python "$shared_environment/bin/python" --link-mode hardlink "$project_root/requirements-dev.lock"
    configured_hooks="$(git -C "$project_root" config --get core.hooksPath || true)"
    if [ -z "$configured_hooks" ] || [ "$configured_hooks" = .githooks ]; then
        git -C "$project_root" config --local core.hooksPath .githooks
    else
        printf 'Existing hooksPath retained: %s\n' "$configured_hooks" >&2
    fi
    printf '%s\n' "$shared_environment/bin/python"
elif [ "${1:-}" = check ]; then
    cd "$project_root"
    "$shared_environment/bin/python" -B .codex/share_check.py
    "$shared_environment/bin/python" -B -m ruff check apps tests
    exec "$shared_environment/bin/python" -B -m pytest -q
else
    exec "$shared_environment/bin/python" -B "$@"
fi
