# Revisão da API Python — correções e redução de custo

## Resultado da implementação local v4.2 — 30/09/2026

Esta seção prevalece sobre o resumo v4.1 e o relatório histórico abaixo. O código v4.2 está neste checkout; ainda não há deploy nem live real validada. Contrato: [DOCUMENTACAO_API_PYTHON.md](DOCUMENTACAO_API_PYTHON.md).

| Problema restante na v4.1 | Implementação v4.2 |
| --- | --- |
| Lacunas, restart e presentes | Jornal SQLite com commit antes de publicar; sessão/ID de evento preservados no restart, deduplicação por ID de origem, retenção configurável, ACK por consumidor, `resume:true`, métricas de lacunas e atraso. Diagnósticos duráveis compactos não expulsam presentes por contagem. Persistência entre reinícios depende de disco real: `render.yaml` é Free/temporário; uma migração para disco pago fica para depois da validação. Não há recuperação de eventos que a API nunca recebeu. |
| Credenciais | `GAME_TOKENS` limitados por conta/consumidor nas rotas do bridge, sem permissão administrativa; limite de login, Origin WS, remoção de chave na query, Secure explícito, segredos longos/independentes em produção e falha clara no startup. API key administrativa legada segue aceita para migração. Tokens rotacionam via configuração e restart. |
| Sessões e reconexão | Hibernação após timeout sem consumidor, ID e histórico preservados, reativação na retomada, fixação administrativa opcional; backoff exponencial com jitter, reinício após conexão, respeito ao prazo do assinador e liberação do cliente antes de dormir. |
| WS lento | Uma fila limitada e tarefa de envio por cliente, timeout, filtro opcional por `session_id`, coalescência de status e descarte de clientes lentos. `push()` não espera envio de socket. |
| Simulação isolada | Sessão `simulation:true` separada, sem TikTokLiveClient nem vaga de conexão, disponível no painel. |
| Contrato/testes | `schema_version:1`, modelos de resposta OpenAPI, limites profundos para regras, dependências fixadas e CI Python/Node. Fixture usa classe protobuf instalada para avatar. |

Validação local: os testes cobrem restart em arquivo SQLite, retry/ACK, restauração de regras/ranking/pausa, isolamento de tokens, limpeza por inatividade, rate limit, Origin, filas lentas e simulação sem TikTok. Um ensaio local isolado gravou 500 presentes simulados em 0,38 s (~1.300 eventos/s, banco ~240 KiB); **não** é benchmark do Render nem medição de custo real. A integração do jogo precisa aplicar efeitos de modo idempotente. O plano Free do Render não oferece disco persistente ([documentação oficial](https://render.com/docs/disks)); a opção paga não está configurada neste repositório. A aplicação ainda não foi implantada nem testada com live real.

---

## Registro da implementação v4.1

Versão final deste checkout: **4.1.0**, consolidada sobre o commit base abaixo. Sem novo commit ou deploy publicado. As seções numeradas posteriores preservam o diagnóstico histórico; esta tabela informa o estado após as correções. O contrato atualizado está em [DOCUMENTACAO_API_PYTHON.md](DOCUMENTACAO_API_PYTHON.md).

Antes das alterações, a revisão local confirmou a divergência `app/` versus raiz, a falha de coleta em `app.diagnostics`, os refreshes por evento e a substituição de perfil em `LiveManager.connect`. Também confirmou por inspeção as conversões permissivas e a dependência da classificação em relação à retenção raw.

| Item do relatório | Resultado atual |
| --- | --- |
| 1. Deploy/v4.1 | Corrigido: backend em `app/`, painel em `static/`, Lua em `roblox/`, diagnósticos em `tests/`; duplicatas removidas. Health/meta/painel usam 4.1.0. |
| 2. Consultas do painel | Corrigido: eventos alimentam o feed sem HTTP; uma sincronização a cada 5s, no máximo uma pendente; timers e reconexão encerrados no logout/expiração e reiniciados sem duplicação. Status de espectadores compacto no WS. |
| 3. Perfis compartilhados | Correção mínima concluída: reutilizar preserva regras e conexão; resposta 200 informa perfil efetivo e `profile_conflict`. Troca somente pela rota administrativa explícita. `consumer_id` continua sem isolamento/ACK. |
| 4. Lacunas/durabilidade | Parcial: metadados em todo poll e recuperação opcional `recover=true` do trecho retido, preservando o padrão antigo. Não há persistência, offsets, métricas de atraso ou buffer separado para presentes. Restart perde estado e a sessão antiga retorna 404. |
| 5. Credenciais | Parcial: removido segredo público; startup Render exige ambas as chaves; senha sem segredo/chave é rejeitada em qualquer ambiente. Pendentes tokens por escopo/conta, rate limit, Origin WS, remoção da chave em URL, Secure configurável e detecção de outros ambientes de produção. API key mantém privilégios antigos para compatibilidade. |
| 6. Avatar | Implementado: URL opcional com fallback entre resoluções, sem chamada externa, também no ranking. Validado com objetos sintéticos; ainda falta fixture sanitizada de uma live real. |
| 7. Validação | Corrigidos booleanos, condições desconhecidas, números usados pelo motor, limite de ações e atualização atômica. Alias de segurança v4.0 aceito na escrita. Falha numérica inesperada não impede coleta. Pendentes modelos completos de resposta e limites profundos de metadados customizados. |
| 8. Sessões/reconexão | Pendente: leases de consumidores, sessão fixa, jitter, tratamento de limite do assinador e reset do backoff. |
| 9. WS lento | Pendente: broadcast continua sequencial; filas limitadas/timeouts por cliente ainda necessários. Redução das consultas do painel não resolve esse problema. |
| 10. Pausa temporal | Corrigido: `automation_paused_at_ingest` e `schema_version:1` em cada evento, controle sequenciado no reset, sem replay de ações. Revisão manual/persistida de presentes permanece responsabilidade do jogo. |
| 11. Retenção raw | Corrigido: classificação independe de `STORE_RAW_DIAGNOSTICS`; truncamento profundo não serializa estruturas contendo segredos. |
| 12. Operação | Parcial: regressões e dependências de teste em `requirements-dev.txt`; perfis desconhecidos rejeitados. Pendentes simulação isolada, CI, lock de dependências, modelos de resposta e medições reais. |

Validação local sem conexão TikTok: suíte Python e testes Node do painel. Cobertura inclui import/versão/assets, conflito entre consumidores, alteração administrativa explícita, paginação/retry, cursor expirado e recuperação opcional, restart/404, combos e deduplicação, avatar, validação atômica, pausa/reset e classificação com raw ligado/desligado. O teste de painel injeta mil eventos, confirma ausência de HTTP por evento e verifica login/logout e requisição lenta. Não houve benchmark, validação em produção nem medição de custo real.

Resultado final: **27 testes Python e 3 testes Node passaram**; `git diff --check` sem erros. Dois avisos de depreciação vêm das dependências WebSocket do TikTokLive. Executar com `python -m pip install -r requirements-dev.txt`, `python -m pytest -q` e `node --test tests/dashboard.test.cjs` (Node com suporte a `node:test`). Persistência e política de inatividade precisam de requisitos de retenção/consumidores; não foram presumidas garantias de entrega durável.

---

## Relatório histórico anterior às correções

Repositório: [juliogitdev/live-manager-tiktok](https://github.com/juliogitdev/live-manager-tiktok). Commit analisado: `83efdb0acde03bf9d158248e67dd9188d53751c7`, em 30/09/2026.

O contrato para entregar à IA do jogo está em [DOCUMENTACAO_API_PYTHON.md](DOCUMENTACAO_API_PYTHON.md). Este relatório contém **constatações e propostas**, não alterações já feitas na API.

## Avaliação

A arquitetura é adequada como ponto de partida para alimentar o jogo: normalização centralizada, reutilização de sessão por conta, long polling, filtro de combos intermediários e buffer com cursor. O nome Roblox não limita a integração; HTTP pode ser consumido pelo Electron, Godot ou backend web.

O potencial de economia vem principalmente de compartilhar uma conexão TikTok, mantê-la estável e evitar trabalho/tráfego redundante. Trocar de linguagem para Python, isoladamente, não demonstra redução de custo.

Antes de depender desta API na migração, corrigiria a divergência de deploy, o excesso de consultas do painel e o conflito de perfis. Para usar presentes com maior confiabilidade, também definiria persistência e recuperação de lacunas.

## 1. Alta prioridade — o deploy não utiliza a atualização 4.1

**Confirmado por inspeção e execução local.**

O Blueprint executa `uvicorn app.main:app`. O pacote `app/` contém versão 4.0.0 e o monitor antigo; `main.py`, `live.py`, `config.py` e `diagnostics.py` na raiz contêm as mudanças 4.1, mas não são os módulos importados. O HTML serve `static/`, onde os arquivos do painel também são antigos.

Efeitos concretos:

- `DIAGNOSTIC_*` configuradas no Render não têm efeito no runtime atual.
- O monitor ainda pausa por tipos classificados como `high`, inclusive `GiftDynamicRestrictionEvent`, sem a avaliação contextual descrita no README.
- `safety_auto_pause_critical` não é uma configuração reconhecida pelo runtime atual.
- `python -m pytest -q` falha na coleta: `ModuleNotFoundError: No module named 'app.diagnostics'`.

**Correção sugerida:** consolidar a atualização no layout efetivamente executado: incorporar os diffs de `main.py`, `live.py`, `config.py` em `app/`; adicionar `app/diagnostics.py`; incorporar `app.js`, `index.html`, `style.css` em `static/`; organizar o teste de diagnóstico em `tests/` com import correto. Comparar também as duas cópias do script Roblox. Depois eliminar duplicatas obsoletas, atualizar a versão e testar o pacote final. Trocar apenas o comando para `uvicorn main:app` não resolve: o arquivo raiz usa imports relativos de pacote.

**Aceite:** import de `app.main`, `/health`, `/api/meta`, painel e testes apontam para a mesma versão; diagnóstico vazio/histórico não pausa indevidamente; sinal crítico de fixture pausa conforme regra.

Fontes: [render.yaml:7](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/render.yaml#L7), [app/config.py:33](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/config.py#L33), [app/live.py:124](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L124), [test_diagnostics.py](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/test_diagnostics.py).

## 2. Alta prioridade para custo — painel gera consultas a cada evento

**Confirmado por inspeção. Não foi feito benchmark de carga.**

`static/app.js` chama `refresh()` para cada gift, comentário, like, follow e share recebido no WebSocket. Cada refresh faz `GET /health` e `GET /api/live/sessions`. Mensagens de status também disparam refresh. Ainda existe um intervalo fixo de cinco segundos.

Com 20 eventos de gameplay por segundo, isso representa aproximadamente **40 requisições HTTP extras por segundo por painel**, sem contar status e intervalo. É uma estimativa aritmética do código, não tráfego medido. O retorno de `/sessions` inclui regras, configurações, estatísticas e possíveis detalhes de segurança de todas as sessões.

**Correção sugerida:** atualizar o feed com o evento já recebido; agrupar atualização de estatísticas/status em uma frequência limitada, por exemplo uma vez por segundo. Garantir no máximo um refresh pendente. Usar mensagens compactas de status/deltas no WS e fazer ressincronização HTTP ocasional. Guardar e limpar o ID de `setInterval`; novos logins chamam `boot()` novamente e podem criar intervalos adicionais. Desativar reconexão WS ao fazer logout.

**Aceite:** aumentar a taxa de likes não aumenta linearmente a taxa de consultas HTTP do painel; logout limpa timers; duas entradas sucessivas não duplicam polling.

Fonte: [static/app.js:96 e 293–319](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/static/app.js#L96).

## 3. Alta prioridade — consumidor altera regras dos outros

**Reproduzido localmente.**

Ao reutilizar uma sessão, `LiveManager.connect()` aplica o perfil do novo pedido se for diferente. Um jogo que registra `raw` pode apagar as regras `kite` usadas por outro consumidor da mesma live. O `consumer_id` é aceito e ignorado, portanto não isola regras nem cursores.

**Correção mínima:** registrar/reutilizar não deve alterar um perfil existente. Se houver conflito, retornar o perfil vigente com indicação clara ou 409 específico; alteração de regras/perfil deve passar por rota administrativa explícita. **Evolução:** manter coleta compartilhada por live e configuração/regras por consumidor, se isso for necessário no produto.

**Aceite:** registrar a mesma conta com consumidores diferentes não muda regras já ativas, não cria uma segunda conexão TikTok e mantém contratos explícitos para conflitos.

Fontes: [app/live.py:637](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L637), [app/main.py:69 e 355](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/main.py#L355).

## 4. Alta prioridade para presentes — não há recuperação durável de eventos

**Comportamento de expiração reproduzido; perda após restart decorre do armazenamento em memória.**

O buffer tem limite de eventos, não de duração. A retenção aproximada é `MAX_EVENTS / taxa_de_eventos_por_segundo`. Com 2.500 eventos e taxa hipotética de 50/s, há cerca de 50 segundos de histórico. Diagnósticos e eventos desconhecidos ocupam o mesmo buffer que presentes.

Quando o cursor expira, o bridge devolve lista vazia e o cursor atual, pulando também os itens ainda retidos. Após restart, não existe histórico para recuperar. Novo registro inicia no final; um cliente que registra novamente após qualquer timeout pode perder recompensas.

**Correção sugerida em etapas:**

1. Retornar `oldest_seq`, `latest_seq`, `gap_detected` e uma identificação da instância/sessão em todos os caminhos, permitindo recuperação explícita do trecho disponível.
2. Separar/reduzir ruído de diagnóstico para não expulsar gameplay; preservar ordenação e prioridade de presentes.
3. Se a garantia de presentes exigir, persistir eventos relevantes e offsets por consumidor, com janela definida de retenção e chave idempotente de origem.
4. Acrescentar métricas de lacunas e atraso do consumidor. Não anunciar entrega exatamente uma vez sem idempotência e confirmação coerentes no jogo.

Para um único PC, armazenamento local persistente pode reduzir custo de infraestrutura; para o Render atual, memória e disco efêmero não fornecem durabilidade. A escolha depende de quanto histórico precisa sobreviver e de quais falhas precisam ser cobertas.

**Aceite:** interromper um consumidor e retomar não duplica presentes; uma lacuna é explicitamente relatada; restart respeita a política documentada de recuperação.

Fontes: [app/live.py:148](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L148), [app/main.py:377](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/main.py#L377).

## 5. Alta prioridade antes de ampliar acesso — credenciais e permissões

**Inspeção estática; não houve teste contra produção.**

A mesma API key acessa leitura, simulação, alteração de regras e desconexão de qualquer sessão. O WebSocket aceita essa chave em query string e transmite todas as sessões. Não há isolamento por cliente/conta.

Há também um problema condicional de configuração: se `SECRET_KEY` e `API_KEY` estiverem vazias, `_secret()` usa um valor fixo público. Mesmo que só `DASHBOARD_PASSWORD` seja definida, cookies são assinados com esse valor conhecido. O Blueprint gera as duas chaves e evita essa condição quando aplicado corretamente, mas outro deploy pode ficar vulnerável.

**Correção sugerida:** falhar no startup de produção quando os segredos obrigatórios estiverem ausentes; não aceitar fallback público. Separar token de leitura/gameplay de credencial administrativa, com escopo por sessão/conta e rotação. Trocar chave em URL por autenticação apropriada ao consumidor e redigir logs. Adicionar limitação de tentativas de login; `sleep(0.35)` não limita concorrência de tentativas. Validar Origin do WS quando usar cookies e tornar configuração de cookie Secure explícita fora do Render.

**Aceite:** token do jogo não simula presentes nem altera/desconecta sessões; credencial de uma conta não lê outra; serviço mal configurado falha de forma clara.

Fonte: [app/auth.py](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/auth.py), [app/main.py:122 e 428](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/main.py#L428).

## 6. Prioridade média — faltam avatares no contrato

**Reproduzido localmente.**

`event_user()` só retorna `unique_id`, `nickname` e `user_id`. O jogo Godot já procura URL de avatar, mas ela não é fornecida pelo backend analisado, mesmo que esteja no objeto original da biblioteca.

**Correção sugerida:** acrescentar `avatar_url: string | null`, extraída das URLs de imagem já recebidas no usuário, com fallback entre resoluções disponíveis. A biblioteca/protobuf instalada possui `avatar_thumb`; validar com fixtures reais sanitizadas o formato de sua lista de URLs. Não criar uma consulta TikTok adicional por evento para buscar foto. Cachear por usuário e atualizar quando a URL mudar.

Também incluir `user_id` e avatar no leaderboard se ele for utilizado em UI. No jogo, manter imagem padrão e tratar falha/expiração da URL sem bloquear eventos.

**Aceite:** evento real de comentário/gift com foto disponibiliza URL; eventos sem foto continuam válidos; crescimento de eventos não gera consultas adicionais de perfil por evento.

Fonte: [app/live.py:69](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L69).

## 7. Prioridade média — validação permite erros em tempo de evento

**Reproduzido localmente.**

`validate_rules()` aceita `min_coins: "invalid"`; ao chegar um gift, `matches()` tenta converter para inteiro e lança ValueError. A aceitação da configuração não garante que o motor consiga executá-la. Campos como `amount_per_count` apresentam o mesmo tipo de risco.

`PUT /config` usa `bool(value)`: `"false"` ativa uma configuração. Campos desconhecidos são ignorados sem aviso. Nos modelos de simulação/regras faltam limites adequados para vários textos/estruturas.

**Correção sugerida:** modelos Pydantic tipados para condições, ações, configuração e respostas; booleanos estritos; limites de strings, ações e valores; rejeitar campos desconhecidos que alterem a intenção. Validar tudo antes de substituir regras ativas e conter uma falha de regra sem perder a coleta de eventos.

**Aceite:** regra inválida é rejeitada ao salvar, preservando a regra anterior; `"false"` não vira true silenciosamente; dados inválidos não interrompem o processamento de gifts seguintes.

Fontes: [app/rules.py:98, 154 e 185](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/rules.py#L98), [app/main.py:201](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/main.py#L201).

## 8. Prioridade média para custo — conexões sem consumidor e reconexão

**Inspeção estática.**

Fechar o jogo não encerra a sessão. Sem timeout de inatividade, ela continua tentando conectar mesmo offline e ocupa uma das cinco vagas. O intervalo offline padrão de 30 segundos permite aproximadamente 120 tentativas por hora por conta, desconsiderando o tempo gasto em cada tentativa. Nem toda tentativa offline chega ao serviço de assinatura: a biblioteca verifica o estado antes de assinar.

O backoff de falhas não tem jitter nem tratamento específico para limite do assinador. O contador de espera pode continuar alto depois de uma conexão bem-sucedida, pois não é explicitamente reiniciado no callback de conexão.

**Correção sugerida:** aproveitar `consumer_id` para acompanhar consumidores/atividade, com janela de tolerância e opção administrativa de manter uma sessão fixa. Aumentar progressivamente o intervalo de contas offline ociosas. Tratar `SignatureRateLimitError` e respeitar a janela indicada pelo provedor; acrescentar jitter. Reiniciar o backoff após conexão estável e fechar recursos antes de longas esperas. Não reconectar a cada rodada.

**Aceite:** jogo reiniciado rapidamente reutiliza a conexão; conta abandonada não tenta para sempre; vários clientes em falha não reconectam todos no mesmo instante.

Fonte: [app/live.py:521–590 e 632](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L521). A sequência de live check/assinatura foi conferida também no pacote instalado TikTokLive 7.0.1.

## 9. Prioridade média — WebSocket lento prolonga processamento

**Reproduzido com assinante artificial que não conclui o envio.**

`Broadcaster.send()` percorre sockets e aguarda cada `send_json`, sem timeout ou fila individual. `push()` espera esse broadcast antes de terminar. O evento já foi inserido no buffer, mas um cliente lento pode manter tarefas penduradas, atrasar outros envios e aumentar trabalho/memória em carga. Isso não significa necessariamente que todo o event loop seja bloqueado.

**Correção sugerida:** fila limitada por cliente, tarefa de envio independente, timeout, descarte de atualizações redundantes de status e desligamento de clientes persistentemente lentos. Filtrar por sessão e evitar enviar snapshots grandes desnecessariamente.

**Aceite:** um painel lento não atrasa a publicação para outro painel nem acumula tarefas sem limite.

Fonte: [app/live.py:107 e 340](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L107).

## 10. Prioridade média — pausa precisa de contexto por evento

**Coleta durante pausa reproduzida; ambiguidade temporal identificada no contrato.**

A pausa só impede geração de `actions`. Eventos raw e estatísticas continuam. Um jogo que ignora `automation_paused` executará gameplay normalmente. Além disso, o poll retorna apenas a pausa atual: se houve pausa e reset entre consultas, o consumidor raw não sabe quais eventos chegaram durante a pausa.

**Correção sugerida:** incluir `automation_paused_at_ingest` ou `gameplay_allowed` no envelope de cada evento e emitir transições de controle com sequência. Definir política explícita de descarte, retenção e revisão de presentes recebidos durante pausa. Não repor automaticamente ações anteriores ao reset.

**Aceite:** cliente raw e cliente de actions concordam sobre quais eventos estavam elegíveis no instante de recepção, inclusive após atraso de polling.

Fonte: [app/live.py:291–341](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L291).

## 11. Ao incorporar 4.1 — armazenamento raw altera a classificação

**Inspeção do código da raiz; não é funcionalidade ativa do deploy analisado.**

`process_diagnostic()` classifica o conteúdo usando `raw.get("payload")`. Quando `STORE_RAW_DIAGNOSTICS=false`, `_raw_event()` retorna somente a classe, então o classificador passa a enxergar payload vazio. Uma configuração destinada a economizar armazenamento pode alterar a decisão do monitor.

**Correção sugerida:** extrair os sinais necessários do evento independentemente de armazenar o payload. A flag deve controlar persistência/exposição, não a existência das informações usadas pela classificação. Classificar primeiro, sanitizar e decidir retenção depois. A sanitização atual também merece testes para objetos profundos: o limite de profundidade converte valores em string antes de percorrer/redigir chaves internas.

**Aceite:** o mesmo evento tem classificação igual com armazenamento raw ligado ou desligado; campos sensíveis sintéticos em profundidade não aparecem nos payloads exportados.

Fontes: [live.py:360–400](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/live.py#L360), [app/live.py:33](https://github.com/juliogitdev/live-manager-tiktok/blob/83efdb0acde03bf9d158248e67dd9188d53751c7/app/live.py#L33).

## 12. Melhorias de operação e manutenção

- **Simulação isolada:** criar sessão de teste que não instancia TikTokLiveClient. Hoje o simulador não gasta presentes, mas cadastrar a sessão ainda inicia conexão e ocupa capacidade.
- **Contrato versionado:** adicionar versão do esquema de eventos e modelos de resposta; OpenAPI atual descreve pedidos melhor que respostas. Rejeitar perfis desconhecidos em vez de converter silenciosamente para raw.
- **Testes/CI:** executar toda a suíte em cada alteração; incluir paginação, combos, perfil compartilhado, pausa, reautenticação, lacunas, restart e assinatura limitada. `py_compile` sozinho não detecta o módulo ausente nem esses comportamentos.
- **Dependências reproduzíveis:** TikTokLive está fixada, mas várias dependências diretas/transitivas flutuam. Manter um lock validado e atualizar deliberadamente. A instalação de revisão importou corretamente, mas emitiu avisos de depreciação do pacote websockets.
- **Higiene do repositório:** remover `app/__pycache__` versionado e ignorar bytecode, ambientes virtuais e segredos; manter um único lugar para cada fonte.
- **Ranking:** deixar explícito o teto de participantes e a ausência de rounds/reset. Adicionar persistência/IDs estáveis se o ranking da API se tornar parte do produto, evitando misturá-lo ao placar de batalha do jogo.
- **Métricas:** medir conexões/assinaturas, reconexões por motivo, eventos por tipo, latência de processamento, tamanho em bytes, atraso/expiração de cursor e clientes WS. Evitar logs completos de cada like.

## O que realmente influencia custo

| Caminho | O que consome recursos | Melhor ação inicial |
| --- | --- | --- |
| TikTok → conector | Conexão, descoberta da sala, assinatura, catálogo e reconexões | Reutilizar por conta e manter estável |
| API → jogo | Pedidos HTTP, bytes e serialização | Um consumidor, long polling, drenar lotes e evitar payload excessivo |
| API → painel | Broadcast e consultas de refresh | Limitar refresh; aproveitar dados já recebidos |
| Sessões offline | Tentativas repetidas e vagas ocupadas | Expiração por inatividade e backoff progressivo |
| Buffer/diagnósticos | Memória, retenção e exportação | Limites por tamanho, supressão e separação de ruído |
| Hospedagem | Horas ativas, memória, CPU, banda, armazenamento | Escolher local ou servidor conforme necessidade de acesso remoto |

Com uma conexão saudável, o recebimento dos eventos usa o fluxo TikTok; cada poll do jogo não cria automaticamente uma nova conexão TikTok. No pacote 7.0.1 instalado, a assinatura é buscada durante a abertura da conexão após verificações iniciais. A biblioteca usa um serviço de assinatura de terceiros com limites comunitários; uma chave pode ampliar limites. Logo, não é correto prometer custo externo zero ou independência desse serviço. [Fonte: TikTokLive](https://github.com/isaackogan/TikTokLive#webdefaults).

Sem eventos, um poll de oito segundos gera aproximadamente 450 pedidos/hora por consumidor; quinze segundos, 240. Com eventos frequentes, as respostas retornam antes e a taxa pode ser maior. Aumentar a espera reduz pedidos vazios, mas não resolve sozinho o volume de uma live movimentada. Lotes curtos de coalescência podem ser avaliados depois de medir a latência aceitável.

Para seu cenário de um PC transmitindo, **rodar a API localmente é uma alternativa a avaliar**: mantém o mesmo contrato e reduz a necessidade de hospedar o coletor em nuvem. Ainda há dependência da internet, do assinador e dos recursos do PC. Deve-se comparar estabilidade da rede/IP local e do servidor, não assumir que serão equivalentes.

No Render Free, a documentação atual informa suspensão após 15 minutos sem tráfego de entrada, retomada em aproximadamente um minuto, filesystem efêmero e possibilidade de restart. Long polling e mensagens recebidas pelo WS mantêm atividade, mas não impedem todas as reinicializações nem tornam os dados duráveis. Há cotas de horas e banda. [Fonte: Render — Deploy for Free](https://render.com/docs/free).

**Não há evidência suficiente para calcular economia em reais:** faltam o provedor/plano anterior, número de contas simultâneas, horas de live, volume de eventos, tráfego e reconexões. Medir essas grandezas antes/depois; não confundir menos consultas ao backend com menos assinaturas TikTok.

## Validação realizada

Ambiente temporário Windows/Python 3.11, dependências instaladas a partir do `requirements.txt` do commit analisado, mais pytest. Acesso ao repositório somente para leitura; nenhuma alteração na API remota, nenhum deploy, nenhuma credencial de produção e nenhuma conexão real TikTok utilizada.

| Verificação | Resultado |
| --- | --- |
| Import de `app.main` | Passou; versão 4.0.0 |
| Suíte original completa: `python -m pytest -q` | Falhou na coleta por ausência de `app.diagnostics` |
| Testes originais `tests/test_rules.py` | 2 passaram |
| Verificações adicionais locais de contrato | 12 passaram, caracterizando o comportamento existente |

As 12 verificações adicionais cobriram:

1. Versão efetiva e exigência de autenticação.
2. Reutilização por username, troca de perfil compartilhado e cursor atual no registro.
3. Paginação, distinção entre cursor entregue e latest_seq, repetição do mesmo ID em retry e formato reduzido do bridge.
4. Cursor expirado com descarte inclusive do buffer remanescente.
5. 404 para sessão inexistente.
6. Pausa mantendo eventos raw/estatísticas e removendo actions.
7. Simulação contornando toggle de tipo e ficando fora do ranking por padrão.
8. String `"false"` convertida em true e configuração 4.1 ignorada.
9. Regra numérica inválida aceita na validação e falhando ao processar evento.
10. Ausência de avatar no usuário normalizado.
11. Filtro de combo intermediário e deduplicação de evento final com ID de origem.
12. Assinante WebSocket lento mantendo `push()` pendente.

Esses testes adicionais são de caracterização: passar significa que a observação foi reproduzida, **não** que o defeito foi corrigido. Foram executados na cópia temporária, sem mudar as fontes do repositório remoto. Não houve teste de carga, avaliação de fatura, validação de um deploy existente ou teste de captura no TikTok Studio.

## Ordem sugerida das alterações

1. Consolidar v4.1 no layout correto e fazer a suíte completa passar.
2. Corrigir refresh do painel e perfil compartilhado; exigir segredos válidos.
3. Entregar `avatar_url`, validação estrita e contexto de pausa por evento.
4. Definir recuperação de presentes, simulação isolada e política de sessões inativas.
5. Medir uma live real e só então decidir persistência, transporte e hospedagem adicionais.

Depois de corrigir a API, atualizar o commit/versão deste relatório e do contrato. A IA que construir o jogo deve integrar a versão efetiva, sem presumir que estas propostas já existem.
