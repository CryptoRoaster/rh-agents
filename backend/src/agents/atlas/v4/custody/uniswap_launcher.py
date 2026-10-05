"""Official Uniswap Liquidity Launcher custody: FeeSplitter and TimelockedPositionRecipient.

Source: https://github.com/Uniswap/liquidity-launcher. Both templates are the
runtime code built from commit ``7ea523c9d75a51cb2f497be5e49bacdaeb80a342``
("release: v3.3.0", the commit the v3.3.0 FeeSplitter deployments name) with
the repository's own settings: solc 0.8.26+commit.8a97fa7a, optimizer 200
runs, EVM cancun, ``bytecode_hash = "none"``. `docs/atlas-v4-position-control.md`
records how to rebuild them and how the deployments were tied to that build.

FeeSplitter (``src/periphery/FeeSplitter.sol``, unchanged from v3.2.0 to the
v3.3.0 tag) is permanently locking by construction. Its complete external
surface is ``getSplits``, ``collectFees``, ``increaseLiquidity``,
``onERC721Received`` (a view), ``receive`` and constant/immutable getters:

* no function transfers, approves or burns a position NFT, and it has neither
  ``isValidSignature`` nor a fallback, so no ERC-721 permit can approve one;
* ``collectFees`` runs exactly ``DECREASE_LIQUIDITY(tokenId, 0, 0, 0)`` and
  ``TAKE_PAIR`` to itself: a zero-liquidity decrease realises fees only;
* ``increaseLiquidity`` runs ``UNWRAP, SETTLE, SETTLE, INCREASE_LIQUIDITY,
  TAKE_PAIR``: liquidity only grows, and accrued fees must be zero first;
* no owner, admin, upgrade, ``delegatecall``, ``selfdestruct`` or arbitrary
  call; its only storage is the fee split, written once in the constructor.

Fee splits route *fees* -- to a beneficiary vault and a compounder -- and give
no recipient any power over principal. A fee beneficiary is not a controller.

TimelockedPositionRecipient holds positions until ``timelockBlockNumber`` and
then lets anyone call ``approveOperator``, which runs
``setApprovalForAll(operator, true)`` on the PositionManager. Before that block
nobody can move a position; from it on, ``operator`` can. Its block clock comes
from BlockNumberish v1.1.0: with ``_USE_ARB_SYS`` set (an ArbSys precompile
answered at construction) it is the Arbitrum L2 block number, which on an
Arbitrum Orbit chain is the block number the snapshot is pinned to; without it
it is ``block.number``, an L1 figure there, and the timelock cannot be judged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents.atlas.v4.control import (
    CustodyChainRefused,
    CustodyQuery,
    CustodyReadPort,
    CustodyRefusal,
    CustodyVerdict,
    PositionControlState,
)
from src.agents.atlas.v4.custody.template import CodeTemplate, as_address, match_template

REPOSITORY = "https://github.com/Uniswap/liquidity-launcher"
BUILD_COMMIT = "7ea523c9d75a51cb2f497be5e49bacdaeb80a342"
BUILD = "solc 0.8.26+commit.8a97fa7a; optimizer 200 runs; evm cancun; bytecode_hash none"

FEE_SPLITTER_TEMPLATE = CodeTemplate(
    contract="FeeSplitter",
    version="v3.3.0",
    source_repository=REPOSITORY,
    source_commit=BUILD_COMMIT,
    build=BUILD,
    length=7260,
    masked_keccak="0x7a79bab4411f0d8bace4d25549d7e4aa79b5539149fcb4d95a38c38c47ecb77e",
    immutables={
        "positionManager": (313, 486, 801, 1373, 1554, 2342, 2449, 2828, 3293, 4001),
        "poolManager": (419, 676, 2269, 4068, 4183),
    },
)

TIMELOCKED_RECIPIENT_TEMPLATE = CodeTemplate(
    contract="TimelockedPositionRecipient",
    version="v3.3.0",
    source_repository=REPOSITORY,
    source_commit=BUILD_COMMIT,
    build=BUILD,
    length=707,
    masked_keccak="0x7cbaa225f6d227ae20e14de773ddb46ac30a33923ba7ff4f153a7c76026d2f77",
    immutables={
        "operator": (93, 389, 534),
        "positionManager": (173, 436),
        "timelockBlockNumber": (224, 296),
        "_USE_ARB_SYS": (611,),
    },
)


@dataclass(frozen=True)
class FeeSplitterDeployment:
    """One official FeeSplitter, as the launcher's README lists it."""

    address: str
    version: str
    # The commit the README names for this deployment.
    deploy_commit: str
    position_manager: str
    pool_manager: str


@dataclass(frozen=True)
class ChainCustodyRegistry:
    chain: str
    chain_id: int
    fee_splitters: tuple[FeeSplitterDeployment, ...]
    # Whether the pinned block number is the Arbitrum L2 block number that
    # BlockNumberish reads through ArbSys on this chain.
    arbsys_block_clock: bool


_ROBINHOOD_POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
_ROBINHOOD_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

# Every listed address and version comes from the launcher README. The v3.3.0
# entries are additionally proven offline: the pinned build's creation code with
# the README's fee splits and salt zero, deployed through the CREATE2 deployer
# 0x4e59b44847b379578588920ca78fbf26c0b4956c, yields exactly these addresses.
# The v3.2.0 entries name a commit whose FeeSplitter source and creation code
# are identical, but their constructor arguments or salt could not be
# reproduced offline; they are accepted only because, like every entry, the
# deployed code must still match the pinned template at the snapshot block.
REGISTRIES: Mapping[str, ChainCustodyRegistry] = {
    "robinhood": ChainCustodyRegistry(
        chain="robinhood",
        chain_id=4663,
        fee_splitters=(
            FeeSplitterDeployment(
                address="0x9411fa7f956f64aa7981aa27cb3bc6ec0415449c",
                version="v3.3.0",
                deploy_commit=BUILD_COMMIT,
                position_manager=_ROBINHOOD_POSITION_MANAGER,
                pool_manager=_ROBINHOOD_POOL_MANAGER,
            ),
            FeeSplitterDeployment(
                address="0x882ae5e2095435a62fd1bbdefcb637f5ceafc0ee",
                version="v3.3.0",
                deploy_commit=BUILD_COMMIT,
                position_manager=_ROBINHOOD_POSITION_MANAGER,
                pool_manager=_ROBINHOOD_POOL_MANAGER,
            ),
            FeeSplitterDeployment(
                address="0xeff166aaf189323c58dc27ed1206eb2c37faacdf",
                version="v3.2.0",
                deploy_commit="dd8769cd45c0e9450e928513ee129b0af74f7f32",
                position_manager=_ROBINHOOD_POSITION_MANAGER,
                pool_manager=_ROBINHOOD_POOL_MANAGER,
            ),
            FeeSplitterDeployment(
                address="0x222d6d4f1ce59b0d48d5505114ec8addc90a4359",
                version="v3.2.0",
                deploy_commit="dd8769cd45c0e9450e928513ee129b0af74f7f32",
                position_manager=_ROBINHOOD_POSITION_MANAGER,
                pool_manager=_ROBINHOOD_POOL_MANAGER,
            ),
        ),
        arbsys_block_clock=True,
    ),
}

FEE_SPLITTER_PROOF = "uniswap-liquidity-launcher:FeeSplitter"
TIMELOCKED_RECIPIENT_PROOF = "uniswap-liquidity-launcher:TimelockedPositionRecipient"


def registry_for(
    query: CustodyQuery, registries: Mapping[str, ChainCustodyRegistry]
) -> ChainCustodyRegistry | None:
    """The chain's registry; a name pinned to another chain id is a hard stop."""
    registry = registries.get(query.chain)
    if registry is not None and registry.chain_id != query.chain_id:
        raise CustodyChainRefused(query.chain)
    return registry


@dataclass(frozen=True)
class FeeSplitterAdapter:
    """PERMANENTLY_LOCKED for an official FeeSplitter deployment, and only that.

    Three things must hold at once: the owner's code is exactly the pinned
    FeeSplitter build; the owner is a FeeSplitter the launcher deployed on this
    very chain; and the code's own immutables bind it to the PositionManager
    and PoolManager the census verified. Official code at an unlisted address
    is not accepted: the deployment context is part of the proof.
    """

    registries: Mapping[str, ChainCustodyRegistry] = field(default_factory=lambda: REGISTRIES)
    template: CodeTemplate = FEE_SPLITTER_TEMPLATE
    name: str = FEE_SPLITTER_PROOF

    async def verify(self, reads: CustodyReadPort, query: CustodyQuery) -> CustodyVerdict | None:
        values = match_template(query.owner_code, self.template)
        if values is None:
            return None
        registry = registry_for(query, self.registries)
        deployment = (
            None
            if registry is None
            else next(
                (item for item in registry.fee_splitters if item.address == query.owner), None
            )
        )
        version = self.template.version if deployment is None else deployment.version

        def unknown(refusal: CustodyRefusal) -> CustodyVerdict:
            return CustodyVerdict(
                state=PositionControlState.UNKNOWN_CONTRACT_CUSTODY,
                proof_contract=self.name,
                proof_version=version,
                refusal=refusal,
            )

        if registry is None:
            return unknown(CustodyRefusal.CHAIN_NOT_SUPPORTED)
        if deployment is None:
            return unknown(CustodyRefusal.DEPLOYMENT_MISMATCH)
        bound_position_manager = as_address(values["positionManager"])
        bound_pool_manager = as_address(values["poolManager"])
        if (
            bound_position_manager != query.position_manager
            or bound_position_manager != deployment.position_manager
            or bound_pool_manager != query.pool_manager
            or bound_pool_manager != deployment.pool_manager
        ):
            return unknown(CustodyRefusal.POSITION_MANAGER_MISMATCH)
        return CustodyVerdict(
            state=PositionControlState.PERMANENTLY_LOCKED,
            proof_contract=self.name,
            proof_version=deployment.version,
        )


@dataclass(frozen=True)
class TimelockedRecipientAdapter:
    """TIMELOCKED before the timelock block, RELEASABLE to ``operator`` from it.

    The launcher has no registry of these: each is deployed for one migration
    with its own operator and timelock. What is proven is the code -- exactly
    the pinned build -- and the immutables it carries, which fully determine
    what it can ever do. Its PositionManager must be the verified one, its
    block clock must be one the pinned block number measures, and its operator
    must be a real address.
    """

    registries: Mapping[str, ChainCustodyRegistry] = field(default_factory=lambda: REGISTRIES)
    template: CodeTemplate = TIMELOCKED_RECIPIENT_TEMPLATE
    name: str = TIMELOCKED_RECIPIENT_PROOF

    async def verify(self, reads: CustodyReadPort, query: CustodyQuery) -> CustodyVerdict | None:
        values = match_template(query.owner_code, self.template)
        if values is None:
            return None
        registry = registry_for(query, self.registries)

        def unknown(refusal: CustodyRefusal) -> CustodyVerdict:
            return CustodyVerdict(
                state=PositionControlState.UNKNOWN_CONTRACT_CUSTODY,
                proof_contract=self.name,
                proof_version=self.template.version,
                refusal=refusal,
            )

        if registry is None:
            return unknown(CustodyRefusal.CHAIN_NOT_SUPPORTED)
        if as_address(values["positionManager"]) != query.position_manager:
            return unknown(CustodyRefusal.POSITION_MANAGER_MISMATCH)
        operator = as_address(values["operator"])
        if operator is None or int(operator, 16) == 0 or values["_USE_ARB_SYS"] not in (0, 1):
            return unknown(CustodyRefusal.IMMUTABLE_INVALID)
        if values["_USE_ARB_SYS"] != 1 or not registry.arbsys_block_clock:
            return unknown(CustodyRefusal.BLOCK_CLOCK_UNSUPPORTED)
        unlock = values["timelockBlockNumber"]
        # `approveOperator` reverts while blockNumberish < timelockBlockNumber.
        if query.block >= unlock:
            return CustodyVerdict(
                state=PositionControlState.RELEASABLE,
                proof_contract=self.name,
                proof_version=self.template.version,
                controller=operator,
                unlock_block=unlock,
            )
        return CustodyVerdict(
            state=PositionControlState.TIMELOCKED,
            proof_contract=self.name,
            proof_version=self.template.version,
            unlock_block=unlock,
        )
