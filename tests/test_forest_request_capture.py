"""Offline, inspectable HTTP requests from real forest dispatch and prompt building."""

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from relics import EntityId

from bunnyland.core.controllers import LLMControllerComponent, WebControllerComponent
from bunnyland.core.ecs import spawn_entity
from bunnyland.core.edges import ControlledBy
from bunnyland.core.world_actor import WorldActor
from bunnyland.llm_agents.agent import OpenRouterAgent
from bunnyland.llm_agents.player_turn_dispatch import PlayerTurnDispatch
from bunnyland.llm_agents.tools import ToolCall, command_from_tool_call
from bunnyland.plugins import apply_plugins, bunnyland_plugins
from bunnyland.plugins.loader import collect_content_items
from bunnyland.prompts.builder import PromptBuilder
from bunnyland.worldgen import GenOptions
from bunnyland.worldgen.wild_boar_forest import SECRETS, wild_boar_forest_generator

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


async def _act(actor: WorldActor, character_id: EntityId, call: ToolCall) -> None:
    edge, controller_id = actor.world.get_entity(character_id).get_relationships(ControlledBy)[0]
    command = command_from_tool_call(
        call,
        character_id=str(character_id),
        controller_id=str(controller_id),
        controller_generation=edge.generation,
        submitted_at_epoch=actor.epoch,
        definitions=actor.action_definitions(),
    )
    assert (await actor.submit(command)).accepted
    await actor.tick(1)


async def test_forest_requests_show_history_and_private_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Capture three SDK request bodies without opening a network connection."""
    requests: list[dict[str, JsonValue]] = []
    replies = (
        "【测试预设回复一】师兄，我没有娘子的近况。",
        "【测试预设回复二】还请师兄替我打听。",
        "【测试预设回复三】我们在此稍歇。",
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "forest-demo.invalid"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.method == "POST"
        requests.append(JSON_OBJECT.validate_json(request.content))
        index = len(requests) - 1
        return httpx.Response(
            200,
            json={
                "id": f"offline-{index}",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen3.8-flash",
                "system_fingerprint": None,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{index}",
                                    "type": "function",
                                    "function": {
                                        "name": "say",
                                        "arguments": json.dumps(
                                            {"text": replies[index]}, ensure_ascii=False
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    # Replace the transport before constructing the real SDK. No credentials or sockets.
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(respond))
    agent = OpenRouterAgent(
        model="qwen3.8-flash",
        api_key="offline-placeholder",
        server_url="https://forest-demo.invalid/compatible-mode/v1",
        max_retries=0,
    )
    actor = WorldActor()
    plugins = bunnyland_plugins()
    apply_plugins(plugins, actor)
    scene = await wild_boar_forest_generator(actor, "request-demo", GenOptions())
    for cid in scene.characters.values():
        controller = spawn_entity(
            actor.world,
            [
                LLMControllerComponent(
                    profile_name="default", model="qwen3.8-flash", provider="openrouter"
                )
            ],
        )
        actor.assign_controller(cid, controller.id)
    human = spawn_entity(actor.world, [WebControllerComponent(client_id="offline-demo")])
    actor.assign_controller(scene.characters["lu"], human.id)
    # Start capture with everyone already in the clearing; entry itself can wake three NPCs.
    await _act(actor, scene.characters["lu"], ToolCall("move", {"direction": "现身"}))
    builder = PromptBuilder(
        actor.world,
        fragment_providers=actor.prompt_fragment_providers,
        persona_providers=tuple(collect_content_items(plugins, "persona_fragments")),
    )
    dispatch = PlayerTurnDispatch(actor, builder, agent)
    questions = ("林冲，嫂嫂怎样了？", "林冲，你想让我怎么帮你？", "董超，你们在这里做什么？")
    try:
        await dispatch.run_once()
        assert not requests
        for index, question in enumerate(questions, 1):
            await _act(actor, scene.characters["lu"], ToolCall("say", {"text": question}))
            await dispatch.run_once()
            await dispatch.await_pending()
            assert len(requests) == index
            await actor.tick(1)
            await dispatch.run_once()
            await dispatch.await_pending()
            assert len(requests) == index, "AI speech must not trigger an automatic reply chain"

        first, second, third = requests
        assert len(first["messages"]) == 2
        assert len(second["messages"]) == 5
        assert len(third["messages"]) == 2
        for index, body in enumerate(requests):
            offered_tools = body["tools"]
            assert isinstance(offered_tools, list)
            offered_names = set()
            for tool in offered_tools:
                assert isinstance(tool, dict)
                function = tool["function"]
                assert isinstance(function, dict)
                offered_names.add(function["name"])
            assert offered_names == {"look", "say", "tell", "move", "forest_scene", "wait"}
            messages = body["messages"]
            assert isinstance(messages, list)
            last = messages[-1]
            assert isinstance(last, dict)
            current = str(last["content"])
            assert questions[index] in current
            own_role = "dong" if index == 2 else "lin"
            assert SECRETS[own_role] in current
            for role, secret in SECRETS.items():
                if role != own_role:
                    assert secret not in json.dumps(messages, ensure_ascii=False)
        second_text = json.dumps(second["messages"], ensure_ascii=False)
        assert replies[0] in second_text
        assert questions[0] in second_text
        assert first["tools"]

        output = Path(os.environ.get("BUNNYLAND_REQUEST_DEMO_DIR", str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        overview = [
            "# 野猪林：离线 API 请求实录",
            "",
            "真实生成器、点名调度、prompt、历史与 SDK 序列化；HTTP 出口被测试替代。",
            "没有访问模型，没有真实 token 用量。回复为测试预设，不代表 Qwen 的表现。",
            "这是当前代码的新场景示例，不是旧 7,610 token 请求的还原。",
            "鲁智深已进入空地后才启动调度；本例只捕获下面三次点名。",
            "JSON 是发送前的完整请求体，不包含 Authorization 请求头。",
            "角色可听到他人的公开发言；私密上下文和 API 历史仍按角色隔离。",
            "",
            "| 请求 | 玩家输入 | 消息数 | 工具数 | 消息 JSON 字符数 | 工具 JSON 字符数 |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for index, body in enumerate(requests, 1):
            stem = f"request-{index:02d}"
            (output / f"{stem}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            messages = body["messages"]
            tools = body["tools"]
            assert isinstance(messages, list) and isinstance(tools, list)
            overview.append(
                f"| [{index}]({stem}.md) | {questions[index - 1]} | {len(messages)} | "
                f"{len(tools)} | {len(json.dumps(messages, ensure_ascii=False))} | "
                f"{len(json.dumps(tools, ensure_ascii=False))} |"
            )
            readable = [
                f"# 请求 {index}：{questions[index - 1]}",
                "",
                f"完整请求：[JSON]({stem}.json)",
                "",
            ]
            for number, message in enumerate(messages, 1):
                assert isinstance(message, dict)
                readable.extend([f"## 消息 {number} · {message.get('role')}", ""])
                readable.extend(["```text", str(message.get("content") or ""), "```", ""])
                if message.get("tool_calls"):
                    readable.extend(
                        [
                            "```json",
                            json.dumps(message["tool_calls"], ensure_ascii=False, indent=2),
                            "```",
                            "",
                        ]
                    )
            readable.extend(
                [
                    "## 完整工具定义",
                    "",
                    "```json",
                    json.dumps(tools, ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            )
            (output / f"{stem}.md").write_text("\n".join(readable), encoding="utf-8")
        overview.extend(
            ["", "字符数不是 token 数；没有真实模型调用，不能据此给出准确 token 分摊。", ""]
        )
        (output / "README.md").write_text("\n".join(overview), encoding="utf-8")
    finally:
        dispatch.close()
        await agent.close()
