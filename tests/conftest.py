import sys
from pathlib import Path

import pytest

from data_agent.api import limiter

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _reset_ask_rate_limiter() -> None:
    """Zera o estado do rate limiter (``slowapi``) de ``data_agent.api`` a cada teste.

    ``data_agent.api.app`` é um singleton de módulo reusado por todos os testes de
    ``test_api.py`` (via ``TestClient(api.app)``) — sem isto, o limite de
    ``_ASK_RATE_LIMIT`` (5/minuto por IP) se acumularia entre testes que chamam
    ``POST /ask`` e derrubaria testes depois do quinto com 429, mesmo sem nenhuma
    relação com o que cada teste individual está verificando.
    """
    limiter.reset()
