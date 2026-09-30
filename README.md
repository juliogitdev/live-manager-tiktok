# TikTok Live Manager v4.2

API Python para compartilhar uma conexão TikTok LIVE entre painel e jogos. Normaliza presentes, comentários e likes; aplica perfis opcionais; oferece bridge HTTP com cursor, ACK por consumidor e retomada após restart com SQLite. Consulte o [contrato](DOCUMENTACAO_API_PYTHON.md) e o [relatório de correções](ANALISE_API_PYTHON.md). O [README v4.1](README_v4.1_HISTORICO.md) é histórico.

## Início local

Requer Python 3.11. No PowerShell, a partir da raiz:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:API_KEY = "chave-administrativa-de-teste"
$env:SECRET_KEY = "segredo-de-teste-diferente"
$env:DASHBOARD_PASSWORD = "senha-de-teste"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Abra `http://127.0.0.1:8000`. O banco padrão é `data/live-manager.sqlite3`. Variáveis do `env.example` precisam ser exportadas; o servidor não lê `.env` automaticamente. Use **um worker e uma instância** para evitar conexões TikTok duplicadas.

## Integração com jogos

Configure `GAME_TOKENS` com token longo e escopo por conta/consumidor. O jogo registra `/api/bridge/register`, lê `/api/bridge/{sid}/poll` com cursor e confirma `/api/bridge/{sid}/ack` após gravar seus efeitos de forma idempotente. O [exemplo Roblox](roblox/TikTokBridge.server.lua) usa esse fluxo; em Electron, mantenha o token no processo principal e encaminhe eventos ao renderer por IPC restrito. Use `profile:"raw"` se as regras de gameplay estiverem no jogo. Sessões `simulation:true` exercitam o fluxo offline sem conexão TikTok.

O servidor retém eventos por 24 horas por padrão, mas só pode recuperar eventos que recebeu e gravou. `gap_detected` indica perda fora da janela. O jogo ainda precisa deduplicar recompensas; ACK sozinho não garante efeitos exatamente uma vez.

## Render

O `render.yaml` padrão mantém o **plano Free**, sem disco persistente. Ele serve para testes, mas o SQLite local e os ACKs são perdidos quando o serviço reinicia, dorme ou recebe deploy, conforme a [documentação oficial](https://render.com/docs/free). Nesse caso, o jogo verá uma nova sessão e deve tratar a lacuna; não use a recuperação durável como garantia.

Por enquanto, o repositório oferece apenas o Blueprint Free. Após validar o fluxo, a migração para um plano com disco persistente pode configurar `DATABASE_PATH` no volume para conservar eventos e ACKs entre reinícios. Configure `GAME_TOKENS` se usar token de jogo, e mantenha `SECRET_KEY` e `API_KEY` como segredos independentes.

Não houve deploy nesta alteração. Após publicar, confirme `/health`, valide uma live real e verifique backups do banco antes de depender de presentes para recompensas.

## Testes

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/dashboard.test.cjs
```

As dependências estão fixadas em `requirements.lock`; a CI executa a suíte em Python 3.11 e Node 22. Os testes simulam TikTok e HTTP localmente, sem consumir presentes reais.
