from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.agentloop import AgentLoopConnection, AgentLoopGateway
from app.domain.errors import LauncherError, NotFoundError, ServiceUnavailableError
from app.secrets import SecretStore
from app.storage import ProfileRepository, ValidationStatus


class ProfileService:
    def __init__(
        self,
        repository: ProfileRepository,
        secret_store: SecretStore,
        gateway: AgentLoopGateway,
    ) -> None:
        self.repository = repository
        self.secret_store = secret_store
        self.gateway = gateway

    def get(self):
        profile = self.repository.get_configured()
        if profile is None:
            raise NotFoundError(
                "CONNECTION_NOT_CONFIGURED", "AgentLoop connection is not configured"
            )
        return profile

    def configure(
        self,
        *,
        display_name: str,
        region_id: str,
        agent_space: str,
        access_key_id: str | None,
        access_key_secret: str | None,
        api_endpoint: str | None = None,
        console_base_url: str | None = None,
        default_timezone: str | None = None,
        max_concurrency: int | None = None,
    ):
        existing = self.repository.get_configured()
        if (access_key_id is None) != (access_key_secret is None):
            raise LauncherError(
                "CREDENTIALS_INCOMPLETE",
                "AccessKey ID and AccessKey Secret must be provided together",
            )
        if existing is None and access_key_id is None:
            raise LauncherError(
                "CREDENTIALS_REQUIRED",
                "AccessKey ID and AccessKey Secret are required for the first configuration",
            )
        profile_id = existing.id if existing else str(uuid4())
        secret_ref = existing.secret_ref if existing else f"profile/{profile_id}/credentials"
        resolved_timezone = (
            default_timezone
            if default_timezone is not None
            else (existing.default_timezone if existing else "UTC")
        )
        resolved_max_concurrency = (
            max_concurrency
            if max_concurrency is not None
            else (existing.max_concurrency if existing else 1)
        )
        old_secret = (
            self.secret_store.get(secret_ref) if self.secret_store.exists(secret_ref) else None
        )
        secret_changed = access_key_id is not None and access_key_secret is not None
        if secret_changed:
            new_secret = json.dumps(
                {"accessKeyId": access_key_id, "accessKeySecret": access_key_secret},
                separators=(",", ":"),
            )
            self.secret_store.put(secret_ref, new_secret)
        hint = (
            f"****{access_key_id[-4:]}"
            if access_key_id is not None
            else (existing.access_key_id_hint if existing else None)
        )
        try:
            if existing is None:
                return self.repository.create(
                    profile_id=profile_id,
                    display_name=display_name,
                    region_id=region_id,
                    agent_space=agent_space,
                    api_endpoint=api_endpoint,
                    console_base_url=console_base_url,
                    access_key_id_hint=hint,
                    secret_ref=secret_ref,
                    default_timezone=resolved_timezone,
                    max_concurrency=resolved_max_concurrency,
                )
            return self.repository.update(
                existing.id,
                display_name=display_name,
                region_id=region_id,
                agent_space=agent_space,
                api_endpoint=api_endpoint,
                console_base_url=console_base_url,
                access_key_id_hint=hint,
                secret_ref=secret_ref,
                default_timezone=resolved_timezone,
                max_concurrency=resolved_max_concurrency,
                validation_status=ValidationStatus.UNVALIDATED.value,
                validated_at=None,
                last_error_code=None,
            )
        except BaseException:
            if secret_changed:
                if old_secret is None:
                    self.secret_store.delete(secret_ref)
                else:
                    self.secret_store.put(secret_ref, old_secret)
            raise

    async def test_connection(self):
        profile = self.get()
        try:
            connection = self.connection(require_valid=False)
        except ServiceUnavailableError as exc:
            self.repository.update(
                profile.id,
                validation_status=ValidationStatus.INVALID.value,
                validated_at=datetime.now(UTC),
                last_error_code=exc.code,
            )
            raise
        try:
            result = await self.gateway.test_connection(connection)
        except Exception as exc:
            self.repository.update(
                profile.id,
                validation_status=ValidationStatus.INVALID.value,
                validated_at=datetime.now(UTC),
                last_error_code="AGENTLOOP_UNAVAILABLE",
            )
            raise ServiceUnavailableError(
                "AGENTLOOP_UNAVAILABLE", "Unable to validate the AgentLoop connection"
            ) from exc

        status = ValidationStatus.VALID if result.ok else ValidationStatus.INVALID
        self.repository.update(
            profile.id,
            validation_status=status.value,
            validated_at=datetime.now(UTC),
            last_error_code=None if result.ok else "AGENTLOOP_AUTH_FAILED",
        )
        return result

    def connection(self, *, require_valid: bool = True) -> AgentLoopConnection:
        profile = self.repository.get_configured()
        if profile is None:
            raise ServiceUnavailableError(
                "CONNECTION_NOT_CONFIGURED",
                "AgentLoop connection is not configured",
                retryable=False,
            )
        if require_valid and profile.validation_status != ValidationStatus.VALID.value:
            raise ServiceUnavailableError(
                "CONNECTION_NOT_VALIDATED",
                "Validate the saved AgentLoop connection before accessing AgentLoop",
                retryable=False,
            )
        try:
            payload = json.loads(self.secret_store.get(profile.secret_ref))
            access_key_id = payload["accessKeyId"]
            access_key_secret = payload["accessKeySecret"]
            if not isinstance(access_key_id, str) or not isinstance(access_key_secret, str):
                raise ValueError("credential fields must be strings")
            return AgentLoopConnection(
                region_id=profile.region_id,
                agent_space=profile.agent_space,
                api_endpoint=profile.api_endpoint,
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )
        except Exception as exc:
            raise ServiceUnavailableError(
                "CREDENTIALS_UNAVAILABLE",
                "Saved AgentLoop credentials are unavailable or invalid",
                retryable=False,
            ) from exc
