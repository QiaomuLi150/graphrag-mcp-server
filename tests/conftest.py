"""Shared fixtures.

Live-Neo4j fixtures create a *disposable* ``gr-test-<id>`` database, seed it with the demo
dataset using admin credentials, and provision a read-only runtime user. The server under
test only ever receives the read-only credentials.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import jwt
import neo4j
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tools import seed_demo

from graphrag_mcp.config import Settings
from graphrag_mcp.main import create_app

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://issuer.test"
AUDIENCE = "graphrag-mcp-test"


def _admin_password() -> str | None:
    if pw := os.environ.get("GRAPHRAG_TEST_NEO4J_ADMIN_PASSWORD"):
        return pw
    f = ROOT / ".local" / "neo4j_admin_pw"
    return f.read_text().strip() if f.exists() else None


@dataclass(frozen=True)
class Keys:
    private_pem: bytes
    public_pem: str


@pytest.fixture(scope="session")
def keys() -> Keys:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return Keys(
        k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        k.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


@pytest.fixture(scope="session")
def other_keys() -> Keys:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return Keys(
        k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        k.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


@pytest.fixture(scope="session")
def mint(keys: Keys) -> Callable[..., str]:
    def _mint(*, key: Keys | None = None, expires_in: int | None = 300, **claims: object) -> str:
        payload: dict[str, object] = {"iss": ISSUER, "aud": AUDIENCE, "sub": "client-a", "scope": "graph:read"}
        if expires_in is not None:
            payload["exp"] = int(time.time()) + expires_in
        payload.update(claims)
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, (key or keys).private_pem, algorithm="RS256")

    return _mint


@dataclass(frozen=True)
class Neo4jTestDb:
    uri: str
    database: str
    reader_user: str
    reader_password: str
    admin_user: str
    admin_password: str
    revision: str
    edition: str = "enterprise"


@pytest.fixture(scope="session")
def neo4j_db() -> Iterator[Neo4jTestDb]:
    uri = os.environ.get("GRAPHRAG_TEST_NEO4J_URI", "bolt://127.0.0.1:27687")
    admin_user = os.environ.get("GRAPHRAG_TEST_NEO4J_ADMIN_USER", "neo4j")
    pw = _admin_password()
    if not pw:
        _unavailable("no GRAPHRAG_TEST_NEO4J_ADMIN_PASSWORD (and no .local/neo4j_admin_pw)")
    assert pw is not None
    try:
        driver = neo4j.GraphDatabase.driver(uri, auth=(admin_user, pw))
        driver.verify_connectivity()
    except Exception as e:  # noqa: BLE001
        _unavailable(f"cannot reach Neo4j at {uri}: {type(e).__name__}")
    suffix = uuid.uuid4().hex[:8]
    database, user = f"gr-test-{suffix}", f"gr_reader_{suffix}"
    reader_pw = uuid.uuid4().hex
    dataset = seed_demo.load_dataset(ROOT / "data" / "demo")
    try:
        driver.execute_query("SHOW ROLES", database_="system")
        enterprise = True
    except neo4j.exceptions.Neo4jError:
        enterprise = False
    if enterprise:
        role = "graphrag_ro_" + database.replace("-", "_")
        try:
            seed_demo.create_database(driver, database)
            report = seed_demo.seed(driver, database, dataset)
            role = seed_demo.provision_reader(driver, database, user, reader_pw)
            yield Neo4jTestDb(uri, database, user, reader_pw, admin_user, pw, report["graph_revision"], "enterprise")
        finally:
            seed_demo.drop_database(driver, database, user, role)
            driver.close()
    else:  # Community: single database; only use it if it is empty or already holds exactly our demo labels
        stray = driver.execute_query(
            "MATCH (n) WHERE none(l IN labels(n) WHERE l IN ['Entity','Chunk','Source','GraphMeta']) RETURN count(n) AS c",
            database_="neo4j",
        ).records[0]["c"]
        if stray:
            pytest.fail("refusing to use a Community database that contains non-demo data")
        try:
            report = seed_demo.seed(driver, "neo4j", dataset, prune=True)
            driver.execute_query(
                f"CREATE USER `{user}` IF NOT EXISTS SET PASSWORD $pw CHANGE NOT REQUIRED",
                {"pw": reader_pw},
                database_="system",
            )
            yield Neo4jTestDb(uri, "neo4j", user, reader_pw, admin_user, pw, report["graph_revision"], "community")
        finally:
            driver.execute_query(f"DROP USER `{user}` IF EXISTS", database_="system")
            driver.close()


def _unavailable(reason: str) -> None:
    if os.environ.get("GRAPHRAG_REQUIRE_NEO4J") == "1":
        pytest.fail(f"live Neo4j required but unavailable: {reason}")
    pytest.skip(f"live Neo4j not available: {reason}")


def make_settings(db: Neo4jTestDb, keys: Keys, **over: object) -> Settings:
    base: dict[str, object] = dict(
        neo4j_uri=db.uri,
        neo4j_user=db.reader_user,
        neo4j_password=db.reader_password,
        neo4j_database=db.database,
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key=keys.public_pem,
        environment="local",
        service_version="test",
        git_sha="test",
        graph_revision_ttl_s=0.2,
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def settings(neo4j_db: Neo4jTestDb, keys: Keys) -> Settings:
    return make_settings(neo4j_db, keys)


@dataclass(frozen=True)
class RunningServer:
    base_url: str
    mcp_url: str


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, settings: Settings) -> None:
        self.port = _free_port()
        cfg = uvicorn.Config(
            create_app(settings), host="127.0.0.1", port=self.port, log_level="warning", log_config=None
        )
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> RunningServer:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            raise RuntimeError("server did not start")
        base = f"http://127.0.0.1:{self.port}"
        return RunningServer(base, base + "/mcp")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


@pytest.fixture(scope="session")
def server(settings: Settings) -> Iterator[RunningServer]:
    s = _Server(settings)
    yield s.start()
    s.stop()


@pytest.fixture
def start_server() -> Iterator[Callable[[Settings], RunningServer]]:
    started: list[_Server] = []

    def _start(settings: Settings) -> RunningServer:
        s = _Server(settings)
        started.append(s)
        return s.start()

    yield _start
    for s in started:
        s.stop()
