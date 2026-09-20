"""Which schema revision this code expects, and which one a database reports.

Two facts that are easy to confuse and expensive to get wrong. The first is a
property of the code — the head of the migration chain that ships with it. The
second is a property of a running database. A deployment is only coherent when
they agree, and every caller that wants to say so should be asking the same
question.

The expected revision is **derived from the migrations that are present**, never
written down a second time. A constant would be correct on the day it was typed
and quietly wrong at the next migration, which is precisely the failure this
module exists to make impossible: a readiness check that passes because it is
comparing against a revision nobody has shipped for months is worse than no
check, because it is trusted.

Nothing here writes, migrates or connects on its own. The recorded revision is
read through a connection the caller owns and bounds.
"""

from functools import cache
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

# The migration chain that ships beside this package.
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


class SchemaUnknown(RuntimeError):
    """The expected revision could not be determined from the code itself."""


@cache
def expected_revision() -> str:
    """The single head of the migration chain this code carries.

    Cached because it cannot change while the process runs: it is decided by the
    files on disk beside this module. More than one head means the chain has
    branched, which is a repository problem rather than a deployment one, and it
    is raised rather than resolved by picking one.
    """
    try:
        heads = ScriptDirectory(str(MIGRATIONS)).get_heads()
    except Exception as error:  # pragma: no cover - a broken checkout, not a state
        raise SchemaUnknown("The migration chain could not be read") from error
    if len(heads) != 1:
        raise SchemaUnknown("The migration chain does not have exactly one head")
    return heads[0]


async def recorded_revision(connection: AsyncConnection) -> str | None:
    """What the database says it has been migrated to, or nothing.

    `None` covers both "the table is not there" and "it is there and empty",
    because neither is a revision and a caller has the same thing to say about
    them: this database has not been migrated by anything that left a record.

    Presence is asked before the row is read, rather than by letting a missing
    table raise. A statement that fails leaves a PostgreSQL transaction poisoned
    for everything after it, so a check that answers "not migrated" by breaking
    the connection it borrowed would take its caller's later reads with it.

    Read-only throughout: nothing is created, migrated or written on the way.
    """

    def present(sync: Connection) -> bool:
        return inspect(sync).has_table("alembic_version")

    if not await connection.run_sync(present):
        return None
    row = (await connection.execute(text("SELECT version_num FROM alembic_version"))).first()
    return None if row is None else str(row[0])
