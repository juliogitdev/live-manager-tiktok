# Análise: contas TikTok e acesso dos jogos

## Origem do trabalho manual

Existem três conceitos no backend:

| Conceito | Função |
| --- | --- |
| Conta TikTok | O @ que tem a live. O painel já permite conectá-lo por `/api/live/connect`, após login. |
| Perfil `raw`, `dance` ou `kite` | Tratamento dos eventos; não é uma autorização de conta. Para a corrida, use `raw`. |
| Credencial de jogo | Token que autoriza um consumidor a receber eventos de determinadas contas. |

A lista restrita de contas dos jogos era lida exclusivamente de `GAME_TOKENS` em `app/auth.py`. Por isso incluir outro @ exigia editar o ambiente. A integração antiga do jogo por senha administrativa não utiliza essa lista; ela continua disponível.

## Implementação

O painel recebeu a aba **Acesso dos jogos**, com criação, edição de contas e revogação. O cadastro contém nome, consumidor e lista de @. A edição entra em vigor sem reinício ou redeploy.

Os registros ficam no SQLite existente. Tokens aleatórios de 48 bytes são mostrados somente na criação; o banco guarda seu hash SHA-256. Listagens não retornam token nem hash. A resposta de criação usa `Cache-Control: no-store`; o navegador limpa o token ao sair da aba ou encerrar a sessão.

As rotas administrativas exigem o login já usado no painel. Os endpoints do bridge verificam conta e consumidor, e o long polling revalida o acesso antes de entregar eventos. Revogar uma credencial ou retirar uma conta impede as requisições seguintes. Tokens de jogos não permitem administrar acessos.

Credenciais de `GAME_TOKENS` continuam funcionando. Elas não são revogadas pela nova tela: remova a configuração antiga uma única vez se quiser substituí-las totalmente pelos cadastros web.

## Integração com a corrida

O jogo recebeu um campo opcional para o token gerado na API. O servidor do jogo envia esse token no cabeçalho, registra um consumidor, lê eventos normalizados e salva os eventos processados antes de confirmar o cursor. O token fica na memória durante a conexão; não é gravado nas partidas nem no armazenamento do navegador.

Desconectar o jogo nesse modo não encerra a sessão aberta pelo painel da API. A integração por senha administrativa permanece compatível. O novo campo depende da publicação desta versão do backend no Render.

## Persistência e capacidade

O SQLite usa WAL e commits, mas precisa estar em armazenamento persistente. O `render.yaml` atual utiliza **Render Free**, sem disco: cadastros podem desaparecer em reinícios ou deploys. Para conservá-los, configure uma vez um disco persistente e `DATABASE_PATH` dentro dele, como `/var/data/live-manager.sqlite3`. A configuração não precisa ser repetida para cada perfil. Discos persistentes exigem um serviço pago. [Documentação oficial](https://render.com/docs/disks).

O limite de conexões ativas permanece `MAX_SESSIONS`, configurado como 5 no Blueprint. Cadastrar mais contas não aumenta esse limite. O backend deve continuar com um worker/processo para compartilhar conexões TikTok e SQLite.

## Validação

- API: 46 testes Python passaram, incluindo persistência, escopos, revogação, long polling e compatibilidade com o ambiente antigo.
- Dashboard: testes Node existentes e navegador real Edge para criar, editar, revogar, limpar o token, escapar nomes e verificar layout móvel.
- Jogo: 33 testes Node passaram, incluindo bridge por token, gravação antes do ACK e integração antiga por senha.

Os testes utilizam TikTok simulado e HTTP local. Nenhuma live real foi conectada durante a validação. O serviço do Render ainda precisa receber esta versão.
