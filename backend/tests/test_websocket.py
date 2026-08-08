"""WebSocket bridge tests."""

from typing import Any

from fastapi.testclient import TestClient

from pet.main import app


def assert_utterance_message(message: dict[str, Any]) -> None:
    """Assert that a received payload is a usable utterance protocol message."""
    assert message["type"] == "utterance"
    assert isinstance(message["id"], str)
    assert message["id"]
    assert isinstance(message["text"], str)
    assert message["text"]
    assert message["emotion"] in {
        "neutral",
        "happy",
        "angry",
        "surprised",
        "speechless",
    }


def test_websocket_greets_requests_idle_lines_and_survives_invalid_json() -> None:
    """The real bridge greets, replies, and remains usable after invalid input."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first_websocket:
            first_greeting = first_websocket.receive_json()
            assert_utterance_message(first_greeting)

            with client.websocket_connect("/ws") as second_websocket:
                second_greeting = second_websocket.receive_json()
                assert_utterance_message(second_greeting)
                assert second_greeting["id"] != first_greeting["id"]

                first_websocket.send_json({"type": "request_idle_line"})
                first_reply = first_websocket.receive_json()
                assert_utterance_message(first_reply)
                assert first_reply["id"] != first_greeting["id"]

                second_websocket.send_json({"type": "unknown"})
                second_websocket.send_text("{not valid JSON")
                second_websocket.send_json({"type": "request_idle_line"})
                second_reply = second_websocket.receive_json()
                assert_utterance_message(second_reply)
                assert second_reply["id"] != second_greeting["id"]
