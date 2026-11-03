#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
export UV_CACHE_DIR="$PWD/.uv-cache"
export HF_HOME="$PWD/data/models/.hub"
export HF_HUB_DISABLE_TELEMETRY=1
export HF_HUB_DISABLE_XET=1
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$PWD/.cloud-tools/python}"
export UV_PYTHON_BIN_DIR="${UV_PYTHON_BIN_DIR:-$PWD/.cloud-tools/bin}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$PWD/.cloud-tools/cache}"
mkdir -p "$UV_PYTHON_INSTALL_DIR" "$UV_PYTHON_BIN_DIR" "$XDG_CACHE_HOME"
if ! command -v uv >/dev/null; then
  python3 -m pip install uv
fi
uv python install 3.11
# Preserve the dependency lock but use the same Torch version's CPU build on Linux.
mapfile -t gpu_packages < <(uv run --no-project --python 3.11 python -c 'import tomllib; d=tomllib.load(open("uv.lock","rb")); print("\n".join(p["name"] for p in d["package"] if p["name"].startswith(("nvidia-", "cuda-")) or p["name"] in ("torch", "triton")))')
skip=()
for package in "${gpu_packages[@]}"; do skip+=(--no-install-package "$package"); done
uv sync --locked --python 3.11 "${skip[@]}"
torch_version=$(uv run --no-sync python -c 'import tomllib; d=tomllib.load(open("uv.lock","rb")); print(next(p["version"] for p in d["package"] if p["name"]=="torch"))')
uv pip install --python .venv/bin/python --no-deps --index-url https://download.pytorch.org/whl/cpu "torch==$torch_version"
uv pip check --python .venv/bin/python
if [ -f data/chroma/index_manifest.json ]; then
  uv run --no-sync python scripts/cloud_bootstrap.py --inputs-only --offline
else
  uv run --no-sync python scripts/cloud_bootstrap.py --offline
fi
uv run --no-sync pytest -q
uv run --no-sync ruff check src scripts/cloud_bootstrap.py scripts/package_cloud_state.py scripts/review_table_pages.py
uv run --no-sync python scripts/search_documents.py '贵州茅台2025年的营业收入是多少？' --top-k 3 --mode hybrid
