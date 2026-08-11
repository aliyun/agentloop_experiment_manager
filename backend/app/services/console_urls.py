from __future__ import annotations

from urllib.parse import quote, urlencode, urlsplit

from app.agentloop import AgentLoopGateway
from app.storage import ProfileRepository


class ConsoleUrlResolver:
    """Resolve a safe AgentLoop deep link without coupling the UI to console routes."""

    def __init__(self, profiles: ProfileRepository, gateway: AgentLoopGateway) -> None:
        self.profiles = profiles
        self.gateway = gateway

    def record_url(self, record_id: str, plan_id: str | None = None) -> str | None:
        official = self.gateway.record_console_url(record_id)
        if official:
            return official

        profile = self.profiles.get_configured()
        if profile is None or not self._valid_base(profile.console_base_url):
            return None
        query = {"recordId": record_id}
        if plan_id:
            query["planId"] = plan_id
        base = profile.console_base_url.rstrip("/")
        return (
            f"{base}/agentloop/region/{quote(profile.region_id, safe='')}"
            f"/agentspace/{quote(profile.agent_space, safe='')}/app/playground"
            f"?{urlencode(query)}"
        )

    def dashboard_url(self, dashboard_name: str) -> str | None:
        """Return the AgentLoop/CMSNext route for one managed Plan dashboard."""

        profile = self.profiles.get_configured()
        if profile is None or not self._valid_base(profile.console_base_url):
            return None
        base = profile.console_base_url.rstrip("/")
        return (
            f"{base}/agentloop/region/{quote(profile.region_id, safe='')}"
            f"/agentspace/{quote(profile.agent_space, safe='')}/app/dashboard/detail/"
            f"{quote(dashboard_name, safe='')}"
        )

    @staticmethod
    def _valid_base(value: str | None) -> bool:
        if not value:
            return False
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
