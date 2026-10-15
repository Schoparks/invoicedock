#!/bin/zsh
cd "${0:A:h}"
if curl --silent --fail 'http://127.0.0.1:8765/api/auth/status' >/dev/null 2>&1; then
  open 'http://127.0.0.1:8765'
  exit 0
fi
CONDA_BIN="$(command -v conda)"
if [[ -z "$CONDA_BIN" ]]; then
  for candidate in "$HOME/anaconda3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda"; do
    if [[ -x "$candidate" ]]; then CONDA_BIN="$candidate"; break; fi
  done
fi
if [[ -z "$CONDA_BIN" ]]; then
  echo "未找到 Conda，请按 README.md 安装环境。"
  read -k 1
  exit 1
fi
(sleep 2; open 'http://127.0.0.1:8765') &
"$CONDA_BIN" run --no-capture-output -n invoice-desk python run.py
