"""Agente de automação: o Claude executa comandos no terminal, com sua aprovação.

Uso:
    python agent.py                       # modo conversa
    python agent.py "sua tarefa"          # uma tarefa só

Variáveis opcionais (no .env):
    AGENTE_DIR=/caminho/da/pasta          # pasta onde os comandos começam (padrão: pasta atual)
    AGENTE_AUTO_APROVAR_LEITURA=1         # roda sem perguntar comandos só de leitura (ls, cat, ...)
"""

import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-5-5"
PASTA_INICIAL = Path(os.getenv("AGENTE_DIR", os.getcwd())).expanduser().resolve()
AUTO_LEITURA = os.getenv("AGENTE_AUTO_APROVAR_LEITURA") == "1"
TIMEOUT_S = 120          # tempo máximo por comando
MAX_SAIDA = 20_000       # caracteres de saída devolvidos ao Claude
LOG = Path(__file__).parent / "agente.log"

SYSTEM = f"""Você é um agente de automação que trabalha no computador do usuário pelo terminal (bash).
Responda em português.

- Pasta de trabalho inicial: {PASTA_INICIAL}
- Sistema: {sys.platform}
- Cada comando passa pela aprovação do usuário, que pode recusar. Se recusar, não insista no
  mesmo comando: pergunte o que ele prefere.
- Antes de mudar ou apagar algo, investigue com comandos de leitura (ls, cat, find...).
- Prefira comandos não interativos (o terminal não aceita digitação durante o comando).
  Cada comando tem limite de {TIMEOUT_S}s.
- Ao terminar, diga em poucas linhas o que foi feito e o que ficou pendente."""

# Ferramenta de terminal oficial da Anthropic: o schema já vem embutido no modelo.
TOOLS = [{"type": "bash_20250124", "name": "bash"}]

# Comandos só de leitura que podem rodar sem perguntar (se AGENTE_AUTO_APROVAR_LEITURA=1).
LEITURA = {"ls", "pwd", "cat", "head", "tail", "wc", "find", "grep", "echo", "date",
           "whoami", "which", "file", "stat", "du", "df", "tree", "uname"}
OPERADORES = re.compile(r"[;&|<>`$\n]|\bsudo\b")


# ---------- Execução dos comandos ----------

class Terminal:
    """Roda cada comando em bash e lembra a pasta atual entre comandos (para `cd` funcionar)."""

    MARCA = "__AGENTE_CWD__"

    def __init__(self) -> None:
        self.cwd = PASTA_INICIAL

    def reiniciar(self) -> str:
        self.cwd = PASTA_INICIAL
        return f"Terminal reiniciado em {self.cwd}"

    def rodar(self, comando: str) -> str:
        script = f'{comando}\n__rc=$?\necho "{self.MARCA}$(pwd)"\nexit $__rc'
        try:
            p = subprocess.run(
                ["bash", "-c", script], cwd=self.cwd, capture_output=True, text=True,
                timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"Erro: o comando passou de {TIMEOUT_S}s e foi interrompido."
        saida = p.stdout
        if self.MARCA in saida:
            saida, _, nova = saida.rpartition(self.MARCA)
            if Path(nova.strip()).is_dir():
                self.cwd = Path(nova.strip())
        texto = (saida + p.stderr).strip() or "(sem saída)"
        if len(texto) > MAX_SAIDA:
            texto = texto[:MAX_SAIDA // 2] + "\n...[saída cortada]...\n" + texto[-MAX_SAIDA // 2:]
        return f"{texto}\n[código de saída: {p.returncode}]"


def eh_leitura(comando: str) -> bool:
    if OPERADORES.search(comando):
        return False
    try:
        partes = shlex.split(comando)
    except ValueError:
        return False
    return bool(partes) and partes[0] in LEITURA


def aprovar(comando: str, cwd: Path) -> tuple[bool, str]:
    """Pergunta ao usuário. Devolve (aprovado, motivo_se_recusado)."""
    if AUTO_LEITURA and eh_leitura(comando):
        print(f"\n  ▶ {comando}   (leitura, aprovado automaticamente)")
        return True, ""
    print(f"\n  📂 {cwd}\n  ▶ {comando}")
    while True:
        r = input("  Executar? [s]im / [n]ão / [m]otivo para recusar: ").strip().lower()
        if r in {"s", "sim", "y"}:
            return True, ""
        if r in {"n", "nao", "não", ""}:
            return False, "O usuário recusou este comando."
        if r in {"m", "motivo"}:
            return False, "O usuário recusou este comando: " + input("  Motivo: ").strip()


def registrar(comando: str, resultado: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] $ {comando}\n{resultado}\n")


# ---------- Loop do agente ----------

client = anthropic.Anthropic()
terminal = Terminal()


def executar_ferramenta(block) -> dict:
    entrada = block.input or {}
    if entrada.get("restart"):
        return {"type": "tool_result", "tool_use_id": block.id, "content": terminal.reiniciar()}
    comando = str(entrada.get("command", "")).strip()
    if not comando:
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": "Erro: comando vazio.", "is_error": True}
    ok, motivo = aprovar(comando, terminal.cwd)
    if not ok:
        registrar(comando, "(recusado)")
        return {"type": "tool_result", "tool_use_id": block.id, "content": motivo, "is_error": True}
    resultado = terminal.rodar(comando)
    registrar(comando, resultado)
    previa = resultado if len(resultado) < 800 else resultado[:800] + " ..."
    print("  " + previa.replace("\n", "\n  "))
    return {"type": "tool_result", "tool_use_id": block.id, "content": resultado}


def rodar(messages: list, max_passos: int = 40) -> None:
    """Roda o agente até ele terminar. `messages` é atualizado no lugar."""
    for _ in range(max_passos):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "high"},
            # Se o modelo recusar por política, a API tenta outro modelo automaticamente.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\nClaude: {block.text}")

        if response.stop_reason == "refusal":
            print("\n(O modelo recusou este pedido.)")
            return
        if response.stop_reason == "max_tokens":
            print("\n(Resposta cortada: limite de tokens atingido.)")
            return
        if response.stop_reason != "tool_use":
            return

        # Todos os resultados vão juntos numa única mensagem.
        resultados = [executar_ferramenta(b) for b in response.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": resultados})

    print("\n(Parei: número máximo de passos atingido. Peça para continuar, se quiser.)")


def main() -> None:
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Falta a chave: copie .env.example para .env e preencha ANTHROPIC_API_KEY.")

    historico: list = []

    def enviar(texto: str) -> bool:
        tamanho = len(historico)
        historico.append({"role": "user", "content": texto})
        try:
            rodar(historico)
            return True
        except anthropic.AuthenticationError:
            print("Erro: chave de API inválida. Confira o arquivo .env.")
        except anthropic.RateLimitError:
            print("Limite de requisições atingido; tente de novo em instantes.")
        except anthropic.APIStatusError as e:
            print(f"Erro da API ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            print("Erro de conexão com a API.")
        del historico[tamanho:]  # descarta a rodada que falhou
        return False

    if len(sys.argv) > 1:
        enviar(" ".join(sys.argv[1:]))
        return

    print(f"Agente de automação pronto (pasta: {PASTA_INICIAL}). Diga a tarefa ou 'sair'.")
    while True:
        try:
            texto = input("\nVocê: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if texto.lower() in {"sair", "exit", "quit"}:
            break
        if texto:
            enviar(texto)


if __name__ == "__main__":
    main()
