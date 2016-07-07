#!/usr/bin/env python
# coding=utf-8
from __future__ import division, print_function, unicode_literals
import datetime
import json

import pytest
import tempfile
from sacred.dependencies import get_digest

from sacred.observers.sql import (SqlObserver, Host, Experiment, Run, Source,
                                  Resource)

T1 = datetime.datetime(1999, 5, 4, 3, 2, 1, 0)
T2 = datetime.datetime(1999, 5, 5, 5, 5, 5, 5)

sqlalchemy = pytest.importorskip("sqlalchemy")


@pytest.fixture(scope="module")
def engine(request):
    """Engine configuration."""
    url = request.config.getoption("--sqlalchemy-connect-url")
    from sqlalchemy.engine import create_engine
    engine = create_engine(url)

    def fin():
        engine.dispose()

    request.addfinalizer(fin)
    return engine


@pytest.fixture(scope="module")
def connection(request, engine):
    connection = engine.connect()

    def fin():
        connection.close()

    request.addfinalizer(fin)
    return connection


@pytest.fixture()
def transaction(request, connection):
    """Will start a transaction on the connection. The connection will
    be rolled back after it leaves its scope."""
    transaction = connection.begin()

    def fin():
        transaction.rollback()

    request.addfinalizer(fin)
    return connection


@pytest.fixture()
def session(transaction):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker()(bind=transaction)


@pytest.fixture
def sql_obs(session, engine):
    return SqlObserver(engine, session)


@pytest.fixture()
def sample_run():
    exp = {'name': 'test_exp', 'sources': [], 'dependencies': [],
           'base_dir': '/tmp'}
    host = {'hostname': 'test_host', 'cpu': 'Intel', 'os': ['Linux', 'Ubuntu'],
            'python_version': '3.4'}
    config = {'config': 'True', 'foo': 'bar', 'answer': 42}
    command = 'run'
    meta_info = {'comment': 'test run'}
    return {
        '_id': 'FEDCBA9876543210',
        'ex_info': exp,
        'command': command,
        'host_info': host,
        'start_time': T1,
        'config': config,
        'meta_info': meta_info,
    }


def test_sql_observer_started_event_creates_run(sql_obs, sample_run, session):
    sample_run['_id'] = None
    _id = sql_obs.started_event(**sample_run)
    assert _id is not None
    assert session.query(Run).count() == 1
    assert session.query(Host).count() == 1
    assert session.query(Experiment).count() == 1
    run = session.query(Run).first()
    assert run.to_json() == {
            '_id': _id,
            'command': sample_run['command'],
            'start_time': sample_run['start_time'],
            'heartbeat': None,
            'stop_time': None,
            'queue_time': None,
            'status': 'RUNNING',
            'result': None,
            'meta': {
                'comment': sample_run['meta_info']['comment'],
                'priority': 0.0},
            'resources': [],
            'artifacts': [],
            'host': sample_run['host_info'],
            'experiment': sample_run['ex_info'],
            'config': sample_run['config'],
            'captured_out': None,
            'fail_trace': None,
        }


def test_sql_observer_started_event_uses_given_id(sql_obs, sample_run, session):
    _id = sql_obs.started_event(**sample_run)
    assert _id == sample_run['_id']
    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()
    assert db_run.id == sample_run['_id']


def test_fs_observer_started_event_saves_source(sql_obs, sample_run, session):
    with tempfile.NamedTemporaryFile(suffix='.py') as f:
        f.write(b'import sacred\n')
        f.flush()
        md5sum = get_digest(f.name)
        sample_run['ex_info']['sources'] = [[f.name, md5sum]]

        sql_obs.started_event(**sample_run)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()
    assert session.query(Source).count() == 1
    assert len(db_run.experiment.sources) == 1
    source = db_run.experiment.sources[0]
    assert source.filename == f.name
    assert source.content == 'import sacred\n'
    assert source.md5sum == md5sum


def test_sql_observer_heartbeat_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)

    info = {'my_info': [1, 2, 3], 'nr': 7}
    outp = 'some output'
    with tempfile.NamedTemporaryFile() as f:
        f.write(outp.encode())
        f.flush()
        sql_obs.heartbeat_event(info=info, cout_filename=f.name,
                                beat_time=T2)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()
    assert db_run.heartbeat == T2
    assert json.loads(db_run.info) == info
    assert db_run.captured_out == outp


def test_sql_observer_completed_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)
    sql_obs.completed_event(stop_time=T2, result=42)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert db_run.stop_time == T2
    assert db_run.result == 42
    assert db_run.status == 'COMPLETED'


def test_sql_observer_interrupted_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)
    sql_obs.interrupted_event(interrupt_time=T2, status='INTERRUPTED')

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert db_run.stop_time == T2
    assert db_run.status == 'INTERRUPTED'


def test_sql_observer_failed_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)
    fail_trace = ["lots of errors and", "so", "on..."]
    sql_obs.failed_event(fail_time=T2, fail_trace=fail_trace)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert db_run.stop_time == T2
    assert db_run.status == 'FAILED'
    assert db_run.fail_trace == "lots of errors and\nso\non..."


def test_sql_observer_artifact_event(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)

    with tempfile.NamedTemporaryFile(suffix='.py') as f:
        f.write(b'foo\nbar')
        f.flush()
        sql_obs.artifact_event('my_artifact.py', f.name)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert len(db_run.artifacts) == 1
    artifact = db_run.artifacts[0]

    assert artifact.filename == 'my_artifact.py'
    assert artifact.content == b'foo\nbar'


def test_fs_observer_resource_event(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)

    with tempfile.NamedTemporaryFile(suffix='.py') as f:
        f.write(b'foo\nbar')
        f.flush()
        sql_obs.resource_event(f.name)
        md5sum = get_digest(f.name)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert len(db_run.resources) == 1
    res = db_run.resources[0]
    assert res.filename == f.name
    assert res.md5sum == md5sum
    assert res.content == b'foo\nbar'


def test_fs_observer_doesnt_duplicate_resources_or_sources(sql_obs, sample_run,
                                                           session, engine):

    sql_obs2 = SqlObserver(engine, session)

    sample_run['_id'] = None

    with tempfile.NamedTemporaryFile(suffix='.py') as f:
        f.write(b'import sacred\n')
        f.flush()
        f.seek(0)
        md5sum = get_digest(f.name)
        sample_run['ex_info']['sources'] = [[f.name, md5sum]]

        sql_obs.started_event(**sample_run)
        sql_obs2.started_event(**sample_run)

    with tempfile.NamedTemporaryFile(suffix='.py') as f:
        f.write(b'foo\nbar')
        f.flush()
        sql_obs.resource_event(f.name)
        sql_obs2.resource_event(f.name)

    assert session.query(Run).count() == 2
    assert session.query(Resource).count() == 1
    assert session.query(Source).count() == 1


def test_sql_observer_equality(sql_obs, engine, session):
    sql_obs2 = SqlObserver(engine, session)
    assert sql_obs == sql_obs2

    assert not sql_obs != sql_obs2

    assert not sql_obs == 'foo'
    assert sql_obs != 'foo'
