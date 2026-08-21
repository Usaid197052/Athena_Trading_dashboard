import pytest

from agents.runtime_ollama import available


@pytest.mark.slow
def test_ollama_optional():
    if not available():
        pytest.skip("Ollama is not running")
    assert available() is True
