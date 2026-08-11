from __future__ import annotations

from collections.abc import Callable

from app.agentloop import (
    AgentLoopConnection,
    AgentLoopGateway,
    Page,
    RecordNotFoundError,
    RecordQuery,
)
from app.domain.errors import NotFoundError, ServiceUnavailableError
from app.storage import LaunchIntentRepository


class RecordService:
    def __init__(
        self,
        launches: LaunchIntentRepository,
        gateway: AgentLoopGateway,
        connection_provider: Callable[[], AgentLoopConnection],
    ) -> None:
        self.launches = launches
        self.gateway = gateway
        self.connection_provider = connection_provider

    async def list(self, *, plan_id: str | None, offset: int, limit: int):
        record_ids = tuple(
            self.launches.record_ids_page(plan_id=plan_id, offset=offset, limit=limit)
        )
        total = self.launches.count(plan_id=plan_id, with_record_only=True)
        try:
            remote = await self.gateway.list_records(
                RecordQuery(
                    record_ids=record_ids,
                    plan_id=plan_id,
                    offset=0,
                    limit=limit,
                ),
                connection=self.connection_provider(),
            )
            by_id = {item.record_id: item for item in remote.items}
            if set(by_id) != set(record_ids):
                raise ServiceUnavailableError(
                    "RECORD_RECONCILIATION_FAILED",
                    "AgentLoop did not return the complete requested record page",
                )
            return Page(
                items=tuple(by_id[record_id] for record_id in record_ids),
                total=total,
                offset=offset,
                limit=limit,
            )
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(
                "AGENTLOOP_UNAVAILABLE", "Unable to read AgentLoop experiment records"
            ) from exc

    async def get(self, record_id: str):
        if self.launches.get_by_record_id(record_id) is None:
            raise NotFoundError(
                "RECORD_NOT_FOUND",
                "The experiment record is not part of this launcher's local ledger",
            )
        try:
            return await self.gateway.get_record(record_id, connection=self.connection_provider())
        except RecordNotFoundError as exc:
            raise NotFoundError("RECORD_NOT_FOUND", str(exc)) from exc
        except Exception as exc:
            raise ServiceUnavailableError(
                "AGENTLOOP_UNAVAILABLE", "Unable to read the AgentLoop experiment record"
            ) from exc

    def local_launch_for_record(self, record_id: str):
        return self.launches.get_by_record_id(record_id)
