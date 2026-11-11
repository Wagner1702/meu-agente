# Agente de automação

O Claude (`claude-opus-5-5`) usa o **terminal** e um **navegador** para cumprir tarefas, e cada
ação pede sua aprovação antes de rodar.

## Instalar (Windows)

No PowerShell, dentro da pasta do projeto:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env          # preencha ANTHROPIC_API_KEY, ERP_USUARIO e ERP_SENHA
```

Navegador: rode `playwright install chromium`. Se a rede da empresa bloquear o download,
coloque `AGENTE_NAVEGADOR_CANAL=msedge` no `.env` para usar o Edge já instalado.

(Linux/macOS: `python3 -m venv .venv && source .venv/bin/activate`, `cp .env.example .env`.)

## Usar

```powershell
python agent.py        # modo conversa
python agent.py "abra a rotina 2855 em http://172.30.0.14/Tratornew/servlet/erpsolution.compras.cmpprodutosconsultar e pesquise o código 404831A1P"
```

Para o ERP interno, rode num computador conectado à rede da empresa (ou VPN).

A cada ação: `s` executa, `n` recusa, `t` aprova o resto da tarefa atual, `m` recusa explicando
o motivo (o Claude ajusta o plano). Olhar a página e tirar captura de tela não pedem aprovação.

## Ferramentas

- `bash`: comandos no terminal (bash; no Windows sem Git Bash, PowerShell).
- `navegador_*`: abrir URL, ver a página (texto + elementos numerados, inclusive em frames e
  pop-ups), clicar, digitar, escolher opção, apertar tecla, captura de tela.
- `navegador_digitar_credencial`: faz login com `ERP_USUARIO`/`ERP_SENHA` do `.env`. A senha não
  é enviada ao Claude e é trocada por `***` no `agente.log` e nas páginas lidas.

Todas as ações (e recusas) ficam registradas em `agente.log`.

## Segurança

As ações são geradas pelo modelo: leia antes de aprovar. O agente foi instruído a não salvar,
excluir nem confirmar registros no ERP sem você pedir.
