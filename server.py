"""
The Pirate Game - multiplayer backend
Run with: uvicorn server:app --host 0.0.0.0 --port 8000
Then open http://localhost:8000 in a browser (4-25 players, each on their own device/tab).
"""

import asyncio
import random
import string
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# The fixed item set every player must place on their own 7x7 board
# ---------------------------------------------------------------------------

COLS = list("ABCDEFG")
ROWS = list(range(1, 8))
ALL_COORDS = [f"{c}{r}" for r in ROWS for c in COLS]  # 49 squares

def required_items_list():
    items = []
    items.append({"type": "rob"})
    items.append({"type": "kill"})
    items.append({"type": "present"})
    items.append({"type": "swap"})
    items.append({"type": "choose_next"})
    items.append({"type": "mirror"})
    items.append({"type": "bomb"})
    items.append({"type": "double"})
    items += [{"type": "shield"}] * 2
    items += [{"type": "bank"}] * 2
    items += [{"type": "cash", "value": 5000}] * 1
    items += [{"type": "cash", "value": 3000}] * 4
    items += [{"type": "cash", "value": 1000}] * 10
    items += [{"type": "cash", "value": 200}] * 20
    while len(items) < 49:
        items.append({"type": "blank"})
    return items

def item_key(cell):
    return (cell.get("type"), cell.get("value"))

def required_counter():
    return Counter(item_key(i) for i in required_items_list())

def validate_board(board):
    """Board must use every one of ALL_COORDS exactly once, with the exact required item multiset."""
    if not isinstance(board, dict):
        return False
    if set(board.keys()) != set(ALL_COORDS):
        return False
    try:
        submitted = Counter(item_key(v) for v in board.values())
    except (AttributeError, TypeError):
        return False
    return submitted == required_counter()

TICKED_TARGET_ABILITIES = ["rob", "kill", "present", "swap"]
DEFENDABLE_ABILITIES = ["rob", "kill", "swap"]  # bomb is NOT defendable
ABILITY_LABEL = {
    "rob": "Rob", "kill": "Kill", "present": "Present", "swap": "Swap Scores",
    "choose_next": "Choose Next Square", "mirror": "Mirror", "bomb": "Bomb",
    "double": "Double Your Score", "shield": "Shield", "bank": "Bank Your Score",
    "cash": "Cash", "blank": "Nothing",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Player:
    id: str
    name: str
    ws: Optional[WebSocket] = None
    board: dict = field(default_factory=dict)
    round_score: int = 0
    banked_score: int = 0
    shields: int = 0
    mirrors: int = 0
    alive: bool = True
    connected: bool = True

    def total(self):
        return self.round_score + self.banked_score

@dataclass
class Room:
    code: str
    host_id: str
    players: dict = field(default_factory=dict)  # id -> Player
    phase: str = "lobby"  # lobby -> setup -> playing -> finished
    started: bool = False
    finished: bool = False
    setup_ready: set = field(default_factory=set)
    called_squares: set = field(default_factory=set)
    pending_square_queue: list = field(default_factory=list)
    pending_futures: dict = field(default_factory=dict)  # key -> asyncio.Future
    round_num: int = 0
    log: list = field(default_factory=list)

ROOMS: dict[str, Room] = {}

def make_room_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in ROOMS:
            return code

# ---------------------------------------------------------------------------
# Messaging helpers
# ---------------------------------------------------------------------------

async def send(player: Player, message: dict):
    if player.ws is not None and player.connected:
        try:
            await player.ws.send_json(message)
        except Exception:
            player.connected = False

async def broadcast(room: Room, message: dict, exclude: set = frozenset()):
    for pid, p in room.players.items():
        if pid not in exclude:
            await send(p, message)

def alive_players(room: Room):
    return [p for p in room.players.values() if p.alive and p.connected]

def public_player_list(room: Room):
    return [
        {
            "id": p.id,
            "name": p.name,
            "round_score": p.round_score,
            "banked_score": p.banked_score,
            "total": p.total(),
            "shields": p.shields,
            "mirrors": p.mirrors,
            "connected": p.connected,
        }
        for p in room.players.values()
    ]

async def broadcast_scores(room: Room):
    await broadcast(room, {"type": "scores_update", "players": public_player_list(room)})

async def log_event(room: Room, text: str):
    room.log.append(text)
    await broadcast(room, {"type": "log", "text": text})

# ---------------------------------------------------------------------------
# Waiting for a specific player's response (target choice, square choice, buzz, defense)
# ---------------------------------------------------------------------------

def wait_key(pid: str, kind: str):
    return f"{pid}:{kind}"

async def wait_for_response(room: Room, pid: str, kind: str, timeout: float):
    key = wait_key(pid, kind)
    fut = asyncio.get_event_loop().create_future()
    room.pending_futures[key] = fut
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        return None
    finally:
        room.pending_futures.pop(key, None)

def resolve_response(room: Room, pid: str, kind: str, data):
    key = wait_key(pid, kind)
    fut = room.pending_futures.get(key)
    if fut and not fut.done():
        fut.set_result(data)

# ---------------------------------------------------------------------------
# Game flow
# ---------------------------------------------------------------------------

BUZZ_TIMEOUT = 8.0
TARGET_TIMEOUT = 15.0
SQUARE_TIMEOUT = 15.0
DEFEND_TIMEOUT = 12.0

async def resolve_order(room: Room, group_ids: list, ability: str):
    """Return group_ids ordered slowest-reaction-first, fastest-last."""
    if len(group_ids) <= 1:
        return group_ids

    prompt_time = asyncio.get_event_loop().time()
    for pid in group_ids:
        p = room.players[pid]
        await send(p, {"type": "buzzer_prompt", "ability": ability})

    async def get_reaction(pid):
        result = await wait_for_response(room, pid, "buzz", BUZZ_TIMEOUT)
        if result is None:
            return (pid, BUZZ_TIMEOUT + 1)  # no response = treated as slowest
        return (pid, asyncio.get_event_loop().time() - prompt_time)

    reactions = await asyncio.gather(*(get_reaction(pid) for pid in group_ids))
    # slowest (largest reaction time) first, fastest (smallest) last
    ordered = sorted(reactions, key=lambda r: r[1], reverse=True)
    order = [pid for pid, _ in ordered]
    names = [room.players[pid].name for pid in order]
    await broadcast(room, {"type": "buzzer_result", "ability": ability, "order": names})
    return order

async def get_target_choice(room: Room, pid: str, ability: str):
    actor = room.players[pid]
    options = [{"id": o.id, "name": o.name} for o in alive_players(room) if o.id != pid]
    if not options:
        return None
    await send(actor, {"type": "target_prompt", "ability": ability, "options": options})
    result = await wait_for_response(room, pid, "target", TARGET_TIMEOUT)
    if result and result.get("target_id") in room.players:
        return result["target_id"]
    return random.choice(options)["id"]

async def get_square_choice(room: Room, pid: str):
    actor = room.players[pid]
    available = [c for c in ALL_COORDS if c not in room.called_squares
                 and c not in room.pending_square_queue]
    if not available:
        return None
    await send(actor, {"type": "square_prompt", "available": available})
    result = await wait_for_response(room, pid, "square", SQUARE_TIMEOUT)
    if result and result.get("coord") in available:
        return result["coord"]
    return random.choice(available)

def next_random_square(room: Room):
    available = [c for c in ALL_COORDS if c not in room.called_squares]
    if not available:
        return None
    return random.choice(available)

async def maybe_defend(room: Room, target_id: str, actor_id: str, ability: str):
    """Ask the target whether to burn a saved Shield or Mirror against this incoming effect.
    Bomb is never defendable and never reaches this function."""
    target = room.players[target_id]
    options = []
    if target.shields > 0:
        options.append("shield")
    if target.mirrors > 0:
        options.append("mirror")
    if not options:
        return "none"
    options.append("none")

    actor_name = room.players[actor_id].name
    await send(target, {
        "type": "defend_prompt",
        "ability": ability,
        "actor": actor_name,
        "options": options,
    })
    result = await wait_for_response(room, target_id, "defend", DEFEND_TIMEOUT)
    choice = result.get("choice") if result else None
    return choice if choice in options else "none"

async def handle_ticked_ability(room: Room, pid: str, ability: str):
    actor = room.players[pid]
    target_id = await get_target_choice(room, pid, ability)
    if target_id is None:
        return
    target = room.players[target_id]
    label = ABILITY_LABEL[ability]

    if ability == "present":
        target.round_score += 1000
        await log_event(room, f"🎁 {actor.name} gave {target.name} 1000 points!")
        return

    defense = await maybe_defend(room, target_id, pid, ability)

    if defense == "shield":
        target.shields -= 1
        await log_event(room, f"🛡️ {target.name} used a Shield to block {actor.name}'s {label}!")
        return

    if defense == "mirror":
        target.mirrors -= 1
        if ability == "rob":
            stolen = actor.round_score
            actor.round_score = 0
            target.round_score += stolen
            await log_event(room, f"🪞 {target.name} mirrored the robbery - {actor.name} got robbed instead!")
        elif ability == "kill":
            actor.round_score = 0
            await log_event(room, f"🪞 {target.name} mirrored Kill back onto {actor.name}!")
        elif ability == "swap":
            await log_event(room, f"🪞 {target.name} mirrored the Swap - it fizzled, nothing happens!")
        return

    # no defense used (or none available) - apply normally
    if ability == "rob":
        stolen = target.round_score
        target.round_score = 0
        actor.round_score += stolen
        await log_event(room, f"🏴‍☠️ {actor.name} robbed {stolen} points from {target.name}!")
    elif ability == "kill":
        target.round_score = 0
        await log_event(room, f"☠️ {target.name} was killed - round score reset to 0!")
    elif ability == "swap":
        actor.round_score, target.round_score = target.round_score, actor.round_score
        await log_event(room, f"🔄 {actor.name} swapped scores with {target.name}!")

async def apply_self_effect(room: Room, pid: str, cell: dict):
    p = room.players[pid]
    t = cell["type"]
    if t == "cash":
        p.round_score += cell["value"]
    elif t == "double":
        p.round_score *= 2
        await log_event(room, f"✨ {p.name} doubled their round score!")
    elif t == "bank":
        p.banked_score += p.round_score
        await log_event(room, f"🏦 {p.name} banked {p.round_score} points!")
        p.round_score = 0
    elif t == "shield":
        p.shields += 1
        await log_event(room, f"🛡️ {p.name} picked up a Shield (saved for later).")
    elif t == "mirror":
        p.mirrors += 1
        await log_event(room, f"🪞 {p.name} picked up a Mirror (saved for later).")
    elif t == "bomb":
        # Bombs cannot be shielded or mirrored - always hits.
        p.round_score = 0
        await log_event(room, f"💣 {p.name} hit a Bomb - round score wiped out!")
    # blank -> nothing

async def run_round(room: Room):
    room.round_num += 1

    if room.pending_square_queue:
        coord = room.pending_square_queue.pop(0)
    else:
        coord = next_random_square(room)

    if coord is None:
        return False  # no squares left

    room.called_squares.add(coord)
    await broadcast(room, {"type": "square_called", "coord": coord, "round": room.round_num})

    reveals = {}
    for p in alive_players(room):
        cell = p.board[coord]
        reveals[p.id] = cell
        await send(p, {"type": "your_reveal", "coord": coord, "cell": cell})

    await asyncio.sleep(1.0)  # brief pause so players can see their reveal

    # ticked abilities requiring targets, resolved one type at a time
    for ability in TICKED_TARGET_ABILITIES:
        group = [pid for pid, c in reveals.items() if c["type"] == ability]
        if not group:
            continue
        order = await resolve_order(room, group, ability)
        for pid in order:
            if room.players[pid].alive:
                await handle_ticked_ability(room, pid, ability)
                await broadcast_scores(room)

    # choose-next-square, possibly multiple, resolved slowest-first
    group = [pid for pid, c in reveals.items() if c["type"] == "choose_next"]
    if group:
        order = await resolve_order(room, group, "choose_next")
        for pid in order:
            sq = await get_square_choice(room, pid)
            if sq:
                room.pending_square_queue.append(sq)
                await log_event(room, f"🧭 {room.players[pid].name} chose the next square: {sq}")

    # self-only effects
    for pid, cell in reveals.items():
        if cell["type"] in ("cash", "double", "bank", "shield", "mirror", "bomb", "blank"):
            await apply_self_effect(room, pid, cell)

    await broadcast_scores(room)
    return True

async def run_game(room: Room):
    room.phase = "playing"
    room.started = True
    await broadcast(room, {"type": "game_started"})
    while True:
        remaining = [c for c in ALL_COORDS if c not in room.called_squares]
        if not remaining and not room.pending_square_queue:
            break
        cont = await run_round(room)
        if not cont:
            break
        await asyncio.sleep(0.5)

    room.finished = True
    room.phase = "finished"
    ranked = sorted(room.players.values(), key=lambda p: p.total(), reverse=True)
    await broadcast(room, {
        "type": "game_over",
        "final_scores": [{"name": p.name, "total": p.total()} for p in ranked],
    })

# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    room_code = None
    player_id = None

    try:
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")

            if mtype == "create_room":
                name = data.get("name", "Host")[:20]
                room_code = make_room_code()
                player_id = str(uuid.uuid4())
                room = Room(code=room_code, host_id=player_id)
                room.players[player_id] = Player(id=player_id, name=name, ws=websocket)
                ROOMS[room_code] = room
                await websocket.send_json({"type": "room_created", "room": room_code, "player_id": player_id})
                await broadcast(room, {"type": "lobby_update", "players": public_player_list(room), "host_id": room.host_id})

            elif mtype == "join_room":
                code = data.get("room", "").upper()
                name = data.get("name", "Player")[:20]
                room = ROOMS.get(code)
                if not room:
                    await websocket.send_json({"type": "error", "message": "Room not found."})
                    continue
                if room.phase != "lobby":
                    await websocket.send_json({"type": "error", "message": "Game already started."})
                    continue
                if len(room.players) >= 25:
                    await websocket.send_json({"type": "error", "message": "Room is full (25 max)."})
                    continue
                room_code = code
                player_id = str(uuid.uuid4())
                room.players[player_id] = Player(id=player_id, name=name, ws=websocket)
                await websocket.send_json({"type": "joined", "room": room_code, "player_id": player_id})
                await broadcast(room, {"type": "lobby_update", "players": public_player_list(room), "host_id": room.host_id})

            elif mtype == "start_game":
                room = ROOMS.get(room_code)
                if not room or player_id != room.host_id or room.phase != "lobby":
                    continue
                if len(room.players) < 4:
                    await websocket.send_json({"type": "error", "message": "Need at least 4 players to start."})
                    continue
                room.phase = "setup"
                await broadcast(room, {"type": "setup_start", "items": required_items_list(), "coords": ALL_COORDS})

            elif mtype == "submit_board":
                room = ROOMS.get(room_code)
                if not room or room.phase != "setup":
                    continue
                board = data.get("board", {})
                if not validate_board(board):
                    await websocket.send_json({"type": "error", "message": "Invalid board - make sure all 49 squares are filled using exactly the given items."})
                    continue
                room.players[player_id].board = board
                room.setup_ready.add(player_id)
                await broadcast(room, {"type": "setup_progress", "ready": len(room.setup_ready), "total": len(room.players)})
                if len(room.setup_ready) == len(room.players):
                    asyncio.create_task(run_game(room))

            elif mtype == "buzz":
                room = ROOMS.get(room_code)
                if room:
                    resolve_response(room, player_id, "buzz", True)

            elif mtype == "choose_target":
                room = ROOMS.get(room_code)
                if room:
                    resolve_response(room, player_id, "target", {"target_id": data.get("target_id")})

            elif mtype == "choose_square":
                room = ROOMS.get(room_code)
                if room:
                    resolve_response(room, player_id, "square", {"coord": data.get("coord")})

            elif mtype == "choose_defense":
                room = ROOMS.get(room_code)
                if room:
                    resolve_response(room, player_id, "defend", {"choice": data.get("choice")})

    except WebSocketDisconnect:
        pass
    finally:
        if room_code and room_code in ROOMS:
            room = ROOMS[room_code]
            if player_id in room.players:
                room.players[player_id].connected = False
                await broadcast(room, {"type": "lobby_update", "players": public_player_list(room), "host_id": room.host_id})

# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")
