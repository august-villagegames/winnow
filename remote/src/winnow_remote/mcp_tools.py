"""Thin official-MCP adapters around the transport-independent coordinator."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import Annotations, CallToolResult, ResourceLink, TextContent, ToolAnnotations

from .contracts import CreateWinnowSessionRequest, ContractError, InvalidModeError, PublishNextRoundRequest, WaitForContinueRequest
from .coordinator import AuthenticationError, CircuitOpen, Coordinator, CoordinatorError, CreationHandle, QuotaExceeded, StateConflict
from .herenow import HereNowError, HereNowPublisher, PendingVersion, expected_live_markers
from .mcp_contract import (
    ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI,
    SEED_SCHEMA_RESOURCE_URI,
    canonical_seed_schema_text,
    round_one_authoring_guide,
    seed_contract_payload,
)
from .settings import RateLimitError, RateLimiter, WaitNotifier, current_mcp_provenance


ROLLING_VERSION = 1


_SUCCESSOR_SEED_REQUIREMENTS = {
    "nextSeed": "A complete Winnow successor seed, not only a new-round object.",
    "rootFields": ["protocol", "schemaVersion", "runtimeVersion", "session", "profileExclusions", "profilePatterns", "history", "round"],
    "rootValues": {"protocol": "winnow.portable-session", "schemaVersion": 4, "runtimeVersion": "4.0.0"},
    "copyFromContinuation": {
        "session": "Copy continuation.session exactly.",
        "profileExclusions": "Copy continuation.profileExclusions exactly.",
        "profilePatterns": "Copy continuation.profilePatterns exactly.",
        "history": "Copy continuation.completedRounds exactly.",
    },
    "excludeFromNextSeed": ["parent", "parentProfilePatterns", "parentProfileExclusions", "completedRounds", "nextRoundNumber"],
    "round": {
        "requiredFields": ["number", "generatedAt", "factors", "sources", "options"],
        "number": "Set to continuation.nextRoundNumber.",
        "primaryFactor": "Keep the session primary factor and its definition unchanged from the completed current round.",
        "research": "Research 4–10 new options; only non-primary factors may change.",
    },
    "publishArguments": "Use exactly the supplied publishArguments. Do not invent or request different eventId or publishFence values.",
}


def publish_handoff_payload(event: Mapping[str, Any], session_handle: str) -> dict[str, Any]:
    """Build the fixed assistant-readable successor handoff without secrets beyond its agent context."""

    continuation = event["continuation"]
    parent = continuation["parent"]
    arguments = {
        "sessionHandle": session_handle,
        "eventId": event["eventId"],
        "publishFence": event["publishFence"],
        "parentSeedHash": parent["seedHash"],
    }
    return {
        "nextAction": "Research one valid complete successor seed, then call publish_next_round with the supplied publishArguments and nextSeed.",
        "publishArguments": arguments,
        "nextSeedRequirements": _SUCCESSOR_SEED_REQUIREMENTS,
        "continuation": continuation,
        "researchDeadline": event["researchDeadline"],
    }


def _load_portable_core() -> Any:
    # Keep this import private to the adapter; the coordinator owns all seed and
    # successor validation, while the compiler is only used to emit a page that
    # the already-validated coordinator state authorizes.
    from .coordinator import _load_portable_core as load

    return load()


def _safe_tool_error(error: BaseException) -> dict[str, str]:
    """Return only fixed contract guidance; never echo exception text."""

    if isinstance(error, ContractError):
        if isinstance(error, InvalidModeError):
            return {
                "status": "rejected",
                "reason": "invalid_mode",
                "message": "mode must be the literal string 'rolling'.",
            }
        return {
            "status": "rejected",
            "reason": "invalid_request",
            "message": "The tool arguments do not match the required Winnow contract.",
        }
    if isinstance(error, CoordinatorError):
        return {
            "status": "rejected",
            "reason": "invalid_seed",
            "message": "The seed must be a valid Winnow v4 round-one seed.",
        }
    return {"status": "rejected"}


@dataclass(frozen=True)
class McpToolConfig:
    coordinator_origin: str
    max_wait_seconds: int = 300
    wait_poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.coordinator_origin, str) or not self.coordinator_origin.startswith("https://"):
            raise ValueError("coordinator origin must be an HTTPS origin")
        if self.max_wait_seconds < 1 or self.wait_poll_seconds <= 0:
            raise ValueError("MCP wait configuration is invalid")


class McpToolService:
    """Runs provider work off the event loop and keeps all transitions in Coordinator."""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        publisher_factory: Callable[[Callable[[Mapping[str, Any], str | None], bytes]], HereNowPublisher],
        rate_limiter: RateLimiter,
        notifier: WaitNotifier,
        config: McpToolConfig,
        core: Any | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._publisher_factory = publisher_factory
        self._rate_limiter = rate_limiter
        self._notifier = notifier
        self._config = config
        self._core = core or _load_portable_core()

    @property
    def max_wait_seconds(self) -> int:
        """Expose the configured bounded wait for an MCP handoff payload."""

        return self._config.max_wait_seconds

    async def create(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Create and publish a complete initial rolling page."""

        try:
            request = CreateWinnowSessionRequest.parse(dict(arguments))
            provenance = current_mcp_provenance()
            self._rate_limiter.mcp_create(provenance)
            handle = self._coordinator.begin_creation(
                request.seed,
                network_prefix=provenance.network_prefix,
                client_family=provenance.client_family,
            )
            return await asyncio.to_thread(self._create_blocking, handle, request.seed)
        except asyncio.CancelledError:
            # ``creating`` is TTL-bound; cancellation must not leak a partial
            # publication receipt or turn a transport cancellation into output.
            raise
        except (AuthenticationError, CircuitOpen, HereNowError, QuotaExceeded, RateLimitError):
            return _safe_tool_error(Exception())
        except (ContractError, CoordinatorError) as error:
            return _safe_tool_error(error)

    def _create_blocking(self, handle: CreationHandle, seed: Mapping[str, Any]) -> dict[str, Any]:
        seed_hash = self._core.seed_hash(dict(seed))

        def build_html(value: Mapping[str, Any], expires_at: str | None) -> bytes:
            return self._core.build_rolling_html(
                dict(value),
                expires_at=expires_at,
                coordinator_origin=self._config.coordinator_origin,
                browser_capability=handle.browser_capability,
                published_revision=1,
            )

        publisher = self._publisher_factory(build_html)

        def persist_claim(created: Any) -> None:
            self._coordinator.persist_creation_publication(
                handle,
                site_url=created.site_url,
                slug=created.slug,
                original_expires_at=created.expires_at,
                claim_token=created.claim_token,
            )

        def markers(expires_at: str) -> Mapping[str, tuple[str, str]]:
            return expected_live_markers(
                session_id=str(seed["session"]["id"]),
                seed_hash=seed_hash,
                runtime_version=str(seed["runtimeVersion"]),
                expires_at=expires_at,
                rolling_version=ROLLING_VERSION,
                published_revision=1,
            )

        publisher.create(seed, persist_claim=persist_claim, published_revision=1, expected_markers=markers)
        return self._coordinator.activate_creation(handle)

    async def wait(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Register one renewable bounded wait, then await durable event state."""

        try:
            request = WaitForContinueRequest.parse(dict(arguments), maximum_wait_seconds=self._config.max_wait_seconds)
            session_handle = arguments.get("sessionHandle")
            if not isinstance(session_handle, str):
                raise ContractError("wait request.sessionHandle: expected bounded non-empty text")
            initial = self._coordinator.wait_for_continue(session_handle, request)
            if initial.get("status") != "still_waiting":
                return initial
            notification_key = self._coordinator.agent_wait_notification_key(session_handle)
            if notification_key is None:
                return initial
            deadline = time.monotonic() + request.max_wait_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return initial
                # A Redis wakeup improves latency.  State is always polled
                # afterwards, so dropped publications, cancellations, and
                # cross-worker misses cannot lose a queued event.
                await self._notifier.wait(notification_key, min(self._config.wait_poll_seconds, remaining))
                result = self._coordinator.poll_wait_for_continue(session_handle, request)
                if result is not None:
                    return result
        except asyncio.CancelledError:
            # The event and its publish fence remain in coordinator state.  A
            # reconnecting agent receives the exact same event safely.
            raise
        except ContractError as error:
            return _safe_tool_error(error)
        except (AuthenticationError, CoordinatorError, StateConflict):
            return _safe_tool_error(Exception())

    async def publish(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the one successor owned by the accepted event/fence."""

        try:
            request = PublishNextRoundRequest.parse(dict(arguments))
            session_handle = arguments.get("sessionHandle")
            if not isinstance(session_handle, str):
                raise ContractError("publish request.sessionHandle: expected bounded non-empty text")
            self._rate_limiter.mcp_publish(session_handle)
            recovered = await asyncio.to_thread(self._recover_pending_publish, session_handle, request)
            if recovered is not None:
                return recovered
            self._coordinator.begin_publish(session_handle, request)
            target = self._coordinator.publication_target(session_handle, request)
            return await asyncio.to_thread(self._publish_blocking, session_handle, request, target)
        except asyncio.CancelledError:
            # Publication ownership and pending metadata are durable.  A later
            # retry reconciles instead of allowing a second publisher to win.
            raise
        except ContractError as error:
            return _safe_tool_error(error)
        except (AuthenticationError, CoordinatorError, HereNowError, RateLimitError, StateConflict):
            return _safe_tool_error(Exception())

    def _recover_pending_publish(self, session_handle: str, request: PublishNextRoundRequest) -> dict[str, Any] | None:
        """Reconcile one durable publish before a replacement worker retries it.

        A process can die after the provider accepts an upload/finalize request
        but before the coordinator commits the revision.  Marker verification
        decides whether that exact version is already public.  Only a verified
        version commits; otherwise the same event/fence is released for a new
        provider update after a bounded failed verification.
        """

        metadata = self._coordinator.pending_publication_metadata(session_handle, request)
        if metadata is None:
            return None
        target = self._coordinator.publication_target(session_handle, request)
        pending = self._pending_reconciliation_value(target, metadata)
        verified = False
        if pending is not None:
            # ``reconcile_pending`` performs only bounded public marker reads.
            # It never uploads or finalizes a provider version.
            publisher = self._publisher_factory(lambda _seed, _expires_at: b"")
            verified = publisher.reconcile_pending(pending)
        if verified:
            return self._coordinator.commit_publish(session_handle, request)
        self._coordinator.retry_pending_publication(session_handle, request)
        return None

    @staticmethod
    def _pending_reconciliation_value(target: Any, metadata: Mapping[str, Any]) -> PendingVersion | None:
        """Rebuild the marker-only portion of a persisted provider version."""

        version_id = metadata.get("versionId")
        revision = metadata.get("publishedRevision")
        raw_markers = metadata.get("expectedMarkers")
        if not isinstance(version_id, str) or not version_id or not isinstance(revision, int) or revision != target.published_revision or not isinstance(raw_markers, Mapping):
            return None
        markers: dict[str, tuple[str, str]] = {}
        for label, value in raw_markers.items():
            if not isinstance(label, str) or not isinstance(value, (list, tuple)) or len(value) != 2 or not all(isinstance(item, str) for item in value):
                return None
            markers[label] = (value[0], value[1])
        if not markers:
            return None
        return PendingVersion(
            slug=target.slug,
            site_url=target.site_url,
            original_expires_at=target.original_expires_at,
            version_id=version_id,
            published_revision=revision,
            expected_markers=markers,
            # These are intentionally unused by marker-only reconciliation;
            # signed provider upload URLs are never retained across restarts.
            upload_url="",
            upload_headers={},
            finalize_url="",
        )

    def _publish_blocking(self, session_handle: str, request: PublishNextRoundRequest, target: Any) -> dict[str, Any]:
        next_seed = request.next_seed
        next_hash = self._core.seed_hash(dict(next_seed))

        def build_html(value: Mapping[str, Any], expires_at: str | None) -> bytes:
            return self._core.build_rolling_html(
                dict(value),
                expires_at=expires_at,
                coordinator_origin=self._config.coordinator_origin,
                browser_capability=target.browser_capability,
                published_revision=target.published_revision,
            )

        publisher = self._publisher_factory(build_html)

        def persist_pending(pending: Any) -> None:
            # The signed upload URL is intentionally not needed for safe
            # restart reconciliation.  Persist only the bounded provider
            # version identity and deterministic marker expectation.
            value = {
                "slug": pending.slug,
                "versionId": pending.version_id,
                "publishedRevision": pending.published_revision,
                "expectedMarkers": dict(pending.expected_markers),
            }
            self._coordinator.persist_pending_publication(
                session_handle,
                publish_fence=request.publish_fence,
                pending_publication=value,
            )

        markers = expected_live_markers(
            session_id=str(next_seed["session"]["id"]),
            seed_hash=next_hash,
            runtime_version=str(next_seed["runtimeVersion"]),
            expires_at=target.original_expires_at,
            rolling_version=ROLLING_VERSION,
            published_revision=target.published_revision,
        )
        try:
            publisher.update(
                next_seed,
                slug=target.slug,
                claim_token=target.claim_token,
                site_url=target.site_url,
                original_expires_at=target.original_expires_at,
                persist_pending=persist_pending,
                published_revision=target.published_revision,
                expected_markers=markers,
            )
        except HereNowError:
            # The coordinator owns the terminal sanitization boundary; the
            # provider exception itself is deliberately not emitted.
            self._coordinator.fail_session(session_handle, category="publication_failure")
            raise
        return self._coordinator.commit_publish(session_handle, request)


def register_mcp_tools(server: MCPServer, service: McpToolService) -> None:
    """Register fixed v4 discovery material and the public rolling tools."""

    @server.resource(
        SEED_SCHEMA_RESOURCE_URI,
        name="Winnow v4 seed schema",
        description="Exact canonical JSON Schema for a Winnow v4 round-one seed.",
        mime_type="application/schema+json",
    )
    def read_seed_schema() -> str:
        return canonical_seed_schema_text()

    @server.resource(
        ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI,
        name="Winnow v4 round-one authoring guide",
        description="Fixed safe guidance and a non-publishable structural v4 round-one example.",
        mime_type="text/markdown",
    )
    def read_round_one_authoring_guide() -> str:
        return round_one_authoring_guide()

    @server.tool(
        name="get_winnow_v4_seed_contract",
        description=(
            "Read the fixed Winnow v4 round-one schema and safe authoring guide. Use this before authoring a seed "
            "when MCP resources are not available to the host. It does not create, research, publish, or inspect a session."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def get_winnow_v4_seed_contract(ctx: Context) -> CallToolResult:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(seed_contract_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    annotations=Annotations(audience=["assistant"]),
                )
            ]
        )

    def wait_handoff(receipt: Mapping[str, Any], session_handle: str) -> TextContent:
        """Give every host a standard text form of the next private tool call.

        ``structuredContent`` is useful to schema-aware clients, but some
        hosts surface only regular MCP content blocks to their model.  The
        bearer is therefore delivered only in this direct, non-URL tool
        response; it is never recoverable from the public page.
        """

        arguments = {
            "sessionHandle": session_handle,
            "expectedRoundNumber": receipt["roundNumber"],
            "expectedSeedHash": receipt["seedHash"],
            "maxWaitSeconds": service.max_wait_seconds,
        }
        return TextContent(
            type="text",
            text=json.dumps({"nextTool": "wait_for_continue", "arguments": arguments}, separators=(",", ":"), sort_keys=True),
            annotations=Annotations(audience=["assistant"]),
        )

    def publish_handoff(event: Mapping[str, Any], session_handle: str) -> TextContent:
        """Expose the one durable browser event in standard assistant content."""

        return TextContent(
            type="text",
            text=json.dumps(publish_handoff_payload(event, session_handle), separators=(",", ":"), sort_keys=True),
            annotations=Annotations(audience=["assistant"]),
        )

    def wait_result(receipt: Mapping[str, Any], session_handle: str, expected_round_number: int, expected_seed_hash: str) -> CallToolResult:
        """Return every wait outcome as standard MCP content for host parity."""

        status = receipt.get("status")
        if status == "continue_requested":
            return CallToolResult(content=[publish_handoff(receipt, session_handle)], structuredContent=dict(receipt))
        if status in {"still_waiting", "publishing"}:
            next_receipt = {
                "roundNumber": receipt.get("roundNumber", expected_round_number),
                "seedHash": receipt.get("seedHash", expected_seed_hash),
            }
            return CallToolResult(content=[wait_handoff(next_receipt, session_handle)], structuredContent=dict(receipt))
        if status == "rejected":
            return CallToolResult(content=[], structuredContent=dict(receipt), isError=True)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(dict(receipt), separators=(",", ":"), sort_keys=True), annotations=Annotations(audience=["assistant"]))],
            structuredContent=dict(receipt),
        )

    @server.tool(
        name="create_winnow_session",
        description=(
            "After an explicit user request for a non-sensitive public-by-link comparison, publish a valid round-one seed. "
            "The host researches; Winnow only validates, publishes, and coordinates. Before calling, tell the user that "
            "link holders can read the temporary page and committed choices, guide a future round while this task waits, "
            "and that it expires; then proceed without another approval. Set mode to literal 'rolling'. Read "
            "get_winnow_v4_seed_contract or the winnow://contracts/v4/seed-schema.json and "
            "winnow://contracts/v4/round-one-authoring-guide resources before authoring. Show siteUrl, then immediately "
            "begin the renewable wait. Stop on cancellation or terminal state, deadline, expiry, or the 100-option cap."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
        structured_output=True,
    )
    async def create_winnow_session(seed: dict[str, Any], mode: Literal["rolling"], ctx: Context) -> CallToolResult:
        receipt = await service.create({"seed": seed, "mode": mode})
        if receipt.get("status") == "awaiting_agent_wait" and isinstance(receipt.get("siteUrl"), str):
            return CallToolResult(
                content=[
                    ResourceLink(name="Winnow session", uri=receipt["siteUrl"], mimeType="text/html"),
                    wait_handoff(receipt, str(receipt["sessionHandle"])),
                ],
                structuredContent=receipt,
            )
        return CallToolResult(content=[], structuredContent=receipt, isError=True)

    @server.tool(
        name="wait_for_continue",
        description=(
            "For a non-sensitive public-by-link session created after an explicit user request, renewably wait for one page-bound "
            "request for the current completed round. A link holder may guide one successor "
            "while this task waits; that browser credential is not user identity or owner authority and grants no agent, provider, "
            "or publication-fence power. On still_waiting, renew this wait without ending the task. On continue_requested, "
            "research the successor yourself, publish it, then wait again. Stop on cancellation or terminal state, research "
            "deadline, original expiry, or the 100-option cap."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        structured_output=True,
    )
    async def wait_for_continue(sessionHandle: str, expectedRoundNumber: int, expectedSeedHash: str, maxWaitSeconds: int, ctx: Context) -> CallToolResult:
        receipt = await service.wait(
            {
                "sessionHandle": sessionHandle,
                "expectedRoundNumber": expectedRoundNumber,
                "expectedSeedHash": expectedSeedHash,
                "maxWaitSeconds": maxWaitSeconds,
            }
        )
        return wait_result(receipt, sessionHandle, expectedRoundNumber, expectedSeedHash)

    @server.tool(
        name="publish_next_round",
        description=(
            "For a non-sensitive public-by-link session created after an explicit user request, validate and publish exactly one "
            "same-session successor after the accepted page-bound event for the current revision. "
            "nextSeed must be a complete successor seed, not merely the new round: copy the supplied continuation's immutable "
            "session, current profiles, and completed history, then construct its next round. "
            "The host researches; Winnow does not. The event does not grant user identity, owner authority, agent capability, "
            "provider access, or publication-fence authority. After success, immediately renew wait on the returned round and "
            "seed hash in this task. Stop on cancellation or terminal state, research deadline, original expiry, or the 100-option cap."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
        structured_output=True,
    )
    async def publish_next_round(sessionHandle: str, eventId: str, publishFence: str, parentSeedHash: str, nextSeed: dict[str, Any], ctx: Context) -> CallToolResult:
        receipt = await service.publish(
            {
                "sessionHandle": sessionHandle,
                "eventId": eventId,
                "publishFence": publishFence,
                "parentSeedHash": parentSeedHash,
                "nextSeed": nextSeed,
            }
        )
        if receipt.get("status") == "awaiting_agent_wait":
            return CallToolResult(content=[wait_handoff(receipt, sessionHandle)], structuredContent=receipt)
        return CallToolResult(content=[], structuredContent=receipt, isError=True)

    # The official SDK's dynamic function model permits unknown kwargs by
    # default. The ASGI guard rejects them before decoding, and this schema
    # annotation keeps discovery clients aligned with that closed boundary.
    for tool_name in ("get_winnow_v4_seed_contract", "create_winnow_session", "wait_for_continue", "publish_next_round"):
        tool = server._tool_manager.get_tool(tool_name)  # SDK has no public per-tool schema override.
        if tool is not None:
            tool.parameters["additionalProperties"] = False
