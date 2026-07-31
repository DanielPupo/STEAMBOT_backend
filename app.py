import sys

if sys.platform != "win32":
    try:
        from gevent import monkey
        monkey.patch_all()
    except ImportError:
        print("Gevent não instalado!")

from flask import Flask, request, session, jsonify
from flask_socketio import SocketIO, emit
from google import genai
from google.genai import types
from dotenv import load_dotenv
from uuid import uuid4
import os

# Carrega chaves privadas do ambiente
load_dotenv()

MODELO = "gemini-2.5-flash"

# INSTRUÇÕES PARA O CHATBOT DA STEAM
instrucoes = """
Você é o Sparky, o assistente virtual inteligente e tutor principal da plataforma STEAM+, especializada em robótica, cultura maker e educação tecnológica com kits e blocos LEGO.
Sua missão é guiar com excelência tanto ALUNOS quanto PROFESSORES do Ensino Fundamental II, adaptando sua abordagem de acordo com quem está interagindo.

antes de começar a ajudar tanto o aluno quanto o professor realize a seguinte pergunta: "Olá! Antes de te auxiliar, gostaria de saber, você é um Aluno(a) ou um Professor de STEAM+?".
Assim que a pergunta for respondida siga para o atendimento por perfil.

--- DIRETRIZES DE ATENDIMENTO POR PERFIL ---

1. QUANDO ATENDER ALUNOS:
- Tom de Voz: EntUSIASTA, motivador, amigável e acessível.
- Foco: Resposta prática sobre montagens, lógica de programação e conceitos de robótica.
- Gamificação Ativa: Incentive o ganho de **XP**, conclusão de missões e conquista de **stickers** e **insígnias** exclusivas.
- Linguagem: Use metáforas criativas envolvendo engrenagens, sensores, encaixe de blocos e lógica de robôs.

2. QUANDO ATENDER PROFESSORES:
- Tom de Voz: Profissional, colaborativo, pedagógico e consultivo.
- Foco: Suporte na gestão de turmas, metodologias ativas, planos de aula e mediação de projetos maker.
- Sistema de Avaliação: Explique como funciona o **sistema de notas automatizado por equipes** e como aplicar rubricas de avaliação prática.

--- ESCOPO DE CONHECIMENTO ---

A. RECURSOS DO SITE E PLATAFORMA STEAM+:
- Gamificação: Como acumular XP, desbloquear selos e evoluir no ranking.
- Módulo LEGO: Como acessar os guias de construção e desafios semanais.
- Gestão de Equipes: Como funciona a divisão de papéis no grupo e a emissão de notas/relatórios para docentes.

B. DÚVIDAS DA MATÉRIA E ROBÓTICA:
- Conceitos de Engenharia: Vantagem mecânica, engrenagens (redução e multiplicação de torque), alavancas e estruturas.
- Programação e Automação: Lógica de blocos, estruturas de repetição (loops), condicionais (se/senão) e leitura de sensores (cor, ultra-sônico, toque, giroscópio).
- Cultura Maker: Etapas de ideação, prototipagem rápida, teste de falhas e documentação de projetos.

--- REGRAS DE FORMATO E RESPOSTA ---
- Respostas Concisas e Diretas: Explique os conceitos de forma escaneável, sem enrolação e dividindo em passos claros quando necessário.
- Destaques Visuais: Utilize **negritos inteligentes** em nomes de sensores, blocos, conceitos e etapas importantes.
- Encerramento Dinâmico: Finalize sempre sugerindo um próximo passo prático, desafio ou pergunta reflexiva adequada ao usuário.
"""

client = genai.Client(api_key=os.getenv("GENAI_KEY"))
app = Flask(__name__)
app.secret_key = "STEAM_bot_key"
socketio = SocketIO(app, cors_allowed_origins="*")
active_chats = {}

def get_user_chat():
    if 'session_id' not in session:
        session['session_id'] = str(uuid4())

    session_id = session['session_id']

    if session_id not in active_chats:
        try:
            chat_session = client.chats.create(
                model=MODELO,
                config=types.GenerateContentConfig(system_instruction=instrucoes)
            )
            active_chats[session_id] = chat_session
        except Exception as e:
            app.logger.error(f"Erro ao criar chat Gemini para {session_id}: {e}", exc_info=True)
            raise  
    
    if session_id in active_chats and active_chats[session_id] is None:
        try:
            chat_session = client.chats.create(
                model=MODELO,
                config=types.GenerateContentConfig(system_instruction=instrucoes)
            )
            active_chats[session_id] = chat_session
        except Exception as e:
            app.logger.error(f"Erro ao recriar chat Gemini para {session_id}: {e}", exc_info=True)
            raise

    return active_chats[session_id]

@app.route('/')
def root():
    return jsonify({
        "plataforma": "STEAM+ Hub",
        "assistente": "Sparky",
        "modulo": "Robótica & Cultura Maker LEGO",
        "status": "Operacional",
        "servico": "STEAM+ Sparky Chatbot API"
    })

@socketio.on('connect')
def handle_connect():
    try:
        get_user_chat()
        user_session_id = session.get('session_id', 'N/A')
        emit('status_conexao', {'data': 'Conectado à STEAM Bot.', 'session_id': user_session_id})
    except Exception as e:
        app.logger.error(f"Erro no connect: {e}", exc_info=True)
        emit('erro', {'erro': 'O Bot está em manutenção de suas peças, aguarde ou tente novamente.'})

@socketio.on('enviar_mensagem')
def handle_enviar_mensagem(data):
    try:
        mensagem_usuario = data.get("mensagem")
        if not mensagem_usuario:
            emit('erro', {"erro": "A mensagem não pode ser vazia."})
            return

        user_chat = get_user_chat()
        if user_chat is None:
            emit('erro', {"erro": "Sessão perdida com o Bot Lego"})
            return

        resposta_gemini = user_chat.send_message(mensagem_usuario)
        resposta_texto = (
            resposta_gemini.text
            if hasattr(resposta_gemini, 'text')
            else resposta_gemini.candidates[0].content.parts[0].text
        )
        
        emit('nova_mensagem', {"remetente": "bot", "texto": resposta_texto, "session_id": session.get('session_id')})

    except Exception as e:
        app.logger.error(f"Erro ao processar mensagem: {e}", exc_info=True)
        emit('erro', {"erro": "Houve uma interrupção na montagem das peças. Por favor, tente enviar novamente."})

@socketio.on('disconnect')
def handle_disconnect():
    pass

if __name__ == "__main__":
    socketio.run(app)