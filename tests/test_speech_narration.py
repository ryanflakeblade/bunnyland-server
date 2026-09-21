"""Player speech formatting preserves visibility without exposing intent metadata."""

import pytest

from bunnyland.tui.events import EventNarrator


@pytest.mark.parametrize("show_icons", [True, False])
@pytest.mark.parametrize("directed", [True, False])
def test_speech_shows_dialogue_and_delivery_only(show_icons, directed):
    event = {
        "event_id": "speech-1",
        "actor_id": "lin",
        "visibility": "directed" if directed else "room",
        "room_id": "forest",
        "target_ids": ["lu"] if directed else [],
        "text": "师兄何出此言？",
        "approach": "低声、诚恳",
        "author_intent": "reassure",
        "inferred_intent": "neutral",
        "final_interpretation": "neutral",
    }
    original = dict(event)
    message = {
        "event_type": "SpeechToldEvent" if directed else "SpeechSaidEvent",
        "event": event,
    }
    narrator = EventNarrator()
    names = {"lin": "林冲", "lu": "鲁智深"}
    lines = narrator.drain_events(
        [message],
        player_id="lu",
        room_of=lambda _: "forest",
        name_for=names.get,
        show_icons=show_icons,
    )
    expected = "林冲（低声、诚恳）" + (" → 鲁智深" if directed else "") + "：师兄何出此言？"
    assert [line.plain for line in lines] == [("💬 " if show_icons else "") + expected]
    assert event == original
    assert not narrator.drain_events(
        [message],
        player_id="lu",
        room_of=lambda _: "forest",
        name_for=names.get,
    )
    if directed:
        assert not EventNarrator().drain_events(
            [message],
            player_id="dong",
            room_of=lambda _: "forest",
            name_for=names.get,
        )


def test_speech_without_names_or_delivery_does_not_leak_entity_ids():
    lines = EventNarrator().drain_events(
        [
            {
                "event_type": "SpeechToldEvent",
                "event": {
                    "event_id": "speech-2",
                    "actor_id": "unknown",
                    "target_ids": ["lu"],
                    "text": "你好。",
                    "approach": None,
                },
            }
        ],
        player_id="lu",
        room_of=lambda _: None,
        name_for=lambda _: None,
        show_icons=False,
    )
    assert lines[0].plain == "Someone：你好。"
