from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from sqlmodel import Field, Session, SQLModel, create_engine, select

# --- Database setup ---------------------------------------------------------

engine = create_engine(
    "sqlite:///nuzlocke.db",
    connect_args={"check_same_thread": False},  # needed for SQLite + FastAPI
)


def get_session():
    """Dependency: one DB session per request, closed automatically."""
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)  # create tables on startup
    yield


# --- Models & schemas -------------------------------------------------------
# Pattern: one table model + separate schemas for create / read / update,
# so clients can never set fields like id or created_at themselves.


class RunBase(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    game: str = Field(min_length=1, max_length=100)


class Run(RunBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True


class RunCreate(RunBase):
    pass


class RunRead(RunBase):
    id: int
    created_at: datetime
    is_active: bool


class RunUpdate(SQLModel):
    # Every field optional, so PATCH can change just one thing
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    game: Optional[str] = Field(default=None, min_length=1, max_length=100)
    is_active: Optional[bool] = None


# --- App & routes -----------------------------------------------------------

app = FastAPI(title="Nuzlocke Helper", lifespan=lifespan)


@app.get("/")
def root():
    return {"status": "ok"}


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
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.patch("/runs/{run_id}", response_model=RunRead)
def update_run(run_id: int, data: RunUpdate, session: Session = Depends(get_session)):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # exclude_unset: only apply fields the client actually sent
    run.sqlmodel_update(data.model_dump(exclude_unset=True))
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: int, session: Session = Depends(get_session)):
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    session.delete(run)
    session.commit()
