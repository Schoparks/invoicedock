#!/usr/bin/env bash
# Run with the existing project Conda environment; never start a second server.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${INVOICEDOCK_PYTHON:-}" ]]; then
    exec "$INVOICEDOCK_PYTHON" "$script_dir/update.py" "$@"
fi
if ! command -v conda >/dev/null 2>&1; then
    echo '未找到 Conda。请将 Conda 加入 PATH，或设置 INVOICEDOCK_PYTHON 为 invoice-desk 环境中的 Python 绝对路径。' >&2
    exit 1
fi
exec conda run --no-capture-output -n invoice-desk python "$script_dir/update.py" "$@"
