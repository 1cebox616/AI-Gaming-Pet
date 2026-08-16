"""WebSocket bridge tests."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pet.main import ALLOWED_ORIGINS, app


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


def assert_state_message(
    message: dict[str, Any],
    *,
    speech_enabled: bool,
    muted: bool,
    game: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None,
) -> None:
    """Assert the complete authoritative runtime state protocol message."""
    expected = {
        "type": "state",
        "speech_enabled": speech_enabled,
        "muted": muted,
        "game": game
        or {
            "state": "offline",
            "mode": None,
            "map": None,
            "round": None,
            "score_ct": None,
            "score_t": None,
            "subject_steamid": None,
            "subject_is_self": None,
        },
    }
    if llm is not None:
        expected["llm"] = llm
    assert message == expected


def assert_initial_messages(websocket: Any) -> None:
    """Assert the state-first handshake and nonempty greeting."""
    state = websocket.receive_json()
    assert state["type"] == "state"
    assert isinstance(state["speech_enabled"], bool)
    assert isinstance(state["muted"], bool)
    assert isinstance(state["game"], dict)
    assert state["game"]["state"] in {
        "offline",
        "menu",
        "warmup",
        "playing",
        "spectating",
        "round_over",
        "match_over",
    }
    assert_utterance_message(websocket.receive_json())


def test_websocket_allows_whitelisted_origin() -> None:
    """A browser connection from an existing development origin is accepted."""
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws",
            headers={"Origin": ALLOWED_ORIGINS[0]},
        ) as websocket:
            assert_initial_messages(websocket)


def test_websocket_allows_missing_origin() -> None:
    """Native clients and test tools without Origin remain supported."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert_initial_messages(websocket)


def test_websocket_rejects_unknown_origin_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An untrusted browser origin is rejected before the bridge accepts it."""
    rejected_origin = "https://untrusted.example"
    caplog.set_level("WARNING", logger="pet.main")

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "/ws",
                headers={"Origin": rejected_origin},
            ):
                pytest.fail("untrusted WebSocket origin was unexpectedly accepted")

    assert error.value.code == 1008
    assert rejected_origin in caplog.text


def test_websocket_greets_requests_idle_lines_and_survives_invalid_json() -> None:
    """The real bridge greets, replies, and remains usable after invalid input."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first_websocket:
            first_state = first_websocket.receive_json()
            assert first_state["type"] == "state"
            assert isinstance(first_state["speech_enabled"], bool)
            assert isinstance(first_state["muted"], bool)
            assert isinstance(first_state["game"], dict)
            first_greeting = first_websocket.receive_json()
            assert_utterance_message(first_greeting)

            with client.websocket_connect("/ws") as second_websocket:
                assert second_websocket.receive_json() == first_state
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


def test_runtime_switches_broadcast_authoritative_state_and_reject_invalid_values() -> None:
    """Both switches broadcast real state while invalid values leave the socket usable."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as first_websocket:
            initial_state = first_websocket.receive_json()
            first_websocket.receive_json()

            with client.websocket_connect("/ws") as second_websocket:
                assert second_websocket.receive_json() == initial_state
                second_websocket.receive_json()

                requested_speech = not initial_state["speech_enabled"]
                first_websocket.send_json(
                    {"type": "set_speech_enabled", "value": requested_speech}
                )
                assert_state_message(
                    first_websocket.receive_json(),
                    speech_enabled=requested_speech,
                    muted=initial_state["muted"],
                    game=initial_state["game"],
                    llm=initial_state.get("llm"),
                )
                assert_state_message(
                    second_websocket.receive_json(),
                    speech_enabled=requested_speech,
                    muted=initial_state["muted"],
                    game=initial_state["game"],
                    llm=initial_state.get("llm"),
                )

                requested_muted = not initial_state["muted"]
                second_websocket.send_json({"type": "set_muted", "value": requested_muted})
                assert_state_message(
                    first_websocket.receive_json(),
                    speech_enabled=requested_speech,
                    muted=requested_muted,
                    game=initial_state["game"],
                    llm=initial_state.get("llm"),
                )
                assert_state_message(
                    second_websocket.receive_json(),
                    speech_enabled=requested_speech,
                    muted=requested_muted,
                    game=initial_state["game"],
                    llm=initial_state.get("llm"),
                )

                second_websocket.send_json(
                    {"type": "set_speech_enabled", "value": "not-a-boolean"}
                )
                second_websocket.send_json({"type": "set_muted", "value": 1})
                second_websocket.send_json({"type": "request_idle_line"})
                assert_utterance_message(second_websocket.receive_json())

                first_websocket.send_json(
                    {
                        "type": "set_speech_enabled",
                        "value": initial_state["speech_enabled"],
                    }
                )
                first_websocket.receive_json()
                second_websocket.receive_json()
                first_websocket.send_json(
                    {"type": "set_muted", "value": initial_state["muted"]}
                )
                first_websocket.receive_json()
                second_websocket.receive_json()
