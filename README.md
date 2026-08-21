# STEAM+ Sparky — backend

API Flask + Socket.IO responsável pelas sessões de conversa do Sparky e pela integração com o Gemini.

## Configuração local

1. Crie um ambiente virtual e instale `requirements.txt`.
2. Copie `.env.example` para `.env` e preencha `GENAI_KEY` e `FLASK_SECRET_KEY`.
3. Execute `python app.py`.
4. Verifique `GET /health`; `ready: true` indica que a integração está configurada.

## Eventos Socket.IO

- `status_conexao`: confirma a sessão e entrega a mensagem inicial.
- `enviar_mensagem`: recebe `{ "mensagem": "..." }`.
- `status_bot`: informa `processando` ou `concluido`.
- `nova_mensagem`: entrega a resposta do Sparky.
- `resetar_conversa`: descarta o contexto atual.
- `conversa_resetada`: confirma a criação de uma conversa limpa.
- `erro`: devolve uma mensagem segura para a interface.

## Produção

No Render, configure as variáveis do `.env.example`. Restrinja `ALLOWED_ORIGINS` aos domínios reais do frontend. O comando recomendado é:

```bash
gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 1 app:app
```

Use um único worker enquanto as conversas estiverem armazenadas em memória. Para escalar horizontalmente, mova sessões e fila de mensagens para Redis.
