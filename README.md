# The Pirate Game — Multiplayer

A live, real-time multiplayer web version of your pirate board game. 4–25 players, one host, one shared game running on a Python backend with WebSockets.

## Running it

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` in a browser. Everyone playing needs to open that same address:
- On the same WiFi network: use your computer's local IP instead of `localhost` (e.g. `http://192.168.1.23:8000`) so other devices can reach it.
- Over the internet: deploy it (Fly.io, Railway, a VPS) or use a tunnel like `ngrok http 8000` for a quick public link.

One player taps **Host a Game** to get a 4-letter room code. Everyone else taps **Join a Game** and enters that code + their name. The host starts once 4+ players have joined.

## How it plays

At the start of the game, every player privately builds their own 7×7 board by tapping each required item onto a square — exactly like filling in the paper sheet by hand. Nobody sees anyone else's board. Once everyone submits, the game begins: each round a coordinate is called (e.g. "D4"), and everyone checks what they placed at that square on their own board. Effects apply automatically; the log shows what happened to whom. Your own board stays visible to you the whole game (you placed it, so you know it) — the current call is just highlighted.

### Rules I assumed (since the sheet didn't spell everything out)

- **Two scores per player**: a *round score* (at risk) and a *banked score* (safe forever). Cash squares add to your round score.
- **Bank** — moves your entire round score into your banked score, resetting round score to 0.
- **Bomb** — always wipes your round score to 0. This cannot be blocked or reflected, per your note.
- **Double Your Score** — doubles your current round score.
- **Rob** — you choose a target; if they don't defend, you steal their *entire* current round score.
- **Kill** — you choose a target; if they don't defend, their round score is wiped to 0.
- **Present** — you choose a target; they receive +1000 points (no defense possible — it's not "the bad").
- **Swap** — you choose a target; if they don't defend, you swap round scores with them. If they mirror it, the swap just fizzles (a symmetric effect has nothing to "reflect" back).
- **Shield & Mirror are saved resources**, not automatic. Landing on one just adds it to your stash — nothing happens immediately. When someone later Robs, Kills, or Swaps you, you get prompted in the moment to choose: burn a Shield (blocks it entirely), burn a Mirror (bounces it back onto them instead), or take it. You can hoard multiple and use them whenever you want.
- **Choose Next Square** — instead of the server picking randomly, you pick the next coordinate called.
- The game ends once all 49 squares have been called. Winner = highest (banked + round) total.

### The mechanics you specifically asked for

- **Random squares, unless "Choose Next Square" is revealed** — implemented. When it's revealed, that player is prompted to pick the next square instead of the server rolling randomly.
- **Tie-break for ticked abilities (Rob/Kill/Present/Swap/Choose Next Square)**: if more than one player reveals the same ability in the same round, everyone in that group gets a **BUZZ** button. Whoever taps fastest resolves **last**; the slowest player(s) resolve first. Order is announced in the log.
- **Multiple "Choose Next Square"**: same buzz-in, but the *slowest* buzzer picks the very next square, the next-slowest picks the square after that, and so on — queued in that order.

## Files

- `server.py` — FastAPI backend: room management, board generation, round logic, all game rules.
- `static/index.html` — the client (single page, plain JS, no build step).
- `requirements.txt` — `fastapi` + `uvicorn`.

## Things you might want to tweak next

- Rob/Kill/Swap amounts or targeting rules, if you want them different from my assumptions above.
- A visible countdown timer on the buzzer/target/square/defend prompts (currently silent server-side timeouts of 8–15s; if nobody responds in time, defense defaults to "take it" and targeting defaults to random).
- Persisting rooms to a database instead of in-memory (needed if you want the server to survive restarts).
- A "spectator" or reconnect flow for players who refresh mid-game (they'd lose their in-progress board placement).
- Letting a player rearrange their board mid-setup is supported (tap a filled square to take the item back); there's currently no "randomize for me" shortcut if someone wants to skip manual placement.
