import os
import unittest

os.environ.setdefault("GENAI_KEY", "test-key")
os.environ.setdefault("RATE_LIMIT_MESSAGES", "12")

import app as app_module


class FakeResponse:
    text = "Resposta de teste do **Sparky**."


class FakeChat:
    def send_message(self, _message):
        return FakeResponse()


class FakeChats:
    def create(self, **_kwargs):
        return FakeChat()


class FakeGenAIClient:
    chats = FakeChats()


class SparkyBackendTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        app_module.genai_client = FakeGenAIClient()
        app_module.active_chats.clear()
        app_module.message_timestamps.clear()
        self.http_client = app_module.app.test_client()

    def test_health_and_metadata(self):
        root_response = self.http_client.get("/")
        health_response = self.http_client.get("/health")

        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(root_response.get_json()["versao"], app_module.APP_VERSION)
        self.assertEqual(health_response.status_code, 200)
        self.assertTrue(health_response.get_json()["ready"])

    def test_socket_flow_validation_response_and_reset(self):
        socket_client = app_module.socketio.test_client(
            app_module.app,
            flask_test_client=self.http_client,
        )
        self.assertTrue(socket_client.is_connected())

        initial_events = socket_client.get_received()
        self.assertIn("status_conexao", [event["name"] for event in initial_events])

        socket_client.emit("enviar_mensagem", {"mensagem": "   "})
        validation_events = socket_client.get_received()
        self.assertIn("erro", [event["name"] for event in validation_events])

        socket_client.emit("enviar_mensagem", {"mensagem": "Sou aluno."})
        response_events = socket_client.get_received()
        event_names = [event["name"] for event in response_events]
        self.assertIn("nova_mensagem", event_names)
        self.assertEqual(event_names.count("status_bot"), 2)

        socket_client.emit("resetar_conversa")
        reset_events = socket_client.get_received()
        self.assertIn("conversa_resetada", [event["name"] for event in reset_events])
        socket_client.disconnect()


if __name__ == "__main__":
    unittest.main()
