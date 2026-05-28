from papolo import Agent

from . import db


def get_or_create_agent(conversation_uuid: str) -> Agent:
    saved = db.load_agent_state(conversation_uuid)
    if saved:
        return Agent(messages=saved)
    return Agent()


def persist_agent(conversation_uuid: str, agent: Agent) -> None:
    db.save_agent_state(conversation_uuid, agent.messages)
