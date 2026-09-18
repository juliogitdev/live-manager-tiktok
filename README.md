# TikTok → Roblox Live Manager v4

Backend pronto para Render com painel web, múltiplas contas TikTok, monitor de diagnóstico, simulador e bridge robusto para Roblox.

## O que esta versão resolve

- **Sem conta fixa no Render.** Conecte `@conta1`, `@conta2`, etc. pelo painel.
- **Reutilização da mesma sessão.** Reiniciar o Roblox não abre outra conexão TikTok se a sessão continua viva.
- **Recuperação automática após restart do Render.** O script Roblox detecta `404`, registra a sessão novamente e continua.
- **Sem replay de gifts antigos ao reiniciar o Roblox.** O registro começa no cursor atual.
- **Long polling.** Menos requests que polling a cada segundo e tráfego de entrada contínuo durante o jogo.
- **Reconexão com backoff.** Contas offline não são marteladas a cada poucos segundos.
- **Deduplicação por message/log ID quando disponível.**
- **Gift streak correto.** Gifts de sequência só geram gameplay quando a sequência termina.
- **Painel protegido por senha/cookie HttpOnly.**
- **API key separada para Roblox.**
- **Perfis e regras configuráveis.**
- **Simulador gratuito de gifts/likes/comments/follows/shares.**
- **Ranking por moedas, gifts, likes, comentários e shares.**
- **Exportação JSON/CSV.**
- **Monitor com severidade.** Alertas fortes podem pausar apenas as automações, sem perder o monitoramento.
- **Eventos opcionais carregados dinamicamente**, reduzindo quebra quando o schema do TikTokLive muda.

## Monitor de diagnóstico

Sinais acompanhados quando disponíveis na versão instalada:

### Alta severidade
- `BottomEvent`
- `PerceptionEvent`
- `PartnershipPunishEvent`
- `RoomVerifyEvent`
- `GiftDynamicRestrictionEvent`

### Média
- `AccessControlEvent`
- `GiftPromptEvent`

### Informativos
- `NoticeEvent`
- `RoomNotifyEvent`
- `SystemEvent`
- `InRoomBannerEvent`
- `ToastEvent`
- `AccessRecallEvent`

Esses sinais **não são tratados como "anti-ban"**. O objetivo é registrar, alertar e, opcionalmente, pausar ações automáticas para revisão.

## Perfis incluídos

### `raw`
Recebe eventos, mas não gera ações automáticas.

### `dance`
- comentário → `spawn`
- follow → `aura`
- Rose → `animation`
- gift de 5+ moedas → `giant`

### `kite`
- comentário → `spawn`
- likes → `heal`
- follow/share → `shield`
- Rose → `heal` + `attack`
- gift de 5+ moedas → `giant`

As regras podem ser alteradas no painel em **Regras**.

## Deploy no Render

### Opção recomendada: Blueprint

1. Crie um repositório GitHub.
2. Envie **todo o conteúdo deste ZIP** para a raiz do repositório.
3. No Render: **New → Blueprint**.
4. Escolha o repositório.
5. O Render detectará `render.yaml`.
6. Na criação, o Render solicitará `DASHBOARD_PASSWORD`.
7. Clique em deploy.
8. Depois do deploy, abra **Environment** e copie o valor de `API_KEY`.
9. Abra `https://SEU-SERVICO.onrender.com`.
10. Entre com `DASHBOARD_PASSWORD`.

`API_KEY` e `SECRET_KEY` são geradas automaticamente pelo Blueprint.

## Roblox

Abra `roblox/TikTokBridge.server.lua` e altere:

```lua
local BASE_URL = "https://SEU-SERVICO.onrender.com"
local API_KEY = "API_KEY_DO_RENDER"
local TIKTOK_USERNAME = "sua_conta"
local PROFILE = "dance"
```

Depois:

1. Coloque o script em `ServerScriptService`.
2. Roblox Studio → **Game Settings → Security**.
3. Ative **Allow HTTP Requests**.
4. Ligue os `ActionHandlers` às funções reais do seu jogo.

O script:
- registra/reutiliza a sessão;
- começa no evento atual para não repetir presentes antigos;
- usa long polling de 8 segundos;
- detecta restart do backend;
- registra uma nova sessão automaticamente;
- deduplica IDs recentes localmente;
- executa somente `actions` geradas pelas regras.

## Render Free: comportamento importante

O Render Free suspende Web Services depois de 15 minutos sem tráfego **de entrada**.

Durante uma live com o Roblox conectado, o long polling gera tráfego de entrada regularmente e mantém o serviço ativo. Enquanto o dashboard está aberto, ele envia mensagens WebSocket periódicas ao backend.

Uma conexão **saindo do Render para o TikTok**, sozinha, não deve ser considerada suficiente para manter o serviço acordado.

Se não houver Roblox nem dashboard aberto, o Free pode suspender o processo.

## AUTO_CONNECT_USERS

Opcionalmente configure no Render:

```env
AUTO_CONNECT_USERS=conta1:dance,conta2:kite
```

Quando o processo iniciar novamente, essas sessões serão recriadas.

Isso é útil porque o filesystem do Render Free é efêmero. A aplicação foi desenhada para funcionar sem depender de estado salvo em disco.

## Estado e persistência

Configurações e estatísticas da sessão ficam em memória. Se o processo reiniciar:

- Roblox se registra novamente automaticamente;
- `AUTO_CONNECT_USERS` pode recriar contas automaticamente;
- estatísticas históricas zeram;
- alterações manuais de regras da sessão zeram.

Para histórico permanente, adicione banco externo/Postgres em uma próxima etapa. Isso **não é necessário para o gameplay funcionar**.

## TikTokLive

Esta versão fixa:

```text
TikTokLive==7.0.1
```

Isso evita que um update incompatível seja instalado automaticamente durante deploy.

A biblioteca é não oficial e depende do Webcast interno do TikTok. Mudanças do TikTok podem exigir atualização futura.

Ela usa um servidor de assinatura de terceiros com limites comunitários. Se você possuir uma chave compatível, pode configurar:

```env
EULER_API_KEY=...
```

Não é obrigatória para os primeiros testes.

## Desenvolvimento local

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:API_KEY="dev-key"
$env:SECRET_KEY="dev-secret"
$env:DASHBOARD_PASSWORD="senha"

uvicorn app.main:app --reload
```

Abra:

```text
http://127.0.0.1:8000
```

## Endpoints principais

Dashboard:
- `GET /`
- `GET /health`
- `POST /api/auth/login`
- `GET /api/live/sessions`
- `POST /api/live/connect`
- `GET /api/live/{id}/events`
- `PUT /api/live/{id}/rules`
- `POST /api/live/{id}/simulate`
- `GET /api/live/{id}/leaderboard`

Roblox:
- `POST /api/bridge/register`
- `GET /api/bridge/{id}/poll`

## Segurança

- `DASHBOARD_PASSWORD` não deve ser a mesma senha do TikTok.
- Nunca coloque `API_KEY` em LocalScript.
- Mantenha o repositório privado se o código tiver chaves preenchidas.
- O ZIP entregue não contém nenhuma chave real.
- Raw payloads de diagnóstico são limitados e campos sensíveis comuns são redigidos.

## Testes incluídos

`tests/test_rules.py` valida o motor de regras sem precisar conectar ao TikTok.

A estrutura também pode ser verificada com:

```bash
python -m py_compile app/*.py
```
