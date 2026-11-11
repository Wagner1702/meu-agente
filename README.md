# Agente de automação

O Claude (`claude-opus-5-5`) executa comandos no terminal para cumprir tarefas, e **cada comando
pede sua aprovação** antes de rodar.

## Começar

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # coloque sua ANTHROPIC_API_KEY
python agent.py        # modo conversa
python agent.py "organize os PDFs de ~/Downloads em pastas por ano"   # tarefa única
```

Ao aparecer um comando: `s` executa, `n` recusa, `m` recusa explicando o motivo (o Claude ajusta o plano).

## Configuração (.env)

- `AGENTE_DIR` – pasta onde os comandos começam (padrão: a pasta atual).
- `AGENTE_AUTO_APROVAR_LEITURA=1` – roda sem perguntar comandos só de leitura (`ls`, `cat`, `grep`...),
  desde que não tenham `|`, `;`, `&&`, `$()`, redirecionamento ou `sudo`.

Todos os comandos (e recusas) ficam registrados em `agente.log`.

## Segurança

Os comandos são gerados pelo modelo: leia antes de aprovar. Para tarefas arriscadas, rode o agente
num usuário sem privilégios, numa VM ou num contêiner. Cada comando tem limite de 120s.
