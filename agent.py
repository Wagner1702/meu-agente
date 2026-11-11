"""Agente de automação: o Claude usa o terminal e um navegador, com sua aprovação.

Uso:
    python agent.py                       # modo conversa
    python agent.py "sua tarefa"          # uma tarefa só

Configuração no .env (veja .env.example).
"""

import os
import re
import shlex
import shutil
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
AUTO_NAVEGADOR = os.getenv("AGENTE_AUTO_APROVAR_NAVEGADOR") == "1"
TIMEOUT_S = 120          # tempo máximo por comando
MAX_SAIDA = 20_000       # caracteres de saída devolvidos ao Claude
LOG = Path(__file__).parent / "agente.log"

# No Windows sem bash (Git Bash/WSL), os comandos rodam no PowerShell.
BASH = shutil.which("bash")
if os.name == "nt" and (not BASH or "system32" in BASH.lower()):
    BASH = None
SHELL_NOME = "bash" if BASH else "PowerShell"

# Credenciais do ERP: o agente as digita direto no campo; o Claude nunca vê os valores.
CREDENCIAIS = {"usuario": os.getenv("ERP_USUARIO", ""), "senha": os.getenv("ERP_SENHA", "")}

SYSTEM = f"""Você é um agente de automação que trabalha no computador do usuário.
Responda em português.

Ferramentas:
- `bash`: roda comandos no terminal. Neste computador o terminal é **{SHELL_NOME}**
  (sistema: {sys.platform}); escreva os comandos nessa linguagem. Pasta inicial: {PASTA_INICIAL}.
  Prefira comandos não interativos; cada um tem limite de {TIMEOUT_S}s.
- `navegador_*`: controla um navegador (Chromium). Depois de cada ação você recebe o texto da
  página e a lista numerada de elementos ([n]); use esses números para clicar/digitar. Os números
  mudam a cada atualização: use sempre os da lista mais recente. Se a lista não bastar para
  entender a tela (menus em imagem, ícones), use `navegador_captura` para ver a imagem.
- Para fazer login, use `navegador_digitar_credencial` (usuario/senha): você não sabe a senha e
  não deve pedir ao usuário que a digite no chat.

Regras:
- Cada ação passa pela aprovação do usuário. Se ele recusar, não insista: pergunte o que prefere.
- Antes de mudar ou apagar algo, investigue primeiro (comandos de leitura, olhar a tela).
- Não salve, exclua nem confirme registros no sistema sem o usuário ter pedido isso.
- Ao terminar, diga em poucas linhas o resultado (ex.: os dados encontrados) e o que ficou pendente."""


def _ferramenta(nome: str, descricao: str, props: dict, obrigatorios: list) -> dict:
    return {
        "name": nome, "description": descricao, "strict": True,
        "input_schema": {"type": "object", "properties": props,
                         "required": obrigatorios, "additionalProperties": False},
    }


_REF = {"type": "integer", "description": "Número do elemento na lista mais recente, ex.: 12"}
TOOLS = [
    # Ferramenta de terminal oficial da Anthropic: o schema já vem embutido no modelo.
    {"type": "bash_20250124", "name": "bash"},
    _ferramenta("navegador_abrir", "Abre uma URL no navegador e devolve a página.",
                {"url": {"type": "string"}}, ["url"]),
    _ferramenta("navegador_ver", "Devolve o texto e a lista de elementos da janela ativa.", {}, []),
    _ferramenta("navegador_captura", "Tira uma captura de tela da janela ativa.", {}, []),
    _ferramenta("navegador_clicar", "Clica num elemento.", {"ref": _REF}, ["ref"]),
    _ferramenta("navegador_digitar", "Apaga o conteúdo de um campo e digita um texto.",
                {"ref": _REF, "texto": {"type": "string"},
                 "enter": {"type": "boolean", "description": "Apertar Enter depois de digitar"}},
                ["ref", "texto", "enter"]),
    _ferramenta("navegador_digitar_credencial",
                "Digita o usuário ou a senha do ERP (guardados no computador) num campo.",
                {"ref": _REF, "campo": {"type": "string", "enum": ["usuario", "senha"]},
                 "enter": {"type": "boolean"}}, ["ref", "campo", "enter"]),
    _ferramenta("navegador_selecionar", "Escolhe uma opção (pelo texto) numa caixa de seleção.",
                {"ref": _REF, "opcao": {"type": "string"}}, ["ref", "opcao"]),
    _ferramenta("navegador_tecla", "Aperta uma tecla na página, ex.: Enter, Tab, Escape, F2.",
                {"tecla": {"type": "string"}}, ["tecla"]),
]

LEITURA = {"ls", "pwd", "cat", "head", "tail", "wc", "find", "grep", "echo", "date",
           "whoami", "which", "file", "stat", "du", "df", "tree", "uname", "dir", "type"}
OPERADORES = re.compile(r"[;&|<>`$\n]|\bsudo\b")


# ---------- Terminal ----------

class Terminal:
    """Roda cada comando e lembra a pasta atual entre comandos (para `cd` funcionar)."""

    MARCA = "__AGENTE_CWD__"

    def __init__(self) -> None:
        self.cwd = PASTA_INICIAL

    def reiniciar(self) -> str:
        self.cwd = PASTA_INICIAL
        return f"Terminal reiniciado em {self.cwd}"

    def rodar(self, comando: str) -> str:
        if BASH:
            script = f'{comando}\n__rc=$?\necho "{self.MARCA}$(pwd)"\nexit $__rc'
            args = [BASH, "-c", script]
        else:
            script = (f"{comando}\n$__rc = if ($?) {{ 0 }} else {{ 1 }}\n"
                      f'"{self.MARCA}$((Get-Location).Path)"\nexit $__rc')
            args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        try:
            p = subprocess.run(args, cwd=self.cwd, capture_output=True, text=True,
                               timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return f"Erro: o comando passou de {TIMEOUT_S}s e foi interrompido."
        saida = p.stdout or ""
        if self.MARCA in saida:
            saida, _, nova = saida.rpartition(self.MARCA)
            if Path(nova.strip()).is_dir():
                self.cwd = Path(nova.strip())
        texto = (saida + (p.stderr or "")).strip() or "(sem saída)"
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


# ---------- Aprovação e registro ----------

class Aprovacao:
    def __init__(self) -> None:
        self.tudo_nesta_tarefa = False  # resposta [t]: aprova o resto da tarefa atual

    def nova_tarefa(self) -> None:
        self.tudo_nesta_tarefa = False

    def pedir(self, descricao: str, automatico: bool = False) -> tuple[bool, str]:
        if automatico or self.tudo_nesta_tarefa:
            print(f"\n  ▶ {descricao}")
            return True, ""
        print(f"\n  ▶ {descricao}")
        while True:
            r = input("  Executar? [s]im / [n]ão / [t]udo nesta tarefa / [m]otivo para recusar: ")
            r = r.strip().lower()
            if r in {"s", "sim", "y"}:
                return True, ""
            if r in {"t", "tudo"}:
                self.tudo_nesta_tarefa = True
                return True, ""
            if r in {"n", "nao", "não", ""}:
                return False, "O usuário recusou esta ação."
            if r in {"m", "motivo"}:
                return False, "O usuário recusou esta ação: " + input("  Motivo: ").strip()


def registrar(acao: str, resultado: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {acao}\n{resultado[:3000]}\n")


# ---------- Execução das ferramentas ----------

client = anthropic.Anthropic()
terminal = Terminal()
aprovacao = Aprovacao()
_navegador = None


def navegador():
    global _navegador
    if _navegador is None:
        from navegador import Navegador  # só carrega o Playwright quando for usado
        _navegador = Navegador()
    return _navegador


def _descrever(nome: str, e: dict) -> str:
    if nome == "bash":
        return f"[{SHELL_NOME} em {terminal.cwd}] {e.get('command', '')}"
    if nome == "navegador_digitar_credencial":
        return f"navegador: digitar {e['campo']} do ERP no campo [{e['ref']}]"
    args = ", ".join(f"{k}={v!r}" for k, v in e.items())
    return f"{nome.replace('_', ': ', 1)}({args})"


def mascarar(texto: str) -> str:
    """Esconde usuário/senha do ERP de tudo que vai para o Claude e para o log."""
    for valor in CREDENCIAIS.values():
        if len(valor) >= 3:
            texto = texto.replace(valor, "***")
    return texto


def _resultado(block, conteudo, erro: bool = False) -> dict:
    r = {"type": "tool_result", "tool_use_id": block.id, "content": conteudo}
    if erro:
        r["is_error"] = True
    return r


def executar_ferramenta(block) -> dict:
    nome, e = block.name, (block.input or {})

    if nome == "bash" and e.get("restart"):
        return _resultado(block, terminal.reiniciar())
    if nome == "bash" and not str(e.get("command", "")).strip():
        return _resultado(block, "Erro: comando vazio.", True)

    somente_leitura = nome in {"navegador_ver", "navegador_captura"}
    automatico = (somente_leitura
                  or (nome == "bash" and AUTO_LEITURA and eh_leitura(e["command"]))
                  or (nome.startswith("navegador_") and AUTO_NAVEGADOR))
    descricao = _descrever(nome, e)
    ok, motivo = aprovacao.pedir(descricao, automatico)
    if not ok:
        registrar(descricao, "(recusado)")
        return _resultado(block, motivo, True)

    try:
        if nome == "bash":
            texto = terminal.rodar(e["command"])
        elif nome == "navegador_captura":
            png = navegador().captura()
            registrar(descricao, "(captura de tela)")
            return _resultado(block, [{"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": png}}])
        elif nome == "navegador_abrir":
            texto = navegador().abrir(e["url"])
        elif nome == "navegador_ver":
            texto = navegador().ver()
        elif nome == "navegador_clicar":
            texto = navegador().clicar(e["ref"])
        elif nome == "navegador_digitar":
            texto = navegador().digitar(e["ref"], e["texto"], e.get("enter", False))
        elif nome == "navegador_digitar_credencial":
            valor = CREDENCIAIS.get(e["campo"], "")
            if not valor:
                raise ValueError(f"ERP_{e['campo'].upper()} não está configurado no .env")
            texto = navegador().digitar(e["ref"], valor, e.get("enter", False))
        elif nome == "navegador_selecionar":
            texto = navegador().selecionar(e["ref"], e["opcao"])
        elif nome == "navegador_tecla":
            texto = navegador().tecla(e["tecla"])
        else:
            raise ValueError(f"ferramenta desconhecida: {nome}")
    except Exception as ex:
        erro = mascarar(f"Erro: {ex}")
        registrar(descricao, erro)
        print(f"  ✖ {erro}")
        return _resultado(block, erro, True)

    texto = mascarar(texto)
    registrar(descricao, texto)
    if nome == "bash":
        previa = texto if len(texto) < 800 else texto[:800] + " ..."
        print("  " + previa.replace("\n", "\n  "))
    return _resultado(block, texto)


# ---------- Loop do agente ----------

def rodar(messages: list, max_passos: int = 60) -> None:
    """Roda o agente até ele terminar. `messages` é atualizado no lugar."""
    for _ in range(max_passos):
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "high"},
            # Se o modelo recusar por política, a API tenta outro modelo automaticamente.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
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

    def enviar(texto: str) -> None:
        tamanho = len(historico)
        aprovacao.nova_tarefa()
        historico.append({"role": "user", "content": texto})
        try:
            rodar(historico)
            return
        except anthropic.AuthenticationError:
            print("Erro: chave de API inválida. Confira o arquivo .env.")
        except anthropic.RateLimitError:
            print("Limite de requisições atingido; tente de novo em instantes.")
        except anthropic.APIStatusError as e:
            print(f"Erro da API ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            print("Erro de conexão com a API.")
        del historico[tamanho:]  # descarta a rodada que falhou

    try:
        if len(sys.argv) > 1:
            enviar(" ".join(sys.argv[1:]))
            if _navegador:
                input("\n(Enter para fechar o navegador e sair) ")
            return

        print(f"Agente pronto ({SHELL_NOME}, pasta: {PASTA_INICIAL}). Diga a tarefa ou 'sair'.")
        while True:
            try:
                texto = input("\nVocê: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if texto.lower() in {"sair", "exit", "quit"}:
                break
            if texto:
                enviar(texto)
    finally:
        if _navegador:
            _navegador.fechar()


if __name__ == "__main__":
    main()
