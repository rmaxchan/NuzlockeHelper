from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine, select

MAX_PARTY_SIZE = 6 #add custom party size rules later

# --- Database setup ---------------------------------------------------------

engine = create_engine(
    "sqlite:///nuzlocke.db",
    connect_args={"check_same_thread": False},
)


def get_session():
    """Dependency: one DB session per request, closed automatically."""
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)  # creates any missing tables
    yield


# --- Run models -------------------------------------------------------------


class RunBase(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    game: str = Field(min_length=1, max_length=100)


class Run(RunBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True

    # Deleting a run deletes its encounters (cascade)
    encounters: list["Encounter"] = Relationship(
        back_populates="run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class RunCreate(RunBase):
    pass


class RunRead(RunBase):
    id: int
    created_at: datetime
    is_active: bool


class RunUpdate(SQLModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    game: Optional[str] = Field(default=None, min_length=1, max_length=100)
    is_active: Optional[bool] = None


# --- Encounter models -------------------------------------------------------


class EncounterStatus(str, Enum):
    caught = "caught"
    failed = "failed"
    #fled = "fled"


class EncounterBase(SQLModel):
    area: str = Field(min_length=1, max_length=100)
    species: str = Field(min_length=1, max_length=100)
    nickname: Optional[str] = Field(default=None, max_length=50)
    status: EncounterStatus


class Encounter(EncounterBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    is_alive: bool = True
    in_party: bool = False

    run: Optional[Run] = Relationship(back_populates="encounters")


class EncounterCreate(EncounterBase):
    in_party: bool = False


class EncounterRead(EncounterBase):
    id: int
    run_id: int
    is_alive: bool
    in_party: bool


class EncounterUpdate(SQLModel):
    area: Optional[str] = Field(default=None, min_length=1, max_length=100)
    species: Optional[str] = Field(default=None, min_length=1, max_length=100)
    nickname: Optional[str] = Field(default=None, max_length=50)
    status: Optional[EncounterStatus] = None
    is_alive: Optional[bool] = None
    in_party: Optional[bool] = None


# --- Helpers (lookups + Nuzlocke rules) -------------------------------------


def get_run_or_404(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def get_encounter_or_404(session: Session, run_id: int, encounter_id: int) -> Encounter:
    get_run_or_404(session, run_id)
    enc = session.get(Encounter, encounter_id)
    # Same-run sanity check
    if not enc or enc.run_id != run_id:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return enc


def ensure_area_is_free(
    session: Session, run_id: int, area: str, exclude_id: Optional[int] = None
):
    """Rule: one encounter per area per run"""
    query = select(Encounter).where(
        Encounter.run_id == run_id,
        func.lower(Encounter.area) == area.strip().lower(),
    )
    if exclude_id is not None:
        query = query.where(Encounter.id != exclude_id)
    if session.exec(query).first():
        raise HTTPException(
            status_code=409, detail=f"Area '{area}' already has an encounter in this run"
        )


def party_size(session: Session, run_id: int, exclude_id: Optional[int] = None) -> int:
    query = select(func.count(Encounter.id)).where(
        Encounter.run_id == run_id, Encounter.in_party == True  # noqa: E712
    )
    if exclude_id is not None:
        query = query.where(Encounter.id != exclude_id)
    return session.exec(query).one()


def resolve_party_flag(
    session: Session,
    run_id: int,
    *,
    status: EncounterStatus,
    is_alive: bool,
    wants_party: bool,
    explicitly_requested: bool,
    currently_in_party: bool = False,
    exclude_id: Optional[int] = None,
) -> bool:
    """Decide the final in_party value, enforcing the party rules."""
    eligible = status == EncounterStatus.caught and is_alive
    if wants_party and not eligible:
        if explicitly_requested:
            raise HTTPException(
                status_code=409,
                detail="Only living, caught Pokémon can be in the party",
            )
        return False  # dead or no longer caught: quietly removed from party
    if wants_party and not currently_in_party:
        if party_size(session, run_id, exclude_id) >= MAX_PARTY_SIZE:
            raise HTTPException(
                status_code=409, detail=f"Party is full ({MAX_PARTY_SIZE} max)"
            )
    return wants_party


# --- App & Run routes -------------------------------------------------------

app = FastAPI(title="Nuzlocke Helper", lifespan=lifespan)


@app.post("/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_run(data: RunCreate, session: Session = Depends(get_session)):
    run = Run.model_validate(data)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.get("/runs", response_model=list[RunRead])
def list_runs(
    is_active: Optional[bool] = None,
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    query = select(Run)
    if is_active is not None:
        query = query.where(Run.is_active == is_active)
    return session.exec(query.offset(offset).limit(limit)).all()


@app.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: int, session: Session = Depends(get_session)):
    return get_run_or_404(session, run_id)


@app.patch("/runs/{run_id}", response_model=RunRead)
def update_run(run_id: int, data: RunUpdate, session: Session = Depends(get_session)):
    run = get_run_or_404(session, run_id)
    run.sqlmodel_update(data.model_dump(exclude_unset=True))
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: int, session: Session = Depends(get_session)):
    run = get_run_or_404(session, run_id)
    session.delete(run)  # cascades to encounters
    session.commit()


# --- Encounter routes (nested under a run) ----------------------------------

@app.post(
    "/runs/{run_id}/encounters",
    response_model=EncounterRead,
    status_code=status.HTTP_201_CREATED,
)
def create_encounter(
    run_id: int, data: EncounterCreate, session: Session = Depends(get_session)
):
    get_run_or_404(session, run_id)
    ensure_area_is_free(session, run_id, data.area)

    in_party = resolve_party_flag(
        session,
        run_id,
        status=data.status,
        is_alive=True,
        wants_party=data.in_party,
        explicitly_requested=data.in_party,
    )
    enc = Encounter(
        **data.model_dump(exclude={"in_party", "area"}),
        area=data.area.strip(),
        run_id=run_id,
        in_party=in_party,
    )
    session.add(enc)
    session.commit()
    session.refresh(enc)
    return enc


@app.get("/runs/{run_id}/encounters", response_model=list[EncounterRead])
def list_encounters(
    run_id: int,
    status: Optional[EncounterStatus] = None,
    is_alive: Optional[bool] = None,
    in_party: Optional[bool] = None,
    session: Session = Depends(get_session),
):
    get_run_or_404(session, run_id)
    query = select(Encounter).where(Encounter.run_id == run_id)
    if status is not None:
        query = query.where(Encounter.status == status)
    if is_alive is not None:
        query = query.where(Encounter.is_alive == is_alive)
    if in_party is not None:
        query = query.where(Encounter.in_party == in_party)
    return session.exec(query).all()


@app.get("/runs/{run_id}/encounters/{encounter_id}", response_model=EncounterRead)
def get_encounter(
    run_id: int, encounter_id: int, session: Session = Depends(get_session)
):
    return get_encounter_or_404(session, run_id, encounter_id)


@app.patch("/runs/{run_id}/encounters/{encounter_id}", response_model=EncounterRead)
def update_encounter(
    run_id: int,
    encounter_id: int,
    data: EncounterUpdate,
    session: Session = Depends(get_session),
):
    enc = get_encounter_or_404(session, run_id, encounter_id)
    changes = data.model_dump(exclude_unset=True)

    if "area" in changes:
        changes["area"] = changes["area"].strip()
        ensure_area_is_free(session, run_id, changes["area"], exclude_id=enc.id)

    changes["in_party"] = resolve_party_flag(
        session,
        run_id,
        status=changes.get("status", enc.status),
        is_alive=changes.get("is_alive", enc.is_alive),
        wants_party=changes.get("in_party", enc.in_party),
        explicitly_requested=bool(changes.get("in_party")),
        currently_in_party=enc.in_party,
        exclude_id=enc.id,
    )

    enc.sqlmodel_update(changes)
    session.add(enc)
    session.commit()
    session.refresh(enc)
    return enc


@app.delete(
    "/runs/{run_id}/encounters/{encounter_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_encounter(
    run_id: int, encounter_id: int, session: Session = Depends(get_session)
):
    enc = get_encounter_or_404(session, run_id, encounter_id)
    session.delete(enc)
    session.commit()




# ---------------------
# --- UI --------------
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="static",
)
