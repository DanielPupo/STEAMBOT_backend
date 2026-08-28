import os
import secrets
import sys
from collections import defaultdict, deque
from time import monotonic

# O monkey patch deve acontecer antes dos imports que utilizam rede.
if sys.platform != "win32":
    try:
        from gevent import monkey

        monkey.patch_all()
    except ImportError:
        print("AVISO: gevent não está instalado; usando o modo disponível.")

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from google import genai
from google.genai import types


load_dotenv()

APP_NAME = "STEAM+ Sparky Chatbot API"
APP_VERSION = "2.1.0"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
CONFIGURED_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
    if model.strip()
]
GEMINI_FALLBACK_MODELS = list(
    dict.fromkeys(
        [
            *CONFIGURED_FALLBACK_MODELS,
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
        ]
    )
)
MODEL_CHAIN = list(dict.fromkeys([GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]))
GENAI_KEY = os.getenv("GENAI_KEY")
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "1500"))
RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "12"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

DEFAULT_ORIGINS = (
    "https://steambot-frontend.vercel.app,"
    "http://localhost:3000,http://localhost:5500,http://127.0.0.1:5500"
)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

WELCOME_MESSAGES = {
    "student": (
        "Olá! Eu sou o **Sparky**, seu parceiro de missões maker. 🤖 "
        "Escolha um desafio ou conte o que você quer construir hoje!"
    ),
    "teacher": (
        "Olá! Eu sou o **Sparky**, seu assistente pedagógico STEAM+. "
        "Posso apoiar seu planejamento, suas avaliações e a organização das turmas."
    ),
}

BASE_SYSTEM_INSTRUCTIONS = """
Você é o Sparky, tutor virtual da plataforma STEAM+, especializada em robótica,
cultura maker e educação tecnológica com kits e blocos LEGO.

ESCOPO DE CONHECIMENTO
- Gamificação, guias de construção, desafios e gestão de equipes.
- Engrenagens, torque, alavancas, estruturas, loops, condicionais e sensores.
- Cultura maker, prototipagem, testes, programação em blocos e documentação.

PADRÃO DAS RESPOSTAS
- Responda em português do Brasil, de modo direto e fácil de entender.
- Use passos e Markdown somente quando melhorarem a leitura.
- Destaque conceitos essenciais em negrito.
- Não invente funcionalidades, regras ou dados específicos da plataforma.
- Quando faltar contexto, faça uma pergunta objetiva.
- Termine com um próximo passo prático, desafio ou pergunta relevante.
"""

PROFILE_INSTRUCTIONS = {
    "student": """
PERFIL AUTENTICADO: ALUNO
- Nunca mostre atalhos, ferramentas administrativas ou opções exclusivas de professor.
- Use tom entusiasmado, motivador, amigável e apropriado para estudantes.
- Explique um passo de cada vez e confirme o entendimento em tarefas complexas.
- Priorize montagem, programação, lógica, sensores e robótica.
- Transforme conteúdos em missões curtas, perguntas, desafios e conquistas.
- Use XP, stickers, insígnias e metáforas maker sem prejudicar a clareza.
""",
    "teacher": """
PERFIL AUTENTICADO: PROFESSOR
- Use tom profissional, claro, colaborativo e pedagógico.
- Não use XP, missões, mascotes ou linguagem infantilizada nas respostas.
- Priorize planejamento, metodologias ativas, rubricas, gestão de equipes e projetos maker.
- Organize respostas em objetivos, materiais, etapas, tempo e avaliação quando aplicável.
- Não afirme ter consultado turmas, alunos ou notas que não foram fornecidos na conversa.
""",
}


def normalize_profile(role):
    """Converte o perfil recebido pela sessão para um dos perfis suportados."""
    normalized = str(role or "").strip().lower()
    if normalized in {"teacher", "professor", "professora"}:
        return "teacher"
    return "student"


def build_system_instructions(profile):
    """Monta as instruções específicas do perfil autenticado."""
    normalized_profile = normalize_profile(profile)
    return f"{BASE_SYSTEM_INSTRUCTIONS}\n{PROFILE_INSTRUCTIONS[normalized_profile]}"


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32),
    JSON_SORT_KEYS=False,
)

CORS(
    app,
    resources={r"/*": {"origins": ALLOWED_ORIGINS}},
    methods=["GET", "OPTIONS"],
)

socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_ORIGINS,
    async_mode="gevent" if sys.platform != "win32" else "threading",
    ping_interval=25,
    ping_timeout=30,
    logger=False,
    engineio_logger=False,
)

genai_client = genai.Client(api_key=GENAI_KEY) if GENAI_KEY else None
active_chats = {}
session_profiles = {}
message_timestamps = defaultdict(deque)


def create_chat(model, profile):
    """Cria uma conversa independente no modelo informado."""
    if genai_client is None:
        raise RuntimeError("GENAI_KEY não configurada no ambiente do servidor.")

    return genai_client.chats.create(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=build_system_instructions(profile)
        ),
    )


def get_chat(model):
    """Obtém o chat da sessão no modelo pedido ou cria um sob demanda."""
    session_id = request.sid
    profile = session_profiles.get(session_id, "student")
    session_chat = active_chats.get(session_id)

    if (
        not session_chat
        or session_chat["model"] != model
        or session_chat["profile"] != profile
    ):
        app.logger.info(
            "Criando conversa Gemini para a sessão %s com %s", session_id, model
        )
        session_chat = {
            "model": model,
            "profile": profile,
            "chat": create_chat(model, profile),
        }
        active_chats[session_id] = session_chat

    return session_chat["chat"]


def get_error_code(error):
    """Extrai o status HTTP das exceções do SDK sem depender de uma única versão."""
    return getattr(error, "code", None) or getattr(error, "status_code", None)


def is_transient_provider_error(error):
    """Indica falhas nas quais trocar de modelo pode manter o serviço disponível."""
    return get_error_code(error) in {404, 429, 500, 502, 503, 504}


def generate_response(message):
    """Gera uma resposta e troca de modelo em falhas transitórias do provedor."""
    session_chat = active_chats.get(request.sid)
    current_model = session_chat.get("model") if session_chat else None
    candidates = list(dict.fromkeys([current_model, *MODEL_CHAIN]))
    candidates = [model for model in candidates if model]
    last_error = None

    for index, model in enumerate(candidates):
        try:
            response = get_chat(model).send_message(message)
            response_text = getattr(response, "text", None)
            if not isinstance(response_text, str) or not response_text.strip():
                raise RuntimeError("O provedor retornou uma resposta vazia.")
            return response_text.strip(), model
        except Exception as error:
            last_error = error
            has_fallback = index < len(candidates) - 1
            if not has_fallback or not is_transient_provider_error(error):
                raise

            app.logger.warning(
                "Modelo %s indisponível (código %s); tentando o próximo modelo.",
                model,
                get_error_code(error),
            )

    raise last_error or RuntimeError("Nenhum modelo Gemini foi configurado.")


def clear_conversation(session_id=None):
    """Descarta o contexto da conversa, preservando o perfil autenticado."""
    target_session = session_id or request.sid
    active_chats.pop(target_session, None)
    message_timestamps.pop(target_session, None)


def remove_session(session_id=None):
    """Descarta todos os dados temporários de uma sessão encerrada."""
    target_session = session_id or request.sid
    clear_conversation(target_session)
    session_profiles.pop(target_session, None)


def is_rate_limited(session_id):
    """Aplica um limite simples por conexão para proteger a API externa."""
    now = monotonic()
    timestamps = message_timestamps[session_id]
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    while timestamps and timestamps[0] < window_start:
        timestamps.popleft()

    if len(timestamps) >= RATE_LIMIT_MESSAGES:
        return True

    timestamps.append(now)
    return False


def validate_message(data):
    """Normaliza o payload recebido e retorna mensagem ou descrição do erro."""
    if not isinstance(data, dict):
        return None, "Formato de mensagem inválido."

    message = data.get("mensagem")
    if not isinstance(message, str):
        return None, "A mensagem deve ser um texto."

    message = message.strip()
    if not message:
        return None, "A mensagem não pode estar vazia."
    if len(message) > MAX_MESSAGE_LENGTH:
        return None, f"A mensagem deve ter até {MAX_MESSAGE_LENGTH} caracteres."

    return message, None


@app.get("/")
def root():
    return jsonify(
        {
            "servico": APP_NAME,
            "versao": APP_VERSION,
            "assistente": "Sparky",
            "plataforma": "STEAM+ Hub",
            "status": "operacional" if GENAI_KEY else "configuracao_incompleta",
        }
    )


@app.get("/health")
def health_check():
    ready = GENAI_KEY is not None
    return (
        jsonify(
            {
                "status": "online" if ready else "degradado",
                "ready": ready,
                "version": APP_VERSION,
                "model": GEMINI_MODEL,
                "fallback_models": GEMINI_FALLBACK_MODELS,
            }
        ),
        200 if ready else 503,
    )


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"erro": "Rota não encontrada."}), 404


@socketio.on("connect")
def handle_connect(auth=None):
    role = auth.get("role") if isinstance(auth, dict) else None
    profile = normalize_profile(role)
    session_profiles[request.sid] = profile
    app.logger.info("Socket conectado: %s", request.sid)
    emit(
        "status_conexao",
        {
            "conectado": True,
            "session_id": request.sid,
            "profile": profile,
            "mensagem_inicial": WELCOME_MESSAGES[profile],
        },
    )


@socketio.on("enviar_mensagem")
def handle_send_message(data):
    message, validation_error = validate_message(data)
    if validation_error:
        emit("erro", {"erro": validation_error})
        return

    if is_rate_limited(request.sid):
        emit(
            "erro",
            {
                "erro": (
                    "Muitas mensagens em pouco tempo. Aguarde alguns segundos "
                    "antes de tentar novamente."
                )
            },
        )
        return

    emit("status_bot", {"status": "processando"})
    app.logger.info(
        "Mensagem recebida da sessão %s (%d caracteres)", request.sid, len(message)
    )

    try:
        response_text, model_used = generate_response(message)

        emit(
            "nova_mensagem",
            {
                "remetente": "bot",
                "texto": response_text.strip(),
                "session_id": request.sid,
                "model": model_used,
            },
        )
        app.logger.info("Resposta enviada para a sessão %s", request.sid)
    except Exception as error:
        app.logger.exception("Falha ao responder à sessão %s: %s", request.sid, error)
        emit(
            "erro",
            {
                "erro": (
                    "Não foi possível gerar a resposta agora. "
                    "Tente novamente em alguns instantes."
                )
            },
        )
    finally:
        emit("status_bot", {"status": "concluido"})


@socketio.on("resetar_conversa")
def handle_reset_conversation():
    profile = session_profiles.get(request.sid, "student")
    clear_conversation(request.sid)
    app.logger.info("Conversa reiniciada: %s", request.sid)
    emit(
        "conversa_resetada",
        {"profile": profile, "mensagem": WELCOME_MESSAGES[profile]},
    )


@socketio.on("disconnect")
def handle_disconnect():
    app.logger.info("Socket desconectado: %s", request.sid)
    remove_session(request.sid)


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
