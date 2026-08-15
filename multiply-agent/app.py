import os
import threading
import time

import requests
from fastapi import FastAPI, HTTPException
from google.protobuf.json_format import MessageToDict

from a2a.types import AgentCard, AgentSkill, AgentCapabilities, AgentInterface, TaskState, SendMessageRequest
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.helpers import get_data_parts, new_task, new_data_artifact, new_data_message
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from a2a.client import create_client

REGISTRY = os.getenv("REGISTRY_URL", "http://registry:8000")
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SELF_URL = os.getenv("SELF_URL", "http://multiply-agent:8002/")

AGENT_CARD = AgentCard(
    name="multiplication-agent",
    description="Multiplies two numbers and returns the product.",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=False),
    supported_interfaces=[
        AgentInterface(url=SELF_URL, protocol_binding="JSONRPC", protocol_version="1.0"),
    ],
    default_input_modes=["application/json"],
    default_output_modes=["application/json"],
    skills=[
        AgentSkill(
            id="math.multiply",
            name="Multiply numbers",
            description="Multiplies two numbers and returns their product.",
            tags=["math", "arithmetic"],
        ),
    ],
)


class MultiplyExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        operands = next(
            (d for d in get_data_parts(context.message.parts) if isinstance(d, dict) and "a" in d and "b" in d),
            None,
        )
        if operands is None:
            raise InvalidParamsError(message="expected a data part with numeric 'a' and 'b' fields")

        result = operands["a"] * operands["b"]
        task = new_task(
            task_id=context.task_id,
            context_id=context.context_id,
            state=TaskState.TASK_STATE_COMPLETED,
            artifacts=[new_data_artifact(name="result", data={"result": result})],
        )
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError()


def register() -> bool:
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
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        register()


app = FastAPI(title="Multiplication Agent")

handler = DefaultRequestHandler(
    agent_executor=MultiplyExecutor(),
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


# --- Agent-to-agent delegation example: this agent calling another agent
# directly (not through the orchestrator), using the real SDK client --
# create_client fetches the target's Agent Card itself and handles method
# naming, protocol versioning, and wire encoding transparently.

@app.post("/delegate-add")
async def delegate_add(request: dict):
    try:
        resolved = requests.get(f"{REGISTRY}/resolve/math.add", timeout=5)
        resolved.raise_for_status()
        provider = resolved.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach registry: {exc}")

    client = await create_client(provider["url"])
    try:
        message = new_data_message(data={"a": request["a"], "b": request["b"]})
        rpc_request = SendMessageRequest(message=message)

        result = None
        async for response in client.send_message(rpc_request):
            if response.HasField("task"):
                for artifact in response.task.artifacts:
                    for value in get_data_parts(artifact.parts):
                        if isinstance(value, dict) and "result" in value:
                            result = value["result"]
            elif response.HasField("message"):
                raise HTTPException(status_code=502, detail="expected a Task response, got a Message")

        if result is None:
            raise HTTPException(status_code=502, detail="no result found in task artifacts")

        return {"result": result, "delegated_to": provider["agent"]}
    finally:
        await client.close()
