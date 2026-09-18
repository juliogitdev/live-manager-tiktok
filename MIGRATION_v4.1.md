# Atualização v4.1 — monitor contextual

Esta atualização corrige falsos positivos do monitor sem alterar a forma básica de conexão TikTok ↔ Roblox.

## Como atualizar o repositório já publicado

1. Substitua os arquivos do repositório pelos arquivos deste pacote.
2. Faça commit/push para a branch usada pelo Render.
3. Como o serviço foi criado por Blueprint, o Render deve sincronizar `render.yaml` e redeployar.
4. Não troque `API_KEY`, `SECRET_KEY` ou `DASHBOARD_PASSWORD` existentes.
5. O `render.yaml` desta versão já remove `maxShutdownDelaySeconds`, incompatível com o plano Free.

## Novas variáveis

O Blueprint adiciona valores padrão:

```env
DIAGNOSTIC_HISTORICAL_GRACE_SECONDS=15
DIAGNOSTIC_FRESH_MAX_SECONDS=120
DIAGNOSTIC_STARTUP_QUARANTINE_SECONDS=20
DIAGNOSTIC_CORROBORATION_SECONDS=20
DIAGNOSTIC_REPEAT_SUPPRESS_SECONDS=30
```

Não é necessário alterá-las para o primeiro teste.

## Novo comportamento

- Mensagem antiga recebida no bootstrap: `HISTORICAL`, nunca pausa.
- `GiftDynamicRestrictionEvent` vazio e atual: `OBSERVATION`, nunca pausa sozinho.
- Sinal atual com conteúdo relevante: `ALERT`, não pausa sozinho.
- Sinal forte atual + conteúdo, ou dois alertas de tipos diferentes corroborados: `CRITICAL`.
- Somente `CRITICAL` pode pausar as ações automáticas.
- `InRoomBannerEvent` vazio e outros infos vazios são suprimidos.
- `UnknownEvent` não salva mais route params/raw completo; guarda apenas métodos e IDs resumidos.

## O que observar após o deploy

Na aba **Monitor**:

- `Observação`: sinais fracos/sem conteúdo.
- `Alerta`: sinais relevantes que merecem revisão.
- `Crítico`: confirmação suficiente para pausa conservadora.
- `Histórico`: backlog anterior à conexão.
- `Suprimidos`: banners vazios, duplicatas e ruído descartado.

O filtro padrão mostra apenas `OBSERVATION`, `ALERT` e `CRITICAL`.
