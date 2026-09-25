"""Backend do Sparky (STEAM+).

Responsabilidades deste arquivo:
1. Configurar Flask/Socket.IO/CORS.
2. Manter o contexto temporário de cada conexão.
3. Validar mensagens e limitar uso excessivo.
4. Enviar mensagens ao Gemini com fallback de modelo.
5. Devolver respostas com request_id para evitar respostas antigas no frontend.

IMPORTANTE SOBRE PERFIS
O perfil recebido pelo Socket.IO serve como contexto de experiência do chatbot.
Ele NÃO substitui a autorização real do sistema principal. Quando o AstroLearn
integrar autenticação, o ideal é validar aluno/professor usando token assinado no
backend antes de liberar qualquer ação privilegiada.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections import defaultdict, deque
from time import monotonic
from typing import Any

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

# -----------------------------------------------------------------------------
# Configuração
# -----------------------------------------------------------------------------

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
    "https://frontend-xi-taupe-77.vercel.app,"
    "http://localhost:3000,http://localhost:5500,http://127.0.0.1:5500"
)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

VALID_ROLES = {"student", "teacher"}
ROLE_LABELS = {"student": "aluno(a)", "teacher": "professor(a)"}

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
""".strip()

ROLE_INSTRUCTIONS = {
    "student": """
PERFIL DA SESSÃO: ALUNO(A)
- Use tom entusiasmado, motivador, amigável e acessível.
- Priorize montagem, programação, lógica, robótica e aprendizagem prática.
- Quando fizer sentido, transforme conteúdos em desafios curtos ou missões.
- Não apresente ferramentas administrativas exclusivas de professores.
""".strip(),
    "teacher": """
PERFIL DA SESSÃO: PROFESSOR(A)
- Use tom profissional, colaborativo e pedagógico.
- Priorize metodologias ativas, planos de aula, projetos maker, rubricas e organização de equipes.
- Seja objetivo e facilite a aplicação em sala de aula.
""".strip(),
}

GENERIC_WELCOME = (
    "Olá! Eu sou o **Sparky**, seu copiloto de robótica e cultura maker. "
    "Escolha **Aluno** ou **Professor** para eu adaptar a experiência para você."
)

# -----------------------------------------------------------------------------
# App e estado temporário
# -----------------------------------------------------------------------------

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

# active_chats[sid] = {"model": str, "chat": chat_object, "role": str|None}
active_chats: dict[str, dict[str, Any]] = {}

# session_profiles[sid] = {"role": str|None, "user_name": str|None, "user_id": str|None}
session_profiles: dict[str, dict[str, str | None]] = {}

# Limite simples por conexão. Quando houver autenticação, troque a chave por user_id.
message_timestamps: defaultdict[str, deque[float]] = defaultdict(deque)


# -----------------------------------------------------------------------------
# Helpers de perfil e prompt
# -----------------------------------------------------------------------------

def normalize_role(value: Any) -> str | None:
    """Retorna somente perfis reconhecidos pelo chatbot."""
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    aliases = {
        "student": "student",
        "aluno": "student",
        "aluna": "student",
        "teacher": "teacher",
        "professor": "teacher",
        "professora": "teacher",
    }
    return aliases.get(normalized)


def safe_short_text(value: Any, max_length: int = 80) -> str | None:
    """Normaliza textos de contexto sem aceitar conteúdo excessivo."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.strip().split())
    return value[:max_length] or None


def profile_for_session(session_id: str | None = None) -> dict[str, str | None]:
    target = session_id or request.sid
    return session_profiles.setdefault(
        target,
        {"role": None, "user_name": None, "user_id": None},
    )


def build_system_instructions(session_id: str | None = None) -> str:
    """Monta as instruções da IA com o perfil atual da sessão."""
    profile = profile_for_session(session_id)
    role = profile.get("role")
    user_name = profile.get("user_name")

    parts = [BASE_SYSTEM_INSTRUCTIONS]

    if role in ROLE_INSTRUCTIONS:
        parts.append(ROLE_INSTRUCTIONS[role])
    else:
        parts.append(
            "PERFIL DA SESSÃO: NÃO DEFINIDO\n"
            "- Antes de fornecer orientação específica, pergunte se a pessoa é aluno(a) ou professor(a)."
        )

    if user_name:
        parts.append(f"NOME INFORMADO DO USUÁRIO: {user_name}")

    return "\n\n".join(parts)


def build_welcome_message(session_id: str | None = None) -> str:
    profile = profile_for_session(session_id)
    role = profile.get("role")
    name = profile.get("user_name")

    if role not in VALID_ROLES:
        return GENERIC_WELCOME

    greeting = f"Olá, **{name}**!" if name else "Olá!"
    if role == "student":
        return f"{greeting} Eu sou o **Sparky**. Pronto para construir, programar e aprender? 🚀"
    return f"{greeting} Eu sou o **Sparky**. Posso ajudar com aulas, projetos maker e atividades para sua turma."


def update_profile(data: Any) -> dict[str, str | None]:
    """Atualiza apenas contexto de UX. Não é mecanismo de autorização."""
    profile = profile_for_session()
    if not isinstance(data, dict):
        return profile

    incoming_role = normalize_role(data.get("role"))
    if incoming_role:
        profile["role"] = incoming_role

    incoming_name = safe_short_text(data.get("user_name") or data.get("name"))
    if incoming_name:
        profile["user_name"] = incoming_name

    incoming_id = safe_short_text(data.get("user_id") or data.get("id"), 120)
    if incoming_id:
        profile["user_id"] = incoming_id

    return profile


# -----------------------------------------------------------------------------
# Helpers do Gemini
# -----------------------------------------------------------------------------

def create_chat(model: str):
    """Cria uma conversa independente para o perfil atual."""
    if genai_client is None:
        raise RuntimeError("GENAI_KEY não configurada no ambiente do servidor.")

    return genai_client.chats.create(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=build_system_instructions(),
        ),
    )


def get_chat(model: str):
    """Obtém o chat da sessão ou cria um novo quando modelo/perfil mudam."""
    session_id = request.sid
    role = profile_for_session(session_id).get("role")
    session_chat = active_chats.get(session_id)

    if (
        not session_chat
        or session_chat.get("model") != model
        or session_chat.get("role") != role
    ):
        app.logger.info(
            "Criando conversa para sessão %s | modelo=%s | perfil=%s",
            session_id,
            model,
            role or "indefinido",
        )
        session_chat = {
            "model": model,
            "role": role,
            "chat": create_chat(model),
        }
        active_chats[session_id] = session_chat

    return session_chat["chat"]


def get_error_code(error: Exception):
    """Extrai o status HTTP sem depender de uma única versão do SDK."""
    return getattr(error, "code", None) or getattr(error, "status_code", None)


def is_transient_provider_error(error: Exception) -> bool:
    """Erros em que tentar outro modelo pode manter o serviço disponível."""
    return get_error_code(error) in {404, 429, 500, 502, 503, 504}


def generate_response(message: str) -> tuple[str, str]:
    """Gera resposta e troca de modelo em falhas transitórias."""
    session_chat = active_chats.get(request.sid)
    current_model = session_chat.get("model") if session_chat else None
    candidates = [model for model in dict.fromkeys([current_model, *MODEL_CHAIN]) if model]
    last_error: Exception | None = None

    for index, model in enumerate(candidates):
        try:
            response = get_chat(model).send_message(message)
            response_text = getattr(response, "text", None)
            if not isinstance(response_text, str) or not response_text.strip():
                raise RuntimeError("O provedor retornou uma resposta vazia.")
            return response_text.strip(), model
        except Exception as error:  # SDK pode lançar diferentes classes por versão
            last_error = error
            has_fallback = index < len(candidates) - 1
            if not has_fallback or not is_transient_provider_error(error):
                raise

            app.logger.warning(
                "Modelo %s indisponível (código %s); tentando fallback.",
                model,
                get_error_code(error),
            )

    raise last_error or RuntimeError("Nenhum modelo Gemini foi configurado.")


# -----------------------------------------------------------------------------
# Validação, limite e limpeza
# -----------------------------------------------------------------------------

def remove_session(session_id: str | None = None, keep_profile: bool = False) -> None:
    """Descarta contexto temporário de uma conexão."""
    target = session_id or request.sid
    active_chats.pop(target, None)
    message_timestamps.pop(target, None)
    if not keep_profile:
        session_profiles.pop(target, None)


def is_rate_limited(session_id: str) -> bool:
    """Limite simples por conexão para proteger a API externa."""
    now = monotonic()
    timestamps = message_timestamps[session_id]
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    while timestamps and timestamps[0] < window_start:
        timestamps.popleft()

    if len(timestamps) >= RATE_LIMIT_MESSAGES:
        return True

    timestamps.append(now)
    return False


def validate_message(data: Any) -> tuple[str | None, str | None, str | None]:
    """Retorna (mensagem, request_id, erro)."""
    if not isinstance(data, dict):
        return None, None, "Formato de mensagem inválido."

    message = data.get("mensagem")
    request_id = safe_short_text(data.get("request_id"), 120)

    if not isinstance(message, str):
        return None, request_id, "A mensagem deve ser um texto."

    message = message.strip()
    if not message:
        return None, request_id, "A mensagem não pode estar vazia."
    if len(message) > MAX_MESSAGE_LENGTH:
        return None, request_id, f"A mensagem deve ter até {MAX_MESSAGE_LENGTH} caracteres."

    return message, request_id, None


def emit_error(message: str, request_id: str | None = None, code: str = "request_error") -> None:
    """Padroniza erros enviados ao frontend."""
    emit(
        "erro",
        {
            "erro": message,
            "code": code,
            "request_id": request_id,
        },
    )


# -----------------------------------------------------------------------------
# Rotas HTTP
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Eventos Socket.IO
# -----------------------------------------------------------------------------

@socketio.on("connect")
def handle_connect(auth=None):
    """Recebe contexto opcional enviado durante a conexão."""
    profile_for_session()
    if isinstance(auth, dict):
        update_profile(auth)

    profile = profile_for_session()
    app.logger.info(
        "Socket conectado: %s | perfil=%s",
        request.sid,
        profile.get("role") or "indefinido",
    )

    emit(
        "status_conexao",
        {
            "conectado": True,
            "session_id": request.sid,
            "profile": profile,
            "mensagem_inicial": build_welcome_message(),
        },
    )


@socketio.on("definir_perfil")
def handle_set_profile(data):
    """Atualiza o perfil de experiência e reinicia o contexto da IA."""
    role = normalize_role(data.get("role")) if isinstance(data, dict) else None
    if role not in VALID_ROLES:
        emit_error("Escolha um perfil válido: aluno ou professor.", code="invalid_profile")
        return

    profile = update_profile(data)
    active_chats.pop(request.sid, None)  # evita manter instruções do perfil anterior

    emit(
        "perfil_atualizado",
        {
            "profile": profile,
            "mensagem": build_welcome_message(),
        },
    )


@socketio.on("enviar_mensagem")
def handle_send_message(data):
    message, request_id, validation_error = validate_message(data)
    if validation_error:
        emit_error(validation_error, request_id, "validation_error")
        return

    if is_rate_limited(request.sid):
        emit_error(
            "Muitas mensagens em pouco tempo. Aguarde alguns segundos antes de tentar novamente.",
            request_id,
            "rate_limit",
        )
        return

    emit("status_bot", {"status": "processando", "request_id": request_id})
    app.logger.info(
        "Mensagem recebida da sessão %s (%d caracteres) | request=%s",
        request.sid,
        len(message or ""),
        request_id or "sem-id",
    )

    try:
        response_text, model_used = generate_response(message or "")
        emit(
            "nova_mensagem",
            {
                "remetente": "bot",
                "texto": response_text,
                "session_id": request.sid,
                "model": model_used,
                "request_id": request_id,
            },
        )
    except Exception as error:
        app.logger.exception("Falha ao responder à sessão %s: %s", request.sid, error)
        emit_error(
            "Não foi possível gerar a resposta agora. Tente novamente em alguns instantes.",
            request_id,
            "provider_error",
        )
    finally:
        emit("status_bot", {"status": "concluido", "request_id": request_id})


@socketio.on("resetar_conversa")
def handle_reset_conversation():
    # Mantém o perfil escolhido, mas zera contexto e limite da conversa.
    remove_session(request.sid, keep_profile=True)
    app.logger.info("Conversa reiniciada: %s", request.sid)
    emit(
        "conversa_resetada",
        {
            "mensagem": build_welcome_message(),
            "profile": profile_for_session(),
        },
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
