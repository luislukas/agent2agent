import os
import threading
import time

import requests
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

from a2a.types import AgentCard, AgentSkill, AgentCapabilities, AgentInterface, TaskState
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.helpers import get_data_parts, new_task, new_data_artifact
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError

REGISTRY = os.getenv("REGISTRY_URL", "http://registry:8000")
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SELF_URL = os.getenv("SELF_URL", "http://add-agent:8001/")

# The Agent Card is now a typed object, not hand-written JSON -- the SDK
# serializes it correctly (including the well-known route) instead of us
# maintaining a JSON file that has to stay in sync with the spec by hand.
AGENT_CARD = AgentCard(
    name="addition-agent",
    description="Adds two numbers and returns the sum.",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=False),
    supported_interfaces=[
        AgentInterface(url=SELF_URL, protocol_binding="JSONRPC", protocol_version="1.0"),
    ],
    default_input_modes=["application/json"],
    default_output_modes=["application/json"],
    skills=[
        AgentSkill(
            id="math.add",
            name="Add numbers",
            description="Adds two numbers and returns their sum.",
            tags=["math", "arithmetic"],
        ),
    ],
)


class AddExecutor(AgentExecutor):
    """The agent's actual logic -- and nothing else. The SDK handles JSON-RPC
    dispatch, protocol version negotiation, Agent Card serving, and
    Task/Artifact/Part wire encoding, so this class only needs to know how
    to add two numbers."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        operands = next(
            (d for d in get_data_parts(context.message.parts) if isinstance(d, dict) and "a" in d and "b" in d),
            None,
        )
        if operands is None:
            raise InvalidParamsError(message="expected a data part with numeric 'a' and 'b' fields")

        result = operands["a"] + operands["b"]
        task = new_task(
            task_id=context.task_id,
            context_id=context.context_id,
            state=TaskState.TASK_STATE_COMPLETED,
            artifacts=[new_data_artifact(name="result", data={"result": result})],
        )
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Every task here completes synchronously and immediately -- there's
        # never anything in flight to cancel.
        raise UnsupportedOperationError()


def register() -> bool:
    """Register this agent's card with our own capability registry (the SDK
    has no discovery-registry concept of its own -- this is still our own
    component). Retries with backoff to tolerate the registry container not
    being ready yet when this one starts, and later transient outages."""
    card_dict = MessageToDict(AGENT_CARD)
    delay = 1.0
    for _ in range(10):
        try:
            r = requests.post(f"{REGISTRY}/register", json=card_dict, timeout=2)
            r.raise_for_status()
            return True
        except requests.RequestException:
            time.sleep(delay)
            delay = min(delay * 2, 10)
    return False


def heartbeat_loop():
    """Re-register on an interval. The registry keeps no persistent state,
    so if it restarts it forgets every agent -- this loop re-populates it
    within one interval without needing a database or file volume."""
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        register()


app = FastAPI(title="Addition Agent")

handler = DefaultRequestHandler(
    agent_executor=AddExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=AGENT_CARD,
)

add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(AGENT_CARD),
    jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.on_event("startup")
def startup():
    if not register():
        raise RuntimeError("Could not register with the capability registry after retries")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
