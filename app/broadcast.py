"""One bounded outbox and one writer per socket; ingestion never awaits a socket."""
import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Subscriber:
    queue: asyncio.Queue
    sessions: set[str] | None
    task: asyncio.Task | None = None
    statuses: dict[str, dict] = field(default_factory=dict)
    closing: bool = False


class Broadcaster:
    def __init__(self, queue_size=128, timeout=5):
        self.clients: dict[Any, Subscriber] = {}
        self.queue_size = queue_size
        self.timeout = timeout
        self.dropped_clients = 0

    async def add(self, ws, sessions=None):
        await self.remove(ws)
        sub = Subscriber(asyncio.Queue(self.queue_size), sessions)
        self.clients[ws] = sub
        sub.task = asyncio.create_task(self._writer(ws, sub))
        # Let the writer enter its try/finally before it can be cancelled on overflow.
        await asyncio.sleep(0)

    async def _writer(self, ws, sub):
        try:
            while True:
                payload = await sub.queue.get()
                if isinstance(payload, tuple):
                    payload = sub.statuses.pop(payload[0])
                sender = ws.send_text if isinstance(payload, str) else ws.send_json
                await asyncio.wait_for(sender(payload), self.timeout)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            if self.clients.get(ws) is sub:
                self.clients.pop(ws, None)
            try:
                await asyncio.wait_for(ws.close(code=1013), self.timeout)
            except (Exception, asyncio.CancelledError):
                pass

    async def remove(self, ws):
        sub = self.clients.pop(ws, None)
        if sub and sub.task:
            sub.task.cancel()
            await asyncio.gather(sub.task, return_exceptions=True)

    def _enqueue(self, ws, sub, payload):
        if sub.closing or sub.task.done():
            return
        try:
            if isinstance(payload, dict) and payload.get("kind") == "status":
                sid = payload["session"]["session_id"]
                if sid in sub.statuses:
                    sub.statuses[sid]["session"].update(payload["session"])
                    return
                sub.queue.put_nowait((sid,))
                sub.statuses[sid] = {**payload, "session": dict(payload["session"])}
            else:
                sub.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped_clients += 1
            sub.closing = True
            # Keep the subscription until its writer exits so shutdown can await it.
            sub.task.cancel()

    async def send_to(self, ws, payload):
        sub = self.clients.get(ws)
        if sub:
            self._enqueue(ws, sub, payload)

    async def send(self, payload):
        sid = (payload.get("event") or payload.get("session") or {}).get("session_id")
        for ws, sub in tuple(self.clients.items()):
            if sub.sessions is None or sid in sub.sessions:
                self._enqueue(ws, sub, payload)

    async def close(self):
        await asyncio.gather(*(self.remove(ws) for ws in tuple(self.clients)))
