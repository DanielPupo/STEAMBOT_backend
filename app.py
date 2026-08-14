import os
import sys
from uuid import uuid4

# O monkey patch precisa acontecer antes dos imports que dependem
# de concorrência/rede.
if sys.platform != "win32":
    try:
        from gevent import monkey
        monkey.patch_all()
    except ImportError:
        print("AVISO: Gevent não está instalado.")


from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit
from google import genai
from google.genai import types


# ============================================================
# CONFIGURAÇÕES
# ============================================================

load_dotenv()

MODELO = "gemini-3.7-flash"
GENAI_KEY = os.getenv("GENAI_KEY")

if not GENAI_KEY:
    raise RuntimeError(
        "A variável de ambiente GENAI_KEY não foi configurada."
    )


INSTRUCOES = """
Você é o Sparky, o assistente virtual inteligente e tutor principal da plataforma STEAM+, especializada em robótica, cultura maker e educação tecnológica com kits e blocos LEGO.

Sua missão é guiar ALUNOS e PROFESSORES do Ensino Fundamental II, adaptando sua abordagem de acordo com quem está interagindo.

Antes de começar a ajudar, faça esta pergunta:

"Olá! Antes de te auxiliar, gostaria de saber, você é um Aluno(a) ou um Professor de STEAM+?"

Assim que a pessoa responder, siga o atendimento de acordo com o perfil.

--- ALUNOS ---

- Tom: entusiasmado, motivador, amigável e acessível.
- Foco: montagem, programação, lógica e robótica.
- Incentive XP, missões, stickers e insígnias.
- Use metáforas relacionadas a engrenagens, sensores, blocos e robôs.

--- PROFESSORES ---

- Tom: profissional, colaborativo e pedagógico.
- Foco: gestão de turmas, metodologias ativas, planos de aula e projetos maker.
- Explique o sistema de notas e rubricas de avaliação quando necessário.

--- CONHECIMENTOS ---

- Gamificação da plataforma STEAM+.
- Guias de construção e desafios.
- Gestão de equipes.
- Engrenagens, torque, alavancas e estruturas.
- Programação de blocos.
- Loops, condicionais e sensores.
- Cultura Maker, prototipagem, testes e documentação.

--- FORMATO DAS RESPOSTAS ---

- Seja direto e fácil de entender.
- Divida explicações em passos quando necessário.
- Utilize negrito para destacar conceitos importantes.
- Finalize sugerindo um próximo passo prático, desafio ou pergunta.
"""


# ============================================================
# APLICAÇÃO
# ============================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "FLASK_SECRET_KEY",
    "steam-hub-dev-key"
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="gevent"
)


# Cliente oficial da API Gemini
client = genai.Client(api_key=GENAI_KEY)


# ============================================================
# SESSÕES DOS CHATS
# ============================================================

# Estrutura:
# {
#     "socket_session_id": chat_do_gemini
# }
#
# Importante:
# Isso mantém o contexto enquanto o processo do servidor estiver
# ativo. Ainda não é um histórico permanente em banco de dados.

active_chats = {}


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def criar_chat():
    """
    Cria uma nova conversa com o Gemini.
    """

    return client.chats.create(
        model=MODELO,
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCOES
        )
    )


def obter_chat():
    """
    Retorna o chat associado à conexão atual.
    Cria um novo chat quando necessário.
    """

    socket_session_id = request.sid

    if socket_session_id not in active_chats:
        app.logger.info(
            f"Criando nova conversa Gemini: {socket_session_id}"
        )

        active_chats[socket_session_id] = criar_chat()

    return active_chats[socket_session_id]


def remover_chat():
    """
    Remove o chat da memória quando o usuário desconecta.
    """

    socket_session_id = request.sid

    if socket_session_id in active_chats:
        del active_chats[socket_session_id]

        app.logger.info(
            f"Conversa removida da memória: {socket_session_id}"
        )


# ============================================================
# ROTAS HTTP
# ============================================================

@app.route("/")
def root():
    return jsonify({
        "plataforma": "STEAM+ Hub",
        "assistente": "Sparky",
        "modulo": "Robótica & Cultura Maker LEGO",
        "status": "Operacional",
        "servico": "STEAM+ Sparky Chatbot API"
    })


@app.route("/health")
def health_check():
    """
    Endpoint simples para verificar se o backend está funcionando.
    """

    return jsonify({
        "status": "online"
    }), 200


# ============================================================
# SOCKET.IO - CONEXÃO
# ============================================================

@socketio.on("connect")
def handle_connect():
    try:
        socket_session_id = request.sid

        app.logger.info(
            f"Socket conectado: {socket_session_id}"
        )

        # Não criamos o Gemini aqui.
        # A criação acontece somente quando a primeira mensagem
        # for enviada.
        emit("status_conexao", {
            "conectado": True,
            "session_id": socket_session_id
        })

    except Exception as error:
        app.logger.exception(
            f"Erro durante conexão Socket.IO: {error}"
        )

        emit("erro", {
            "erro": "Não foi possível estabelecer a conexão com o Sparky."
        })


# ============================================================
# SOCKET.IO - ENVIO DE MENSAGEM
# ============================================================

@socketio.on("enviar_mensagem")
def handle_enviar_mensagem(data):
    try:
        # --------------------------------------------------------
        # Validação
        # --------------------------------------------------------

        if not isinstance(data, dict):
            emit("erro", {
                "erro": "Formato de mensagem inválido."
            })
            return

        mensagem_usuario = str(
            data.get("mensagem", "")
        ).strip()

        if not mensagem_usuario:
            emit("erro", {
                "erro": "A mensagem não pode ser vazia."
            })
            return

        # --------------------------------------------------------
        # Obtém/cria a conversa
        # --------------------------------------------------------

        user_chat = obter_chat()

        # --------------------------------------------------------
        # Informa ao frontend que o bot está processando
        # --------------------------------------------------------

        emit("status_bot", {
            "status": "processando"
        })

        app.logger.info(
            f"Mensagem recebida de {request.sid}: "
            f"{mensagem_usuario[:100]}"
        )

        # --------------------------------------------------------
        # Envia para o Gemini
        # --------------------------------------------------------

        resposta_gemini = user_chat.send_message(
            mensagem_usuario
        )

        # --------------------------------------------------------
        # Validação da resposta
        # --------------------------------------------------------

        resposta_texto = getattr(
            resposta_gemini,
            "text",
            None
        )

        if not resposta_texto:
            raise RuntimeError(
                "O Gemini retornou uma resposta vazia."
            )

        # --------------------------------------------------------
        # Envia resposta para o frontend
        # --------------------------------------------------------

        emit("nova_mensagem", {
            "remetente": "bot",
            "texto": resposta_texto,
            "session_id": request.sid
        })

        emit("status_bot", {
            "status": "concluido"
        })

        app.logger.info(
            f"Resposta enviada com sucesso para {request.sid}"
        )

    except Exception as error:
        # Este log é MUITO importante para descobrirmos
        # o verdadeiro motivo caso o Gemini falhe.
        app.logger.exception(
            f"Erro ao processar mensagem: {error}"
        )

        emit("status_bot", {
            "status": "concluido"
        })

        emit("erro", {
            "erro": (
                "Não foi possível processar sua mensagem. "
                "Verifique o servidor e tente novamente."
            )
        })


# ============================================================
# SOCKET.IO - DESCONEXÃO
# ============================================================

@socketio.on("disconnect")
def handle_disconnect():
    app.logger.info(
        f"Socket desconectado: {request.sid}"
    )

    remover_chat()


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000))
    )