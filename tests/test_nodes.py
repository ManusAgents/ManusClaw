"""Tests for canvas nodes (DeviceManager, protocol serialization)."""

import json
import pytest

from app.nodes.protocol import (
    MessageType,
    NodeRegister,
    NodeRegistered,
    NodeHeartbeat,
    NodeEvent,
    CanvasPush,
    NodeCommand,
    NodeInfo,
    CapabilityChecker,
    serialize_message,
    parse_message,
    parse_node_register,
    parse_node_event,
)
from app.nodes.manager import DeviceManager


# ── Protocol serialization ────────────────────────────────────────────────

def test_serialize_node_register():
    reg = NodeRegister(
        device_id="ios-device-1",
        device_type="ios",
        capabilities=["voice", "screen", "touch"],
        os_name="iOS",
        os_version="17.0",
        screen_width=390,
        screen_height=844,
    )
    raw = serialize_message(reg)
    data = json.loads(raw)
    assert data["type"] == MessageType.REGISTER.value
    assert data["device_id"] == "ios-device-1"
    assert data["device_type"] == "ios"
    assert data["capabilities"] == ["voice", "screen", "touch"]


def test_parse_node_register():
    raw = json.dumps({
        "type": "register",
        "device": {
            "device_id": "android-1",
            "device_type": "android",
            "capabilities": ["voice", "touch"],
            "os_name": "Android",
            "os_version": "14",
        }
    })
    reg = parse_node_register(raw)
    assert reg is not None
    assert reg.device_id == "android-1"
    assert reg.device_type == "android"


def test_parse_node_register_wrong_type():
    raw = json.dumps({"type": "heartbeat", "device_id": "x"})
    reg = parse_node_register(raw)
    assert reg is None


def test_parse_node_event():
    raw = json.dumps({
        "type": "event",
        "device_id": "dev-1",
        "event_type": "tap",
        "x": 100,
        "y": 200,
        "component_id": "btn-1",
    })
    event = parse_node_event(raw)
    assert event is not None
    assert event.event_type == "tap"
    assert event.component_id == "btn-1"


def test_parse_node_event_wrong_type():
    raw = json.dumps({"type": "heartbeat"})
    event = parse_node_event(raw)
    assert event is None


def test_serialize_canvas_push():
    push = CanvasPush(
        session_id="sess-1",
        components=[{"id": "c1", "component_type": "text"}],
        clear_first=True,
        full_state=True,
    )
    raw = serialize_message(push)
    data = json.loads(raw)
    assert data["type"] == MessageType.CANVAS_PUSH.value
    assert data["session_id"] == "sess-1"
    assert data["clear_first"] is True


def test_node_command_from_dict():
    data = {"command": "screenshot", "device_id": "dev-1", "params": {"quality": "high"}}
    cmd = NodeCommand.from_dict(data)
    assert cmd.command == "screenshot"
    assert cmd.device_id == "dev-1"


def test_capability_checker():
    checker = CapabilityChecker(["voice", "screen", "touch"])
    assert checker.has("voice") is True
    assert checker.has("camera") is False
    assert checker.all("voice", "screen") is True
    assert checker.all("voice", "camera") is False
    assert checker.any("voice", "camera") is True
    assert checker.any("camera", "gps") is False


# ── DeviceManager register/unregister ─────────────────────────────────────

@pytest.mark.asyncio
async def test_device_manager_register():
    dm = DeviceManager()
    reg = NodeRegister(
        device_id="dev-1",
        device_type="ios",
        capabilities=["voice", "screen"],
    )
    node = await dm.register(reg)
    assert node.device_id == "dev-1"
    assert node.connected is True
    assert dm.total_count == 1
    assert dm.connected_count == 1


@pytest.mark.asyncio
async def test_device_manager_unregister():
    dm = DeviceManager()
    reg = NodeRegister(device_id="dev-1", device_type="ios")
    await dm.register(reg)
    result = await dm.unregister("dev-1")
    assert result is True
    assert dm.total_count == 0

    result = await dm.unregister("nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_device_manager_register_update():
    dm = DeviceManager()
    reg1 = NodeRegister(device_id="dev-1", device_type="ios", capabilities=["voice"])
    await dm.register(reg1)

    reg2 = NodeRegister(device_id="dev-1", device_type="ios", capabilities=["voice", "touch"])
    node = await dm.register(reg2)
    assert "touch" in node.capabilities
    assert dm.total_count == 1  # Still one node


@pytest.mark.asyncio
async def test_device_manager_find_by_capability():
    dm = DeviceManager()
    await dm.register(NodeRegister(device_id="dev-1", device_type="ios", capabilities=["voice"]))
    await dm.register(NodeRegister(device_id="dev-2", device_type="android", capabilities=["screen"]))

    voice_nodes = dm.find_by_capability("voice")
    assert len(voice_nodes) == 1
    assert voice_nodes[0].device_id == "dev-1"


@pytest.mark.asyncio
async def test_device_manager_heartbeat():
    dm = DeviceManager()
    await dm.register(NodeRegister(device_id="dev-1", device_type="ios"))
    hb = NodeHeartbeat(device_id="dev-1", battery_level=0.8, network_type="wifi")
    await dm.update_heartbeat("dev-1", hb)

    node = dm.get_node("dev-1")
    assert node.battery_level == 0.8
    assert node.network_type == "wifi"


@pytest.mark.asyncio
async def test_device_manager_list_connected():
    dm = DeviceManager()
    await dm.register(NodeRegister(device_id="dev-1", device_type="ios"))
    await dm.register(NodeRegister(device_id="dev-2", device_type="android"))

    connected = dm.list_connected()
    assert len(connected) == 2

    await dm.unregister("dev-1")
    connected = dm.list_connected()
    assert len(connected) == 1


@pytest.mark.asyncio
async def test_device_manager_shutdown():
    dm = DeviceManager()
    await dm.register(NodeRegister(device_id="dev-1", device_type="ios"))
    await dm.register(NodeRegister(device_id="dev-2", device_type="android"))
    await dm.shutdown()
    assert dm.total_count == 0
