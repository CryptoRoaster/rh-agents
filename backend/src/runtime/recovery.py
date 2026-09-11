"""Verified HTTP recovery is authoritative for processable chain progress.

WebSocket notifications only wake recovery. Logs are persisted after confirmation
lag, with matching fetched block hashes; notifications never bypass this path.
"""

from uuid import UUID

from sqlalchemy import select

from src.core.clock import Clock
from src.core.config import Settings
from src.data.tables import EvmCursorRow
from src.runtime.models import ChainConfig, ErrorCode, Head, Log, RuntimeFailure, SubscriptionSpec
from src.runtime.rpc import EvmRpcClient
from src.runtime.store import RuntimeStore


class Recovery:
    def __init__(
        self,
        config: ChainConfig,
        settings: Settings,
        rpc: EvmRpcClient,
        store: RuntimeStore,
        clock: Clock,
        session_id: UUID,
        specs: tuple[SubscriptionSpec, ...] = (),
    ) -> None:
        if len(specs) > 20 or any(spec.chain != config.chain for spec in specs):
            raise RuntimeFailure(ErrorCode.CONFIGURATION)
        self.config, self.settings, self.rpc, self.store = config, settings, rpc, store
        self.clock, self.session_id, self.specs = clock, session_id, specs
        self.gap: tuple[int, int] | None = None
        self.reorg = False

    async def accept(self, notification: Head | None = None) -> None:
        number = await self.rpc.block_number()
        head = await self.rpc.block(number)
        if notification is not None:
            verified = await self.rpc.block(notification.number)
            if verified.hash != notification.hash:
                # HTTP and WSS disagree: don't select a winner or advance.
                raise RuntimeFailure(ErrorCode.REORG)
        safe = max(0, head.number - self.config.confirmations)
        cursor = await self.store.cursor(self.config.chain)
        if cursor is None:
            # Explicit bootstrap anchor: no claim to have scanned pre-start history.
            anchor = await self.rpc.block(max(0, safe - 1))
            async with self.store.sessions.begin() as session:
                session.add(
                    EvmCursorRow(
                        chain=self.config.chain,
                        network="mainnet",
                        last_seen_head=head.number,
                        last_safe_block=safe,
                        last_processed_block=anchor.number,
                        updated_at=self.clock.now(),
                        session_id=self.session_id,
                        payload={
                            "hashes": {str(anchor.number): anchor.hash},
                            "head": head.model_dump(mode="json"),
                        },
                    )
                )
                session.add(
                    self.store.audit_row(
                        self.config.chain,
                        "evm_rpc",
                        "BOOTSTRAP",
                        self.session_id,
                        {"history_starts_after": anchor.number, "state": "STARTING"},
                    )
                )
            cursor = await self.store.cursor(self.config.chain)
            assert cursor is not None
        hashes: dict[str, str] = dict(cursor.payload["hashes"])
        previous_head = Head.model_validate(cursor.payload["head"])
        previous = cursor.last_processed_block
        current_anchor = await self.rpc.block(previous)
        changed = current_anchor.hash != hashes.get(str(previous))
        if head.number == previous_head.number and head.hash != previous_head.hash:
            changed = True
        if head.number == previous_head.number + 1 and head.parent_hash != previous_head.hash:
            changed = True
        if head.number < previous_head.number:
            changed = True
        if changed:
            self.reorg = True
            await self.store.audit(
                self.config.chain,
                "evm_rpc",
                "REORG_DETECTED",
                self.session_id,
                {
                    "state": "DEGRADED",
                    "error_category": ErrorCode.REORG.value,
                    "last_processed_block": previous,
                },
            )
            ancestor = None
            for candidate in range(
                min(previous, safe), max(-1, previous - self.settings.evm_reorg_window), -1
            ):
                known = hashes.get(str(candidate))
                if known is not None and (await self.rpc.block(candidate)).hash == known:
                    ancestor = candidate
                    break
            if ancestor is None:
                raise RuntimeFailure(ErrorCode.DEEP_REORG)
            async with self.store.sessions.begin() as session:
                locked = await session.scalar(
                    select(EvmCursorRow)
                    .where(EvmCursorRow.chain == self.config.chain)
                    .with_for_update()
                )
                if locked is None or locked.last_processed_block != previous:
                    raise RuntimeFailure(ErrorCode.CURSOR_RACE)
                locked.last_processed_block = ancestor
                locked.payload = {
                    "hashes": {k: v for k, v in hashes.items() if int(k) <= ancestor},
                    "head": head.model_dump(mode="json"),
                }
                locked.updated_at = self.clock.now()
                session.add(
                    self.store.audit_row(
                        self.config.chain,
                        "evm_rpc",
                        "REWIND",
                        self.session_id,
                        {
                            "from_block": previous,
                            "to_block": ancestor,
                            "invalidated_after": ancestor,
                        },
                    )
                )
            previous = ancestor
            hashes = {k: v for k, v in hashes.items() if int(k) <= ancestor}
        if safe < previous:
            raise RuntimeFailure(ErrorCode.REORG)
        self.gap = (previous + 1, safe) if safe > previous else None
        if self.gap:
            await self.store.audit(
                self.config.chain,
                "evm_rpc",
                "GAP",
                self.session_id,
                {"gap_detected": True, "gap_start": self.gap[0], "gap_end": self.gap[1]},
            )
        while previous < safe:
            end = min(safe, previous + self.settings.evm_recovery_chunk_size)
            blocks = []
            parent = hashes[str(previous)]
            for number in range(previous + 1, end + 1):
                block = await self.rpc.block(number)
                if block.parent_hash != parent:
                    raise RuntimeFailure(ErrorCode.REORG)
                blocks.append(block)
                parent = block.hash
            by_number = {block.number: block for block in blocks}
            logs: dict[tuple[str, str, int], tuple[Log, set[str]]] = {}
            for spec in self.specs:
                for log in await self.rpc.logs(spec, previous + 1, end):
                    if by_number[log.block_number].hash != log.block_hash:
                        raise RuntimeFailure(ErrorCode.REORG)
                    key = (log.block_hash, log.transaction_hash, log.log_index)
                    if key in logs:
                        existing, decoders = logs[key]
                        if existing != log:
                            raise RuntimeFailure(ErrorCode.CONFLICT)
                        decoders.add(spec.decoder)
                    else:
                        logs[key] = (log, {spec.decoder})
            # Detect branch change during range fetch before committing its cursor.
            if (await self.rpc.block(end)).hash != blocks[-1].hash:
                raise RuntimeFailure(ErrorCode.REORG)
            hashes.update({str(block.number): block.hash for block in blocks})
            hashes = {
                k: v for k, v in hashes.items() if int(k) >= end - self.settings.evm_reorg_window
            }
            async with self.store.sessions.begin() as session:
                locked = await session.scalar(
                    select(EvmCursorRow)
                    .where(EvmCursorRow.chain == self.config.chain)
                    .with_for_update()
                )
                if locked is None or locked.last_processed_block != previous:
                    raise RuntimeFailure(ErrorCode.CURSOR_RACE)
                for log, decoders in sorted(
                    logs.values(),
                    key=lambda item: (
                        item[0].block_number,
                        item[0].transaction_index,
                        item[0].log_index,
                    ),
                ):
                    await self.store.save_log(
                        session,
                        self.config.chain,
                        log,
                        self.clock.now(),
                        self.session_id,
                        tuple(sorted(decoders)),
                    )
                locked.last_processed_block = end
                locked.last_seen_head, locked.last_safe_block = head.number, safe
                locked.updated_at, locked.session_id = self.clock.now(), self.session_id
                locked.payload = {"hashes": hashes, "head": head.model_dump(mode="json")}
                session.add(
                    self.store.audit_row(
                        self.config.chain,
                        "evm_rpc",
                        "PROGRESS",
                        self.session_id,
                        {"from_block": previous + 1, "through_block": end, "logs": len(logs)},
                    )
                )
            previous = end
        # Refresh observed head metadata even when no safe block became processable.
        # Otherwise a resolved shallow head reorg would be detected repeatedly.
        async with self.store.sessions.begin() as session:
            locked = await session.scalar(
                select(EvmCursorRow)
                .where(EvmCursorRow.chain == self.config.chain)
                .with_for_update()
            )
            if locked is None or locked.last_processed_block != safe:
                raise RuntimeFailure(ErrorCode.CURSOR_RACE)
            locked.last_seen_head, locked.last_safe_block = head.number, safe
            locked.updated_at, locked.session_id = self.clock.now(), self.session_id
            locked.payload = {"hashes": hashes, "head": head.model_dump(mode="json")}
        self.gap = None
