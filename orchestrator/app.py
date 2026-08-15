import json
import os
import re
import threading
import time

import anthropic
import requests
from fastapi import FastAPI, HTTPException
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel

from a2a.types import AgentCard, AgentSkill, AgentCapabilities, AgentInterface, TaskState, SendMessageRequest
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.helpers import get_data_parts, get_text_parts, new_task, new_data_artifact, new_data_message
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from a2a.client import create_client

REGISTRY = os.getenv("REGISTRY_URL", "http://registry:8000")
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
SELF_URL = os.getenv("SELF_URL", "http://orchestrator:8003/")

# Planning runs on the Claude API. The only configuration this needs is an
# API key in ANTHROPIC_API_KEY -- no gateway URLs, tenants or OAuth credentials.
LLM_MODEL = "claude-opus-5"
LLM_MAX_TOKENS = 4096

MAX_LLM_STEPS = int(os.getenv("MAX_LLM_STEPS", "10"))  # guards against a non-converging loop

# The orchestrator is itself a proper A2A agent -- it advertises a
# "math.solve" skill and is callable by any A2A client the same way
# math.add or math.multiply are, not just something that calls other agents.
AGENT_CARD = AgentCard(
    name="expression-orchestrator",
    description="Decomposes an arithmetic expression into individual operations and resolves each one through whichever capability agents are currently registered.",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=False),
    supported_interfaces=[
        AgentInterface(url=SELF_URL, protocol_binding="JSONRPC", protocol_version="1.0"),
    ],
    default_input_modes=["application/json", "text/plain"],
    default_output_modes=["application/json"],
    skills=[
        AgentSkill(
            id="math.solve",
            name="Solve arithmetic expression",
            description="Accepts an arithmetic expression using + and * with parentheses, and returns the fully resolved result along with the step-by-step trace of every agent call made to compute it.",
            tags=["math", "orchestration"],
        ),
    ],
)


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


# --- Calling other agents over real A2A, using the official client. This
# replaces every hand-rolled JSON-RPC request/response helper: create_client
# fetches the target's own Agent Card and speaks the wire protocol (method
# naming, protocol version header, Part/Task encoding) correctly on its own.

async def invoke_capability(capability: str, payload: dict):
    """Resolve a capability via the registry, then call whichever agent
    currently provides it using the real A2A client. The orchestrator never
    hard-codes an agent URL or hand-builds the wire format."""
    try:
        resolved = requests.get(f"{REGISTRY}/resolve/{capability}", timeout=5)
        resolved.raise_for_status()
        target = resolved.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach registry: {exc}")

    try:
        client = await create_client(target["url"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach agent at {target['url']}: {exc}")

    try:
        message = new_data_message(data={"a": payload["a"], "b": payload["b"]})
        request = SendMessageRequest(message=message)

        result = None
        task_id = None
        async for response in client.send_message(request):
            if response.HasField("task"):
                task_id = response.task.id
                for artifact in response.task.artifacts:
                    for value in get_data_parts(artifact.parts):
                        if isinstance(value, dict) and "result" in value:
                            result = value["result"]
            elif response.HasField("message"):
                raise HTTPException(status_code=502, detail="expected a Task response, got a Message")

        if result is None:
            raise HTTPException(status_code=502, detail="no result found in task artifacts")

        return {"result": result, "agent": target.get("agent"), "task_id": task_id}
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail=f"remote agent error: {exc}")
    finally:
        await client.close()


class ExpressionRequest(BaseModel):
    expression: str


app = FastAPI(title="A2A Orchestrator")


class SolveExecutor(AgentExecutor):
    """The orchestrator's own A2A entry point. Any A2A client can discover
    this agent via its Agent Card and call math.solve exactly like it would
    call math.add or math.multiply -- composite capabilities are still just
    capabilities. Decomposition is LLM-driven: the orchestrator plans each
    step against whatever capability agents are registered at that moment."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text_parts = get_text_parts(context.message.parts)
        expression = text_parts[0] if text_parts else None

        if not expression:
            data_parts = get_data_parts(context.message.parts)
            expr_data = next((d for d in data_parts if isinstance(d, dict) and "expression" in d), None)
            expression = expr_data["expression"] if expr_data else None

        if not expression:
            raise InvalidParamsError(
                message="expected a text part with the expression, or a data part with an 'expression' field"
            )

        result, steps = await _solve_expression_llm(expression)
        task = new_task(
            task_id=context.task_id,
            context_id=context.context_id,
            state=TaskState.TASK_STATE_COMPLETED,
            artifacts=[new_data_artifact(name="result", data={"result": result, "steps": steps})],
        )
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError()


handler = DefaultRequestHandler(
    agent_executor=SolveExecutor(),
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


# --- LLM-driven decomposition. Unaffected by the A2A/SDK migration except
# that it now awaits invoke_capability. Planning runs on Claude via the
# official Anthropic SDK, driven by a strict-JSON decision protocol.

_llm_client = None
_llm_lock = threading.Lock()


def get_llm_client():
    """Build (once) and return the Anthropic client. Cached so we don't
    rebuild an HTTP client on every planning turn. The API key is read from
    ANTHROPIC_API_KEY by the SDK itself."""
    global _llm_client
    if _llm_client is not None:
        return _llm_client

    with _llm_lock:
        if _llm_client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise HTTPException(
                    status_code=500,
                    detail="LLM is not configured; ANTHROPIC_API_KEY is not set",
                )
            _llm_client = anthropic.Anthropic()
        return _llm_client


def discover_capabilities():
    """Ask the registry which capabilities are available right now. This is
    what lets the LLM pick from real, currently-registered agents instead of
    a fixed list baked into the orchestrator."""
    try:
        resp = requests.get(f"{REGISTRY}/agents", timeout=5)
        resp.raise_for_status()
        agents = resp.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach registry: {exc}")

    return [
        {
            "capability": capability,
            "agent": info["agent"],
            "description": info["skill"].get("description", capability),
        }
        for capability, info in agents.items()
        if capability != "math.solve"  # don't offer the orchestrator its own composite skill
    ]

def _extract_json_object(text: str):
    """Best-effort extraction of a single JSON object from an LLM response.

    Models sometimes wrap JSON in prose or markdown fences even when asked not
    to, so we try a direct parse first, then fall back to scanning for the
    first balanced ``{...}`` block. Returns the parsed dict, or None if no
    valid JSON object can be recovered."""
    if not text:
        return None

    candidate = text.strip()

    # Strip a leading/trailing markdown code fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # Scan for the first balanced brace-delimited object.
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    snippet = text[start:i + 1]
                    try:
                        parsed = json.loads(snippet)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        start = None
    return None


def ask_llm_for_next_step(expression: str, steps: list):
    """Ask the LLM to decide the single next move: invoke one available
    capability with two operands, or declare the expression fully resolved.

    Capabilities are discovered from the registry fresh on every call, not
    passed in or cached -- so the LLM always plans against whichever agents
    are actually registered at that moment, the same way any other A2A
    client would.

    Returns (decision, args, valid_ids) so the caller can validate the LLM's
    choice against the same capability set this turn was planned against.
    """
    capabilities = discover_capabilities()
    if not capabilities:
        raise HTTPException(status_code=502, detail="No capabilities are currently registered")

    valid_ids = {c["capability"] for c in capabilities}

    capability_list = "\n".join(
        f"- {c['capability']} (provided by {c['agent']}): {c['description']}"
        for c in capabilities
    )

    history = "\n".join(
        f"{i + 1}. {s['capability']}({s['input']['a']}, {s['input']['b']}) = {s['result']}  [via {s['agent']}]"
        for i, s in enumerate(steps)
    ) or "(none yet)"

    system_prompt = (
        "You are the planning component of an agent-to-agent orchestrator. "
        "You are given an expression to solve and a list of capabilities "
        "currently offered by registered agents. On each turn, decide only "
        "the SINGLE next operation needed, respecting normal arithmetic "
        "precedence and parentheses. You may only use capabilities from the "
        "list provided -- never invent one. Once the expression is fully "
        "resolved, report the final numeric answer instead of another step.\n\n"
        "You do not have any built-in tools or functions. Communicate your "
        "decision ONLY as a single JSON object and nothing else -- no prose, "
        "no explanation, no markdown fences. Use exactly one of these two "
        "shapes:\n"
        '  {"action": "call_capability", "capability": "<one of the listed '
        'capability ids>", "a": <number>, "b": <number>}\n'
        '  {"action": "final_answer", "result": <number>}\n'
        "The 'capability' value must be copied verbatim from the available "
        "list. Numbers must be plain JSON numbers, not strings."
    )

    user_prompt = (
        f"Expression to solve: {expression}\n\n"
        f"Capabilities currently available from the registry:\n{capability_list}\n\n"
        f"Steps already taken:\n{history}\n\n"
        "What is the next single operation, or is the expression fully "
        "resolved? Respond with ONLY the JSON object."
    )

    # We drive the model with a strict-JSON protocol and parse the decision
    # ourselves rather than defining Claude tools: the capability list is
    # discovered at runtime, so a fixed tool schema would have to be rebuilt
    # every turn anyway, and this keeps the decision surface to one shape.
    client = get_llm_client()

    try:
        message = client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}")

    if message.stop_reason == "refusal":
        raise HTTPException(status_code=502, detail="LLM declined to answer the planning request")

    content = "".join(block.text for block in message.content if block.type == "text")

    decision_obj = _extract_json_object(content)
    if not decision_obj:
        raise HTTPException(
            status_code=502,
            detail=f"LLM did not return a usable JSON decision. Raw response: {content[:500]}",
        )

    action = decision_obj.get("action")
    if action == "call_capability":
        try:
            args = {
                "capability": decision_obj["capability"],
                "a": decision_obj["a"],
                "b": decision_obj["b"],
            }
        except KeyError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"LLM call_capability decision missing field {exc}: {decision_obj}",
            )
        return "call_capability", args, valid_ids

    if action == "final_answer":
        if "result" not in decision_obj:
            raise HTTPException(
                status_code=502,
                detail=f"LLM final_answer decision missing 'result': {decision_obj}",
            )
        return "final_answer", {"result": decision_obj["result"]}, valid_ids

    raise HTTPException(
        status_code=502,
        detail=f"LLM returned an unrecognized action: {decision_obj}",
    )


async def _solve_expression_llm(expression: str):
    """LLM-driven decomposition shared by the A2A `math.solve` entry point and
    the `/solveLLM` convenience endpoint. On each turn the orchestrator asks the
    registry what capabilities exist right now, hands that list plus the steps
    solved so far to the LLM, executes the single step the LLM chooses, and
    repeats until the LLM reports a final answer or MAX_LLM_STEPS is hit.

    Returns (result, steps)."""
    steps = []

    for _ in range(MAX_LLM_STEPS):
        decision, args, valid_ids = ask_llm_for_next_step(expression, steps)

        if decision == "final_answer":
            return args["result"], steps

        if decision == "call_capability":
            capability = args.get("capability")
            if capability not in valid_ids:
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM chose an unavailable capability: {capability}"
                )

            outcome = await invoke_capability(capability, {"a": args["a"], "b": args["b"]})
            steps.append({
                "capability": capability,
                "input": {"a": args["a"], "b": args["b"]},
                "agent": outcome.get("agent"),
                "result": outcome["result"],
            })
            continue

        raise HTTPException(status_code=502, detail=f"Unrecognized decision from LLM: {decision}")

    raise HTTPException(
        status_code=502,
        detail=f"LLM did not converge to a final answer within {MAX_LLM_STEPS} steps"
    )


@app.post("/solveLLM")
async def solve_llm(request: ExpressionRequest):
    """Convenience REST wrapper around the same LLM-driven decomposition behind
    the A2A `math.solve` entry point: the LLM decides each single step against
    the capabilities currently registered, instead of a fixed grammar.
    """
    result, steps = await _solve_expression_llm(request.expression)
    return {
        "expression": request.expression,
        "result": result,
        "steps": steps,
        "resolved_by": "llm",
    }
