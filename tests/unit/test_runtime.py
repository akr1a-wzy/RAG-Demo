import pytest

from app.retrieval.runtime import resolve_torch_device


class _Cuda:
    def __init__(self, available):
        self.available = available

    def is_available(self):
        return self.available


class _Torch:
    def __init__(self, available):
        self.cuda = _Cuda(available)


def test_device_auto_selects_cuda_when_available(monkeypatch) -> None:
    monkeypatch.delenv("RAG_DEVICE", raising=False)
    assert resolve_torch_device(_Torch(True)) == "cuda"
    assert resolve_torch_device(_Torch(False)) == "cpu"


def test_device_override_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("RAG_DEVICE", "cpu")
    assert resolve_torch_device(_Torch(True)) == "cpu"
    monkeypatch.setenv("RAG_DEVICE", "cuda")
    with pytest.raises(RuntimeError):
        resolve_torch_device(_Torch(False))
    monkeypatch.setenv("RAG_DEVICE", "invalid")
    with pytest.raises(ValueError):
        resolve_torch_device(_Torch(True))
