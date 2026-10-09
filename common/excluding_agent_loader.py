"""
Hides specific agents from ADK's own auto-discovery (get_fast_api_app's
default AgentLoader), so they never get the shared, Postgres-backed
DatabaseSessionService that call wires up for every OTHER agent in
agents_dir — see main.py's EXCLUDED_FROM_SHARED_SESSION_SERVICE and
routers/live.py for the agent(s) this applies to and why.

This does NOT remove the agent from the codebase or stop it being
importable - member_goal_setter/agent.py's root_agent is still imported
directly by routers/live.py's own InMemoryRunner. It only removes it from
the list get_fast_api_app uses to register its automatic
/apps/{agent_name}/... endpoints and attach the shared session service,
so routers/live.py's own endpoint is the only way to reach it in
production.
"""

from google.adk.cli.utils.agent_loader import AgentLoader


class ExcludingAgentLoader(AgentLoader):
    def __init__(self, agents_dir: str, excluded_agent_names: set[str]):
        super().__init__(agents_dir)
        self._excluded_agent_names = excluded_agent_names

    def list_agents(self) -> list[str]:
        return [name for name in super().list_agents() if name not in self._excluded_agent_names]
