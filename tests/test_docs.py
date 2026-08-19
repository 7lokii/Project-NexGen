"""Regra de ouro do AGENTS.md: a documentação tem de mencionar o número real
de testes da suite. Se este teste falhar, a suite cresceu/mudou e o AGENTS.md
ficou desatualizado — corrige o número lá (e em docs/ se o citarem).
"""

import re
import subprocess
import sys
from pathlib import Path


def _suite_size() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True, text=True, timeout=180,
    )
    m = re.search(r"(\d+) tests? collected", out.stdout + out.stderr)
    assert m, f"não consegui contar os testes da suite: {out.stdout[-300:]!r}"
    return int(m.group(1))


def test_agents_md_menciona_o_numero_real_de_testes():
    md = Path("AGENTS.md")
    assert md.exists(), "AGENTS.md não encontrado na raiz do projeto"
    n = _suite_size()
    assert str(n) in md.read_text(encoding="utf-8"), (
        f"AGENTS.md não menciona os {n} testes atuais — a suite mudou sem "
        f"atualizar a documentação. Corrige o número em AGENTS.md."
    )


def test_ci_workflow_executa_suite_e_regras_documentais():
    """O workflow deve proteger tanto o código como as invariantes das docs.

    O job de failure-probe é opt-in no GitHub Actions: não torna o CI verde
    artificialmente, mas confirma manualmente que um pytest com falha produz
    exit code não-zero.
    """
    workflow = Path(".github/workflows/ci.yml")
    assert workflow.exists(), "workflow de CI não encontrado"
    text = workflow.read_text(encoding="utf-8")
    assert "pull_request:" in text and "push:" in text
    assert "python -m pytest -q" in text
    assert "tests/test_docs.py" in text
    assert "failure-probe:" in text
    assert "failure_probe" in text
    assert "ci_failure_probe_test.py" in text
    assert ".ci_failure_probe_test.py" not in text
    assert "trap 'rm -f ci_failure_probe_test.py' EXIT" in text
    assert "set +e" not in text
