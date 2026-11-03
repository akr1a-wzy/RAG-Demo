"""检索模型运行设备选择。"""

from __future__ import annotations

import os


def resolve_torch_device(torch_module) -> str:
    requested = os.environ.get("RAG_DEVICE", "auto").lower()
    if requested not in {"auto", "cpu", "cuda"}:
        raise ValueError("RAG_DEVICE只允许auto、cpu或cuda")
    if requested == "cuda" and not torch_module.cuda.is_available():
        raise RuntimeError("RAG_DEVICE要求cuda，但当前PyTorch无法使用CUDA")
    if requested == "cpu":
        return "cpu"
    return "cuda" if torch_module.cuda.is_available() else "cpu"
