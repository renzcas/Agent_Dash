from typing import Any, Dict, Optional
import httpx


class UnifiedEngineBridge:
    def __init__(
        self,
        base_url: str = "http://localhost:8001",
        state_endpoint: str = "/api/unified/state",
        step_endpoint: str = "/api/unified/step",
        timeout: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.state_endpoint = state_endpoint
        self.step_endpoint = step_endpoint
        self.timeout = timeout

    async def fetch_latest_snapshot(self) -> Dict[str, Any]:
        url = f"{self.base_url}{self.state_endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()

        subsystem_outputs = payload.get("subsystem_outputs") or {}
        return {
            "source": "unified_engine",
            "status": payload.get("status", "unknown"),
            "pipeline": payload.get("active_subsystems", []),
            "fields": payload.get("state_snapshot", {}),
            "subsystems": {
                "gravity": subsystem_outputs.get("gravity", {}),
                "cave": subsystem_outputs.get("cave", {}),
                "big_animal": subsystem_outputs.get("big_animal", {}),
            },
        }

    async def step_simulation(
        self,
        dt: float = 0.1,
        state_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{self.step_endpoint}"
        body: Dict[str, Any] = {"dt": dt}
        if state_override is not None:
            body["state"] = state_override

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()