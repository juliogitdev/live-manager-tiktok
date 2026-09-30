# API Python TikTok Live Manager — contrato v4.2.0

Este documento descreve o código **deste checkout**, executado por `uvicorn app.main:app`. O [relatório](ANALISE_API_PYTHON.md) preserva o diagnóstico e o [contrato anterior](DOCUMENTACAO_API_PYTHON_v4.1_HISTORICO.md) é histórico. As mudanças locais ainda não foram publicadas nem testadas contra uma live real. Confirme `/health` no serviço antes de integrar.

## Arquitetura e durabilidade

Uma sessão TikTok por conta é compartilhada entre consumidores. `app/live.py` normaliza eventos, aplica regras e reconecta; `app/storage.py` grava eventos e cursores em SQLite antes de publicá-los; `app/broadcast.py` isola cada painel WebSocket. O banco local requer **um processo e uma instância**. A retenção padrão é 24 horas (`RETENTION_SECONDS=86400`); aumente-a após medir taxa de eventos e espaço. Diagnósticos são gravados como marcadores compactos e não expulsam presentes por limite de quantidade; todos expiram ao fim da janela.

No Render, `render.yaml` mantém o **plano Free** para validação: o SQLite local é temporário e os eventos/ACKs desaparecem quando o serviço dorme, reinicia ou recebe deploy ([documentação oficial](https://render.com/docs/free)). O bridge funciona durante a vida da instância, mas após uma perda do filesystem o jogo precisa registrar uma nova sessão; `resume:true` não recupera dados apagados. Após validar, um plano com disco persistente pode montar um volume e configurar `DATABASE_PATH` nesse volume. O código também funciona em um PC com `DATABASE_PATH=data/live-manager.sqlite3`. SQLite preserva somente eventos que a API recebeu e confirmou no disco; falhas de coleta TikTok, perda do disco ou indisponibilidade além da janela ainda criam lacunas. Faça backup com a API de backup do SQLite ou snapshot do disco; copiar apenas o arquivo principal com WAL ativo pode omitir transações.

## Credenciais

`API_KEY` permanece como credencial administrativa legada, aceita também no bridge. Jogos devem usar `GAME_TOKENS`, configurado como JSON em variável de ambiente:

```json
{"token-aleatorio-com-pelo-menos-32-caracteres":{"consumer_id":"pipajogo","usernames":["sua_conta"]}}
```

Use `X-API-Key: <token>` no processo principal do Electron, backend ou script de servidor Roblox. Cada token só pode registrar/ler/confirmar eventos das contas e do `consumer_id` configurados. Ele não pode simular, alterar regras/configuração, desconectar, ler outra conta ou acessar o painel. Rotação: configure temporariamente dois tokens para o mesmo consumidor e remova o antigo após migrar; a lista é lida no startup. Não entregue segredos ao renderer/browser ou LocalScript.

O painel usa `DASHBOARD_PASSWORD` (ou API key legada se ausente) e cookie HttpOnly/SameSite Strict. Produção exige `SECRET_KEY` e `API_KEY` independentes com ao menos 32 caracteres e `COOKIE_SECURE=true`; configuração inválida falha no startup. Login tem limite por IP e global em janela configurável. WebSocket aceita cookie com Origin correspondente ou header administrativo `X-API-Key`; recusa `?key=` na URL. `ALLOWED_ORIGINS` permite origens extras separadas por vírgula. Em produção, configure um proxy que informe corretamente o endereço do cliente para o limite de login.

## Registrar e retomar

```http
POST /api/bridge/register
X-API-Key: TOKEN_DO_JOGO
Content-Type: application/json

{"username":"sua_conta","profile":"raw","consumer_id":"pipajogo","resume":true}
```

A resposta contém `session_id`, `cursor`, `created`, `profile`, `requested_profile`, `profile_conflict`, `consumer_id`, `resumed`, `oldest_seq`, `latest_seq`, `gap_detected`, `state` e `automation_paused`. Uma conta existente mantém perfil e regras; `profile_conflict:true` informa divergência. Só `POST /api/live/{sid}/profile` altera o perfil. `resume:true` usa o último **ACK** desse consumidor, inclusive após restart se o SQLite persistir. Um consumidor novo ou `resume:false` começa em `latest_seq`, preservando o legado. Se o ACK saiu da retenção, `gap_detected:true`. Use um `consumer_id` estável.

`POST /api/live/connect` (admin) aceita `{"username":"conta","profile":"raw","simulation":false,"pinned":true}`. Sessão `simulation:true` é separada da real, não instancia TikTokLiveClient nem ocupa vaga TikTok; suporta `POST /api/live/{sid}/simulate`. O painel oferece “Criar sessão de teste offline”. Há limites separados `MAX_SIMULATIONS`, `MAX_SESSIONS` ativas e `MAX_STORED_SESSIONS` totais (incluindo dormentes). Sessões administrativas são fixas por padrão; `PUT /api/live/{sid}/pin` com `{"pinned":false}` permite hibernar. Sessões do bridge hibernam após `CONSUMER_TIMEOUT_SECONDS` sem registro/poll e são reativadas no próximo uso, mantendo ID e histórico. Após `RETENTION_SECONDS` sem atividade, são removidas. `AUTO_CONNECT_USERS` cria sessões fixas no startup.

## Poll, lacunas e ACK

```http
GET /api/bridge/{sid}/poll?after=150&wait=8&limit=100&consumer_id=pipajogo
X-API-Key: TOKEN_DO_JOGO
```

`after` é a última sequência assumida pelo jogo; `wait` vai de 0 a 15 segundos (padrão 8); `limit` de 1 a 250 (padrão 100). A resposta traz `events`, `cursor` do último item da página, `latest_seq`, `oldest_seq`, `gap_detected`, `cursor_expired`, `session_id`, `connection_state` e pausa atual. Se `cursor < latest_seq`, busque outra página. **Não avance para `latest_seq` sem processar os eventos.** Após timeout de rede, repita com o mesmo cursor e backoff com jitter. Repetir poll pode devolver os mesmos IDs.

Quando `after < oldest_seq - 1`, o padrão legado é `cursor_expired:true`, lista vazia e cursor final. Para recuperar o trecho retido, repita **com o cursor original** e `recover=true`. A resposta marca `gap_detected:true`, entrega eventos disponíveis e cursor paginado. Registre a perda anterior a `oldest_seq`. Cursor maior que `latest_seq` é inválido mesmo com recuperação. Após 404, registre novamente; se o armazenamento foi perdido, a nova sessão começa no final.

```http
POST /api/bridge/{sid}/ack
X-API-Key: TOKEN_DO_JOGO
Content-Type: application/json

{"consumer_id":"pipajogo","cursor":151}
```

ACK é monotônico e não pode ultrapassar a maior sequência entregue ao consumidor. Retorna `{"ok":true,"cursor":151}`. Confirme **após persistir no jogo** o lote/efeitos ou uma fila idempotente. Uma queda pode reenviar o lote; deduplique por `(session_id,id)` antes de recompensar. Não há promessa de efeitos exatamente uma vez. Sem ACK, cursor local ainda funciona, mas `resume:true` desconhece o progresso. `/api/live/{sid}/metrics` (admin) mostra ACK, sequência entregue, atraso em eventos, última atividade, lacunas e clientes WebSocket descartados.

## Envelope de eventos

Cada item do bridge contém `seq`, `id` (UUID da API), `type`, `timestamp` em ms Unix, `source` (`tiktok`, `simulation` ou `control`), `data`, `actions`, `safety`, `schema_version:1` e `automation_paused_at_ingest`. O bridge envia `data:{}` para diagnósticos/controles. `/events`, simulação e WS incluem também `session_id`, `username` e `simulated`.

Gameplay: `comment` (`comment`), `like` (`count`, `total`), `follow`, `share`, `gift` (`gift.id`, `gift.name`, `gift.type`, `gift.diamond_count`, `repeat_count`, `repeat_end`) e `subscription`. `data.user` traz `unique_id`, `nickname`, `user_id` como string e `avatar_url` opcional, extraída do evento sem consulta adicional. Use imagem padrão se ausente/expirada. O ranking traz `user_id` e `avatar_url`. Presentes de combo intermediários (`gift.type==1`, `streaking=true`) são ignorados; o final usa `repeat_count` total. IDs de origem são deduplicados quando disponíveis, durante a retenção.

Com `raw`, o jogo interpreta `data`. Com `kite` ou `dance`, execute `actions`; não aplique ambas as estratégias ao mesmo evento. O perfil `kite` é exemplo e não define todo o PipaJogo. O monitor usa `safety.level` (`INFO`, `HISTORICAL`, `OBSERVATION`, `ALERT`, `CRITICAL`) e conserva `safety.severity` legado. Só CRITICAL confirmado com configuração ativa pausa automaticamente. A coleta continua, mas `actions` ficam vazias. Em `raw`, respeite `automation_paused_at_ingest`, mesmo após reset. Reset emite `safety_reset` sequenciado e não repõe ações passadas. Filtre `source:"simulation"` fora do modo de teste, avançando cursor/ACK mesmo assim.

## Rotas administrativas

| Rota | Função |
| --- | --- |
| `GET /health` | Saúde do processo e versão; não comprova conexão TikTok |
| `GET /api/meta`, `/api/live/sessions` | Perfis e snapshots |
| `POST /api/live/connect`, `/api/live/{sid}/disconnect`, `PUT /api/live/{sid}/pin` | Ciclo da sessão |
| `GET /api/live/{sid}/status`, `/events`, `/metrics` | Estado, buffer e consumidores; os três caminhos têm o prefixo `/api/live/{sid}` |
| `PUT /api/live/{sid}/config`, `/rules`, `POST /profile` | Configuração e regras; mesmo prefixo |
| `POST /api/live/{sid}/safety/reset`, `/simulate` | Pausa e teste; mesmo prefixo |
| `GET /api/live/{sid}/leaderboard`, `/export.json`, `/export.csv` | Ranking e exportação; mesmo prefixo |
| `GET /docs`, `/openapi.json` | Modelos de pedidos e respostas |

Snapshots contêm estado, estatísticas, regras, configuração, sequência, `simulation`, `pinned` e `last_activity`. `/events` pagina o buffer administrativo em sequência; `tail=true&limit=500` obtém os itens mais recentes para o painel. Exportações administrativas incluem o buffer raw em memória limitado por `MAX_EVENTS`, enquanto o bridge usa o jornal durável. O buffer é reconstruído após restart a partir do jornal compacto, sem payload raw de diagnóstico anterior ao restart. Regras, configuração, perfil e ranking são restaurados do SQLite. Campos desconhecidos de pedidos, condições, ações e configuração são rejeitados; metadados personalizados de ação pertencem a `data`. Tamanho e profundidade das regras são limitados. JSON acima de `MAX_JSON_BYTES` (padrão 256 KiB) recebe 413.

Simulação aceita `gift`, `comment`, `like`, `follow`, `share` e `subscription`; exemplo: `{"type":"gift","username":"Teste","gift_name":"Rose","gift_coins":1,"count":5}`. Ela entra no fluxo da sessão indicada e pode gerar ações; use `simulation:true` para isolamento offline.

## Operação

Instale `requirements.txt` (versões fixadas em `requirements.lock`), exporte `API_KEY`, `SECRET_KEY`, `DASHBOARD_PASSWORD` e execute **um worker**: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`. `.env` não é carregado automaticamente. `env.example` enumera retenção, leases, backoff, limites de WS/login e tokens. No Render, o Blueprint incluído é Free e usa banco temporário; avalie espaço e custo com a taxa real de eventos antes de uma futura migração para disco persistente.

Testes: `python -m pip install -r requirements-dev.txt`, `python -m pytest -q`, `node --test tests/dashboard.test.cjs`. A CI executa os mesmos comandos. Os testes cobrem lógica e HTTP com eventos simulados; antes de usar recompensas em produção, valide live real, assinador, backup e a idempotência do jogo final.
