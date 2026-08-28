# STEAM+ Sparky — backend

API Flask + Socket.IO responsável pelas sessões de conversa do Sparky e pela integração com o Gemini.

## Configuração local

1. Crie um ambiente virtual e instale `requirements.txt`.
2. Copie `.env.example` para `.env` e preencha `GENAI_KEY` e `FLASK_SECRET_KEY`.
3. Execute `python app.py`.
4. Verifique `GET /health`; `ready: true` indica que a integração está configurada.

`GEMINI_MODEL` define o modelo principal. Quando o provedor responder com erro
transitório (`429` ou `5xx`), o backend tenta automaticamente os modelos de
`GEMINI_FALLBACK_MODELS`, na ordem configurada.

## Eventos Socket.IO

- A conexão deve enviar `auth: { "role": "student" }` ou `auth: { "role": "teacher" }`.
- `status_conexao`: confirma a sessão, o perfil normalizado e entrega a mensagem inicial.
- `enviar_mensagem`: recebe `{ "mensagem": "..." }`.
- `status_bot`: informa `processando` ou `concluido`.
- `nova_mensagem`: entrega a resposta do Sparky.
- `resetar_conversa`: descarta o contexto atual.
- `conversa_resetada`: confirma a criação de uma conversa limpa.
- `erro`: devolve uma mensagem segura para a interface.

Valores de perfil ausentes ou desconhecidos usam `student`. O perfil controla a
experiência do chatbot, mas não substitui a autorização da plataforma. Recursos de
administração de turmas e alunos devem continuar protegidos pelo login e pelas permissões
do servidor principal.

## Produção

No Render, configure as variáveis do `.env.example`. Restrinja `ALLOWED_ORIGINS` aos domínios reais do frontend. O comando recomendado é:

```bash
gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 1 app:app
```

Use um único worker enquanto as conversas estiverem armazenadas em memória. Para escalar horizontalmente, mova sessões e fila de mensagens para Redis.
