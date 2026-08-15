from fastapi import FastAPI, HTTPException

app = FastAPI(title="A2A Capability Registry")

# In-memory only, by design: this is a curated discovery index (one of the
# discovery mechanisms the A2A spec explicitly allows, alongside well-known
# URIs and direct configuration), not the source of truth for any agent's
# capabilities -- each agent still serves its own real Agent Card. If the
# registry restarts, every agent re-registers automatically on its next
# heartbeat, so no persistent store is needed here.
agents = {}


@app.post("/register")
def register(agent_card: dict):
    """Accepts a real A2A Agent Card (v1.0 shape) and indexes it by skill id.
    Requires supportedInterfaces (where a client should send message/send
    requests) rather than the old flat 'url' field."""
    name = agent_card.get("name")
    interfaces = agent_card.get("supportedInterfaces")
    if not name or not interfaces:
        raise HTTPException(
            status_code=400,
            detail="agent_card must include 'name' and a non-empty 'supportedInterfaces'"
        )

    url = interfaces[0]["url"]

    for skill in agent_card.get("skills", []):
        capability = skill["id"]
        agents[capability] = {
            "agent": name,
            "url": url,
            "skill": skill,
        }

    return {"status": "registered", "agent": name}


@app.get("/resolve/{capability:path}")
def resolve(capability: str):
    target = agents.get(capability)
    if not target:
        raise HTTPException(status_code=404, detail="Capability not found")
    return target


@app.get("/agents")
def list_agents():
    return agents


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
