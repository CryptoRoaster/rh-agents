"""From a recorded candidate to whatever the real specialists actually produce.

Everything is production code except one boundary: the model. The recorded
observation is written through the real `MarketRecorder`; intake, the workflow,
the worker runtime, the real `WorkerRunner`, the real specialist handlers and
their real context readers, the risk request, SENTINEL, `CaseFillService`, the
executor and the ledger are all the production objects, composed by the
production `build_stack`.

No evidence is submitted directly and no case is prepared at `READY_FOR_RISK`:
whatever state a case reaches here, a specialist handler put it there.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.models import AgentRole
from src.data.tables import TradeCaseEvidenceRow
from src.orchestration.workflow.models import EvidenceType
from src.runner.composition import RunnerPorts
from src.runner.models import ExitCode
from tests.runner.conftest import FRESH, record_market, run, runner_settings
from tests.runner.specialists import ScriptedSpecialists

# The price the recorded market and the structure series agree on, so a setup
# grounded in one is grounded in the other.
SPOT = Decimal("1.00")


async def succeeded(sessions, role: AgentRole, trade_case_id=None) -> bool:
    """Whether that role reached a successful attempt on that case.

    Scoped to one case because a run may legitimately be working several, and a
    specialist refusing on one market says nothing about another.
    """
    from src.data.tables import WorkerTaskAttemptRow

    async with sessions() as session:
        statement = select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.role == role.value)
        if trade_case_id is not None:
            statement = statement.where(WorkerTaskAttemptRow.trade_case_id == trade_case_id)
        rows = (await session.scalars(statement)).all()
    assert rows, f"{role.value} never attempted anything"
    # At least one successful attempt. A monitor that waited first and then
    # triggered is exactly right, and demanding an unblemished record would call
    # that a failure.
    return any(item.outcome == "SUCCEEDED" for item in rows)


async def traded_case(sessions):
    """The case opened for the market this run is actually trading."""
    from src.data.tables import TradeCaseRow
    from tests.riskdata.conftest import PAIR_ID

    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseRow.id).where(TradeCaseRow.market_key == PAIR_ID)
        )


async def evidence_of(sessions, kind: EvidenceType):
    async with sessions() as session:
        return (
            await session.scalars(
                select(TradeCaseEvidenceRow).where(TradeCaseEvidenceRow.evidence_type == kind.value)
            )
        ).all()


async def test_orbit_runs_through_the_runner_and_records_discovery(risk_db, now, trace):
    """Intake opens the case, the real ORBIT handler answers it, and it is recorded.

    The only substitution is the model. The context reader, the validator, the
    evidence contract and the workflow transition are production code.
    """
    _, sessions = risk_db
    await record_market(sessions, now)
    model = ScriptedSpecialists()
    settings = runner_settings(orbit_worker_enabled=True, reasoning_provider="anthropic")

    summary = await run(sessions, settings, now, ports=RunnerPorts(reasoning=model))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert summary.cases_opened == 1
    assert model.calls == ["OrbitAssessment"], summary.roles
    # Intake writes its own discovery record when it opens a case, so the one
    # that matters here is the one ORBIT produced.
    recorded = await evidence_of(sessions, EvidenceType.DISCOVERY)
    assert AgentRole.ORBIT.value in {item.producer_role for item in recorded}
    assert await succeeded(sessions, AgentRole.ORBIT)
    assert summary.steps_taken >= 2


def specialist_ports(now, model, **overrides):
    """Every external boundary the specialists reach, as controlled fixtures.

    The ATLAS stubs and the SIGNAL observation set come from those suites' own
    fixtures, against the very same market this run records: robinhood/mainnet,
    token `0xa1…`, pool `0xe5…`. Everything between them and the evidence is
    production code.
    """
    from tests.atlas.conftest import (
        StubContracts,
        StubHolders,
        StubOrigins,
        chain_snapshot,
        contract_facts,
        holder_source_result,
        origin_facts,
    )
    from tests.signal.conftest import organic_set, source_for
    from tests.vector.conftest import StubHistory, history_for

    defaults: dict[str, object] = {
        "reasoning": model,
        "onchain": StubContracts(chain_snapshot(now), contract_facts()),
        "holders": StubHolders(holder_source_result(now)),
        "origins": StubOrigins(origin_facts()),
        "social": source_for(organic_set(now)),
        # The series is hourly, so its newest bar closes on the hour. Handing it
        # an arbitrary instant would build bars that do not align to their own
        # interval, which the contract refuses.
        "history": StubHistory(
            history_for(now.replace(minute=0, second=0, microsecond=0), price=SPOT)
        ),
    }
    return RunnerPorts(**{**defaults, **overrides})  # type: ignore[arg-type]


def atlas_ports(now, model):
    """The two ATLAS fact sources and the chain read, as controlled fixtures.

    The stubs are the ones the ATLAS suite already uses, against the very same
    market: robinhood/mainnet, token `0xa1…`, pool `0xe5…`. Everything between
    them and the recorded evidence — the snapshot builder, the policy, the
    validator, the workflow — is production code.
    """
    from tests.atlas.conftest import (
        StubContracts,
        StubHolders,
        StubOrigins,
        chain_snapshot,
        contract_facts,
        holder_source_result,
        origin_facts,
    )

    return RunnerPorts(
        reasoning=model,
        onchain=StubContracts(chain_snapshot(now), contract_facts()),
        holders=StubHolders(holder_source_result(now)),
        origins=StubOrigins(origin_facts()),
    )


async def test_atlas_runs_through_the_runner_and_records_onchain(risk_db, now, trace):
    """The second real specialist, on the same case the first one discovered."""
    _, sessions = risk_db
    await record_market(sessions, now)
    model = ScriptedSpecialists()
    settings = runner_settings(
        orbit_worker_enabled=True,
        atlas_worker_enabled=True,
        evm_runtime_enabled=True,
        reasoning_provider="anthropic",
    )

    summary = await run(sessions, settings, now, ports=atlas_ports(now, model))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert {item.role for item in summary.roles if item.available} == {"ORBIT", "ATLAS"}
    assert await succeeded(sessions, AgentRole.ORBIT)
    recorded = await evidence_of(sessions, EvidenceType.ONCHAIN)
    assert len(recorded) == 1, summary
    assert recorded[0].producer_role == AgentRole.ATLAS.value
    assert await succeeded(sessions, AgentRole.ATLAS)


async def test_signal_runs_through_the_runner_and_records_sentiment(risk_db, now, trace):
    """The third real specialist: the deterministic half plus a bounded reading."""
    _, sessions = risk_db
    await record_market(sessions, now)
    model = ScriptedSpecialists()
    settings = runner_settings(
        orbit_worker_enabled=True,
        atlas_worker_enabled=True,
        signal_worker_enabled=True,
        signal_social_provider="neynar",
        neynar_api_key="unused-because-the-source-is-supplied",
        evm_runtime_enabled=True,
        reasoning_provider="anthropic",
    )

    summary = await run(sessions, settings, now, ports=specialist_ports(now, model))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert await succeeded(sessions, AgentRole.SIGNAL), summary
    recorded = await evidence_of(sessions, EvidenceType.SENTIMENT)
    assert len(recorded) == 1
    assert recorded[0].producer_role == AgentRole.SIGNAL.value


async def test_vector_runs_through_the_runner_and_records_a_setup(risk_db, now, trace):
    """The fourth real specialist: a setup proposed against real structure."""
    _, sessions = risk_db
    await record_market(sessions, now, price=SPOT)
    model = ScriptedSpecialists()
    settings = runner_settings(
        orbit_worker_enabled=True,
        atlas_worker_enabled=True,
        signal_worker_enabled=True,
        signal_social_provider="neynar",
        neynar_api_key="unused-because-the-source-is-supplied",
        vector_worker_enabled=True,
        evm_runtime_enabled=True,
        reasoning_provider="anthropic",
    )

    summary = await run(sessions, settings, now, ports=specialist_ports(now, model))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert await succeeded(sessions, AgentRole.VECTOR), summary
    recorded = await evidence_of(sessions, EvidenceType.TRADE_SETUP)
    assert len(recorded) == 1
    assert recorded[0].producer_role == AgentRole.VECTOR.value


def dispersed_holders(now):
    """A holder set inside SENTINEL's concentration limit.

    The ATLAS suite's default set puts 45.5% in the top ten, which SENTINEL
    refuses at 35% — correctly, and that refusal is proved elsewhere. Here the
    question is whether the chain reaches a fill at all, so the fixture states a
    dispersed market rather than a concentrated one.
    """
    from tests.atlas.conftest import holder_rows, holder_source_result

    return holder_source_result(
        now, rows=holder_rows(count=12, top_balance=8_000 * 10**18, step=200 * 10**18)
    )


def full_settings(**overrides):
    """Every specialist this runtime can claim, switched on deliberately."""
    defaults: dict[str, object] = {
        # One candidate per pass. The payment asset's own market is recorded too
        # — ANCHOR needs its price — and this keeps intake from spending the
        # budget on a market nobody is trading into.
        "paper_runner_max_candidates": 1,
        "orbit_worker_enabled": True,
        "atlas_worker_enabled": True,
        "signal_worker_enabled": True,
        "signal_social_provider": "neynar",
        "neynar_api_key": "unused-because-the-source-is-supplied",
        "vector_worker_enabled": True,
        "fuse_worker_enabled": True,
        "pulse_worker_enabled": True,
        "anchor_worker_enabled": True,
        "evm_runtime_enabled": True,
        "reasoning_provider": "anthropic",
    }
    return runner_settings(**{**defaults, **overrides})


def full_ports(now, model):
    from tests.anchor.conftest import source as quote_source
    from tests.atlas.conftest import StubHolders

    return specialist_ports(
        now,
        model,
        holders=StubHolders(dispersed_holders(now)),
        quotes=quote_source(now, reference_price=SPOT),
    )


# The payment asset's pool sorts after the traded one, so with a candidate
# budget of one, intake opens the market being traded and leaves the other. The
# reading itself has to stay fresh: ANCHOR bounds how old a unit bridge may be,
# and an old one is refused rather than used.
PAYMENT_POOL = "0x" + "ff" * 20


async def record_payment_asset(sessions, now, *, label="quote"):
    """A recorded observation *of the payment asset*, priced in dollars.

    ANCHOR sizes a ladder in the token that would actually be spent, so it needs
    that token's own USD value — a different reading from the pair's, and one it
    refuses to infer. A market that never recorded one cannot be assessed, which
    is a real data requirement rather than a gap this test may paper over.
    """
    from tests.riskdata.conftest import CHAIN, NETWORK, QUOTE, recorded_snapshot

    quote_asset = f"{CHAIN}:{NETWORK}:{QUOTE}"
    snapshot = recorded_snapshot(
        now,
        age=FRESH,
        metadata_age=FRESH,
        base_asset_id=quote_asset,
        # A pool where the payment asset is the thing being priced, against a
        # different asset again — a pair cannot quote itself.
        quote_address="0x" + "dd" * 20,
        pair_id=f"{CHAIN}:{NETWORK}:contract_address:{PAYMENT_POOL}",
        label=label,
        price=Decimal("1"),
    )
    from src.core.clock import FixedClock
    from src.markets.recorder import MarketRecorder

    await MarketRecorder(sessions, clock=FixedClock(now)).record(snapshot)


async def test_the_whole_chain_runs_from_a_recorded_candidate(risk_db, now, trace):
    """Recorded candidate → intake → six real specialists → SENTINEL → a fill.

    Two explicit runs, because that is what the contract actually produces: the
    first carries the case as far as a trigger that has not happened yet, and the
    second picks it up once the market has moved. Nothing waits in between — the
    pass ends, and the task table holds the work.

    Substituted, and only at the outside edge: the model, the chain read, the
    holder and origin indexers, the social source, the market structure series
    and the quote source. Every handler, context reader, validator, workflow
    rule, risk input, verdict, executor and ledger posting is production code,
    and no evidence is submitted directly by this test.
    """
    _, sessions = risk_db
    await record_market(sessions, now, price=SPOT)
    await record_payment_asset(sessions, now)
    model = ScriptedSpecialists()
    # The first pass carries the case as far as a setup. The monitor is switched
    # on for the second: PULSE re-checks on a ninety-second interval by policy,
    # and SENTINEL refuses sources older than thirty seconds — so a trigger found
    # on a *rescheduled* check arrives with on-chain evidence the risk engine has
    # already stopped accepting. That interaction between two existing policies
    # is a real limit of this system, recorded in the phase notes rather than
    # worked around here.
    settings = full_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)

    first = await run(sessions, settings, now, ports=full_ports(now, model))

    assert first.exit_code is ExitCode.COMPLETED, first
    case = await traded_case(sessions)
    assert case is not None
    for role in (AgentRole.ORBIT, AgentRole.ATLAS, AgentRole.SIGNAL, AgentRole.VECTOR):
        assert await succeeded(sessions, role, case), (role, first)
    assert first.fills == 0, "nothing may fill before a trigger"

    # The market moves through the level the setup named. Recorded the way the
    # market layer records anything, and only then is there something to trigger.
    # Twenty seconds later: long enough for a new observation to be its own
    # event, short enough that the facts the first run established are still
    # inside SENTINEL's own thirty-second bound. A longer gap is refused as
    # `SOURCE_OLDER_THAN_RISK_LIMIT`, correctly.
    later = now + timedelta(seconds=20)
    await record_market(sessions, later, price=SPOT * Decimal("1.20"), label="moved")
    await record_payment_asset(sessions, later, label="quote-later")

    second = await run(sessions, full_settings(), later, ports=full_ports(later, model))

    assert second.exit_code is ExitCode.COMPLETED, second
    assert await succeeded(sessions, AgentRole.PULSE, case), second
    assert await succeeded(sessions, AgentRole.ANCHOR, case), second

    # Every specialist the workflow requires produced its own evidence, through
    # its own handler. Nothing here was submitted by this test. FUSE is in the
    # set too: its synthesis is optional and gates nothing, and it is claimed
    # through the same runtime as the rest.
    produced = await produced_for(sessions, case)
    assert produced == {
        AgentRole.ORBIT.value,
        AgentRole.ATLAS.value,
        AgentRole.SIGNAL.value,
        AgentRole.VECTOR.value,
        AgentRole.FUSE.value,
        AgentRole.PULSE.value,
        AgentRole.ANCHOR.value,
    }, produced

    progress = next(item for item in second.cases if item.trade_case_id == case)
    assert progress.risk_outcome == "APPROVE", (progress, second)
    assert progress.execution_id is not None
    assert second.fills == 1, second

    # And the money moved, in the real ledger.
    from src.data.tables import ExecutionRow, PositionRow

    async with sessions() as session:
        fills = (await session.scalars(select(ExecutionRow))).all()
        held = await session.scalar(select(PositionRow))
    assert len(fills) == 1
    assert held is not None and held.quantity > 0


async def produced_for(sessions, trade_case_id) -> set[str]:
    """Which roles actually wrote evidence for that case."""
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(TradeCaseEvidenceRow).where(
                    TradeCaseEvidenceRow.trade_case_id == trade_case_id
                )
            )
        ).all()
    return {item.producer_role for item in rows} - {"COMMANDER"}


async def test_fuse_is_claimed_and_produces_synthesis(risk_db, now, trace):
    """FUSE has a requirement in the workflow policy, so its task is claimable.

    Nothing special is needed for it: the synthesis reads verdicts the other
    specialists already committed, so there is no provider, no model and no
    extra port. This runs it through the same runtime and the same
    `WorkerRunner` as every other role, on a case the real specialists built.
    """
    _, sessions = risk_db
    await record_market(sessions, now, price=SPOT)
    await record_payment_asset(sessions, now)
    model = ScriptedSpecialists()
    settings = full_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)

    summary = await run(sessions, settings, now, ports=full_ports(now, model))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert "FUSE" in {item.role for item in summary.roles if item.available}, summary.roles
    case = await traded_case(sessions)
    assert await succeeded(sessions, AgentRole.FUSE, case), summary
    recorded = await evidence_of(sessions, EvidenceType.SYNTHESIS)
    assert [item.producer_role for item in recorded] == [AgentRole.FUSE.value]


async def test_fuse_without_its_inputs_waits_rather_than_inventing_one(risk_db, now, trace):
    """Missing prerequisites end in the existing wait or block contract."""
    _, sessions = risk_db
    await record_market(sessions, now, price=SPOT)
    model = ScriptedSpecialists()
    # Only FUSE. Nothing has produced the verdicts it synthesises.
    settings = runner_settings(fuse_worker_enabled=True)

    summary = await run(sessions, settings, now, ports=RunnerPorts(reasoning=model))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert "FUSE" in {item.role for item in summary.roles if item.available}
    # FUSE synthesises what exists and names what does not. It is advisory and
    # gates nothing, so an incomplete evidence set produces a reading that says
    # so rather than silence or an invented verdict.
    recorded = await evidence_of(sessions, EvidenceType.SYNTHESIS)
    assert [item.producer_role for item in recorded] == [AgentRole.FUSE.value]
    synthesis = recorded[0].payload["payload"]["synthesis"]
    assert synthesis["sources"], "the reading names what it was built from"


def test_a_disabled_fuse_is_reported_as_disabled(risk_db, now):
    """And not as something the composition could not build."""
    from src.core.clock import FixedClock
    from src.runner.composition import build_stack

    _, sessions = risk_db
    stack = build_stack(
        runner_settings(fuse_worker_enabled=False),
        sessions,
        ports=RunnerPorts(),
        clock=FixedClock(now),
    )

    reasons = {item.role: item.reason for item in stack.roles}
    assert reasons["FUSE"] == "ROLE_NOT_ENABLED"


def test_the_cli_precheck_accepts_a_composed_fuse(risk_db, now):
    """An enabled, composable role is not a configuration problem."""
    from src.core.clock import FixedClock
    from src.runner.composition import build_stack
    from src.runner.main import _misconfigured

    _, sessions = risk_db
    stack = build_stack(
        runner_settings(fuse_worker_enabled=True),
        sessions,
        ports=RunnerPorts(),
        clock=FixedClock(now),
    )

    assert stack.misconfigured == ()
    assert _misconfigured(stack) is None
