# Autorizar contas pela web

O painel administrativo já permite conectar um @ sem editar variáveis. `GAME_TOKENS` é a lista antiga de contas que cada jogo pode consumir. Agora essa autorização também pode ser cadastrada e alterada na aba **Acesso dos jogos**, com armazenamento no SQLite.

1. Entre no painel com sua senha administrativa.
2. Em **Lives**, informe o @ e escolha o perfil **raw** para o jogo de Lula e Flávio. Conecte e confirme o estado da live.
3. Em **Acesso dos jogos**, crie um acesso com nome `Corrida dos Presidentes`, consumidor `corrida-presidentes` e os @ autorizados.
4. Copie o token mostrado na criação. Ele não volta a aparecer depois: o banco guarda apenas seu hash. Caso o perca, revogue o acesso e crie outro.
5. No jogo, informe o mesmo @ e cole o token no campo **Token de acesso do jogo**. A senha administrativa fica dispensada nesse modo.
6. Para outra conta futura, edite a lista de @ na mesma credencial. O token continua valendo e a mudança entra em vigor nas próximas requisições, sem redeploy ou alteração de `GAME_TOKENS`.

O jogo compartilha a conexão do Live Manager e recebe os eventos normalizados. Desconectar o jogo nesse modo não encerra a sessão que você abriu no painel. O jogo salva os eventos processados antes de confirmar o cursor à API. O token fica na memória do servidor do jogo durante a conexão; não é salvo nas configurações do navegador nem no arquivo das partidas.

As credenciais existentes em `GAME_TOKENS` continuam funcionando por compatibilidade. Elas precisam ser removidas dessa variável uma única vez caso você queira parar de usar o cadastro antigo; a revogação pela web se aplica às credenciais criadas pela web.

## Persistência no Render

O Blueprint atual usa **Render Free**, cujo sistema de arquivos é temporário. Os cadastros do SQLite podem desaparecer em reinícios ou deploys sem armazenamento persistente. Portanto, para manter contas autorizadas e tokens, configure um disco persistente no serviço e `DATABASE_PATH` para um arquivo dentro dele, por exemplo `/var/data/live-manager.sqlite3`. Essa é uma configuração inicial da infraestrutura, não uma alteração por perfil.

Discos persistentes exigem serviço pago no Render. O arquivo `render-persistent.example.yaml` é um modelo opcional para revisão; não substitui o Blueprint atual nem altera seu serviço automaticamente. Nenhum plano foi contratado nem configuração de produção alterada nesta implementação. Consulte a [documentação de discos do Render](https://render.com/docs/disks).

## Endpoints

Todos os endpoints abaixo exigem autenticação administrativa pelo cookie do painel ou por `API_KEY`. Um token de jogo não permite administrar acessos.

- `GET /api/game-access`: lista as credenciais sem token ou hash.
- `POST /api/game-access`: cria a credencial e retorna o token uma única vez.
- `PATCH /api/game-access/{id}`: altera nome, consumidor ou contas autorizadas.
- `DELETE /api/game-access/{id}`: revoga a credencial.

O jogo usa o token no cabeçalho `X-API-Key` dos endpoints `/api/bridge/register`, `/poll` e `/ack`; ele não deve ser colocado em URLs.
