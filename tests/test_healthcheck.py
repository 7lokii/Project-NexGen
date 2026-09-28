"""Health check externo (`healthcheck.py`) — deteção offline, restart e recovery.

Corre a lógica real contra uma **porta isolada sem servidor** (porta livre
escolhida em runtime) e controla apenas o `_restart_server` com monkeypatch
— nunca arranca uvicorn de verdade nem toca no servidor de produção.

Contratos verificados:
  - 1 falha isolada NÃO age: regista ("falha 1/N") mas não reinicia nem alerta;
  - 2 falhas consecutivas alertam (log) e reiniciam;
  - cooldown entre restarts (min_interval) impede tentativas consecutivas;
  - recuperação limpa o estado e notifica "servidor de volta";
  - `--no-restart` nunca reinicia (só alerta);
  - processo encravado na porta: é terminado antes de subir o novo.
"""

import importlib.util
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_hc(tmp_path, monkeypatch):
    """Importa healthcheck.py isolado, com ficheiros de estado/log em tmp."""
    spec = importlib.util.spec_from_file_location(
        "healthcheck_under_test", ROOT / "healthcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "healthcheck.state")
    monkeypatch.setattr(mod, "LOG", tmp_path / "healthcheck.log")
    # sem Telegram nos testes — garante que _notify só regista no log
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    return mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _state(mod) -> dict:
    try:
        return json.loads(mod.STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log_text(mod) -> str:
    return mod.LOG.read_text(encoding="utf-8", errors="replace")


def test_servidor_offline_so_age_apos_2_falhas_consecutivas(tmp_path, monkeypatch):
    """1 falha = só registo; a 2ª falha alerta e reinicia (porta morta real)."""
    mod = _load_hc(tmp_path, monkeypatch)
    port = _free_port()
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setattr(mod, "URL", f"http://127.0.0.1:{port}/")
    restarts: list = []
    monkeypatch.setattr(mod, "_restart_server", lambda: (restarts.append(1), True)[1])
    monkeypatch.setattr(mod, "_port_pid", lambda: None)

    # pass 1 — falha isolada: exit 1, sem restart, sem alerta OFFLINE
    assert mod._check(restart=True, min_interval=120) == 1
    assert restarts == []
    assert _state(mod).get("consecutive_failures") == 1
    assert "falha 1/2" in _log_text(mod)
    assert "OFFLINE" not in _log_text(mod)

    # pass 2 — 2ª falha consecutiva: alerta + restart
    assert mod._check(restart=True, min_interval=120) == 1
    assert restarts == [1]
    assert "OFFLINE" in _log_text(mod)
    assert _state(mod).get("consecutive_failures") == 2


def test_restart_fica_no_cooldown_entre_tentativas(tmp_path, monkeypatch):
    """Depois de reiniciar, uma nova passagem dentro do min_interval não repete."""
    mod = _load_hc(tmp_path, monkeypatch)
    port = _free_port()
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setattr(mod, "URL", f"http://127.0.0.1:{port}/")
    restarts: list = []
    monkeypatch.setattr(mod, "_restart_server", lambda: (restarts.append(1), True)[1])
    monkeypatch.setattr(mod, "_port_pid", lambda: None)

    assert mod._check(restart=True, min_interval=120) == 1  # falha 1
    assert mod._check(restart=True, min_interval=120) == 1  # falha 2 -> restart
    assert restarts == [1]
    assert mod._check(restart=True, min_interval=120) == 1  # ainda down, em cooldown
    assert restarts == [1]  # sem nova tentativa dentro do intervalo
    assert "cooldown" in _log_text(mod)


def test_recuperacao_limpa_estado_e_notifica(tmp_path, monkeypatch):
    """Quando o servidor volta, o estado é limpo e há notificação de recovery."""
    mod = _load_hc(tmp_path, monkeypatch)
    port = _free_port()
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setattr(mod, "URL", f"http://127.0.0.1:{port}/")
    restarts: list = []
    monkeypatch.setattr(mod, "_restart_server", lambda: (restarts.append(1), True)[1])
    monkeypatch.setattr(mod, "_port_pid", lambda: None)

    assert mod._check(restart=True, min_interval=120) == 1  # falha 1
    assert mod._check(restart=True, min_interval=120) == 1  # falha 2 -> restart

    # o servidor "volta": o check limpa o estado e notifica
    monkeypatch.setattr(mod, "_server_ok", lambda: True)
    # zera o cooldown para o restart anterior não interferir na leitura
    st = _state(mod)
    st["down"] = True
    mod._save_state(st)
    assert mod._check(restart=True, min_interval=120) == 0
    assert _state(mod).get("down") is False
    assert _state(mod).get("consecutive_failures") == 0
    assert "servidor de volta" in _log_text(mod)


def test_no_restart_nunca_reinicia(tmp_path, monkeypatch):
    """`--no-restart` (restart=False) alerta mas nunca chama o restart."""
    mod = _load_hc(tmp_path, monkeypatch)
    port = _free_port()
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setattr(mod, "URL", f"http://127.0.0.1:{port}/")
    restarts: list = []
    monkeypatch.setattr(mod, "_restart_server", lambda: (restarts.append(1), True)[1])
    monkeypatch.setattr(mod, "_port_pid", lambda: None)

    assert mod._check(restart=False, min_interval=120) == 1  # falha 1
    assert mod._check(restart=False, min_interval=120) == 1  # falha 2 -> alerta
    assert mod._check(restart=False, min_interval=120) == 1  # falha 3 -> alerta
    assert restarts == []
    assert "OFFLINE" in _log_text(mod)


def test_processo_encravado_na_porta_e_terminado_antes_de_reiniciar(tmp_path, monkeypatch):
    """Porta ocupada sem resposta HTTP: mata o PID preso e depois reinicia."""
    mod = _load_hc(tmp_path, monkeypatch)
    port = _free_port()
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setattr(mod, "URL", f"http://127.0.0.1:{port}/")
    restarts: list = []
    killed: list = []
    monkeypatch.setattr(mod, "_restart_server", lambda: (restarts.append(1), True)[1])
    monkeypatch.setattr(mod, "_port_pid", lambda: 4242)  # processo encravado
    monkeypatch.setattr(mod, "_process_age_seconds", lambda pid: 99999.0)  # velho
    monkeypatch.setattr(mod, "subprocess",
                        type("FakeSP", (), {"run": staticmethod(
                            lambda *a, **k: killed.append(a[0]) or None)})())

    assert mod._check(restart=True, min_interval=120) == 1  # falha 1
    assert killed == []
    assert mod._check(restart=True, min_interval=120) == 1  # falha 2 -> ação
    assert killed and "taskkill" in killed[0] and "4242" in killed[0]
    assert restarts == [1]


def test_processo_jovem_na_porta_nao_e_morto_durante_o_boot(tmp_path, monkeypatch):
    """Porta ocupada por um processo recém-arrancado (< BOOT_GRACE): sem kill
    nesta passagem — o processo pode estar só a fazer o boot (lifespan liga o
    MT5 e a porta já escuta mas o HTTP ainda não responde). As falhas voltam
    a 1 e a próxima passagem (processo já mais velho) é que decide."""
    mod = _load_hc(tmp_path, monkeypatch)
    port = _free_port()
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setattr(mod, "URL", f"http://127.0.0.1:{port}/")
    restarts: list = []
    killed: list = []
    monkeypatch.setattr(mod, "_restart_server", lambda: (restarts.append(1), True)[1])
    monkeypatch.setattr(mod, "_port_pid", lambda: 4242)
    monkeypatch.setattr(mod, "_process_age_seconds", lambda pid: 42.0)  # jovem
    monkeypatch.setattr(mod, "subprocess",
                        type("FakeSP", (), {"run": staticmethod(
                            lambda *a, **k: killed.append(a[0]) or None)})())

    assert mod._check(restart=True, min_interval=120) == 1  # falha 1
    assert killed == []
    assert mod._check(restart=True, min_interval=120) == 1  # falha 2
    assert killed == []  # jovem → NUNCA morto nesta passagem
    assert restarts == []
    # falhas voltam a 1: o kill só acontece se a próxima passagem falhar com
    # o processo já fora da janela de boot
    assert _state(mod).get("consecutive_failures") == 1
    assert "a arrancar" in _log_text(mod)

    # processo entretanto envelheceu e continua sem responder → agora sim
    monkeypatch.setattr(mod, "_process_age_seconds", lambda pid: 99999.0)
    assert mod._check(restart=True, min_interval=120) == 1  # falha -> 2 de novo
    assert killed and "taskkill" in killed[0] and "4242" in killed[0]
    assert restarts == [1]


def test_servidor_saudavel_e_silencioso(tmp_path, monkeypatch):
    """Servidor a responder: exit 0, sem ruído (não cria sequer o log)."""
    mod = _load_hc(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_server_ok", lambda: True)
    assert mod._check(restart=True, min_interval=120) == 0
    assert not mod.LOG.exists()  # saudável e silencioso: nada foi escrito
    assert _state(mod).get("down") in (None, False)