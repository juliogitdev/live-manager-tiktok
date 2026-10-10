# Credenciais de jogos no painel

O administrador pode cadastrar e revogar credenciais sem editar `GAME_TOKENS` para cada conta. Todos os endpoints abaixo exigem a sessão autenticada do painel ou o cabeçalho administrativo `X-API-Key`. Tokens de jogos não têm acesso ao cadastro, às regras administrativas, à simulação administrativa nem ao dashboard.

| Operação | Endpoint | Resposta |
|---|---|---|
| Listar | `GET /api/game-access` | `{credentials: [...]}` |
| Criar | `POST /api/game-access` | HTTP 201, `{credential: {...}, token: "game_..."}` |
| Editar | `PATCH /api/game-access/{id}` | `{credential: {...}}` |
| Revogar | `DELETE /api/game-access/{id}` | `{credential: {...}}` |

Criação recebe `{name, consumer_id, usernames}`. Edição aceita apenas os campos enviados; nenhum aceita `token` ou `token_hash`. Uma credencial tem `{id,name,consumer_id,usernames,created_at,updated_at,revoked_at,active}`, com datas em milissegundos UTC. `usernames` aceita de 1 a 100 contas, normaliza o `@` inicial e maiúsculas, elimina duplicatas e não aceita curingas. `consumer_id` aceita letras, números, ponto, hífen, sublinhado e dois-pontos, até 128 caracteres. Há limite de 1.000 credenciais ativas.

O token é gerado com 48 bytes aleatórios e exibido somente na criação, com `Cache-Control: no-store`. O banco armazena somente SHA-256 do token. Listagem, edição e revogação não revelam o segredo. Se ele for perdido, crie outra credencial e revogue a anterior. Credenciais revogadas permanecem no histórico, não podem ser editadas ou reativadas, e a revogação repetida é segura.

Jogos utilizam o token no cabeçalho `X-API-Key` de `/api/bridge/register`, `/api/bridge/{sid}/poll` e `/api/bridge/{sid}/ack`. O token autoriza somente as contas cadastradas e o consumidor vinculado. Conta ou consumidor fora do escopo retorna 403; token inválido ou revogado retorna 401. A autorização é verificada novamente após a espera do polling, para que uma revogação ou mudança de escopo durante a espera não entregue eventos.

As credenciais antigas de `GAME_TOKENS` continuam funcionando, mas não são listadas ou modificadas por esses endpoints. Os cadastros novos ficam na tabela `game_credentials` do mesmo SQLite usado pelo `EventStore`. A persistência sobrevive a reinícios com o mesmo arquivo, mas não à substituição de um sistema de arquivos efêmero: no Render Free, um redeploy ou substituição da instância pode apagar os cadastros. Para persistência entre substituições, a hospedagem precisa preservar `DATABASE_PATH` em armazenamento persistente. Nenhuma durabilidade em disco efêmero é prometida.

Validação: a suíte Python completa passou com 46 testes, incluindo criação administrativa por chave e cookie, armazenamento sem segredo em texto, escopos, compatibilidade com o bridge legado, persistência em reinício, edição, revogação e revogação durante polling em espera. Não houve conexão real ao TikTok, push ou deploy nesta implementação.
