"""Real isolated source/SQLite tests for explicit local trials. No models or network."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.db.models import User
from src.db.models.authoring_job import AuthoringAttempt, AuthoringJob
from src.db.models.script_review import ScriptAuditRecord
from src.db.session import get_db_session
from src.fusion.local_trial_publication import LocalTrialPublicationService, RuntimePublicationReader
from src.fusion.package_import import PackageImportService
from src.fusion.package_play import PackagePlayError, PackagePlayService
from src.fusion.package_runtime import PackageRuntimeError, PackageRuntimeService
from src.fusion.script_publication import PublicationError, ScriptPublicationService, LOCAL_TRIAL_POLICY_VERSION
from src.schemas.script_publication import ApprovePublicationRequest, PublishPackageRequest
from tests.fusion_security.test_script_review import review_db
from tests.fusion_security.test_package_investigation_publication import make_investigation_sources
from tests.fusion_security.test_package_play_store import action_body, ask_body


@pytest.fixture
def trial(review_db, tmp_path, monkeypatch):
    monkeypatch.setenv('ENABLE_LOCAL_TRIALS', 'true')
    monkeypatch.setenv('ENV', 'development')
    db, factory = review_db
    source = make_investigation_sources(tmp_path / 'trial-private')
    db.add(User(id=2, username='trial-player', email='trial@example.invalid', hashed_password='not-a-login', is_admin=False))
    db.commit()
    result = PackageImportService(db).submit(source.package, submitted_by=1, idempotency_key='trial-import')
    db.commit()
    return SimpleNamespace(db=db, factory=factory, sources=source.sources, bundle=source.bundle_hash, version=result['version_id'])


def approve(trial, key='trial-approve'):
    service = LocalTrialPublicationService(trial.db, trial.sources)
    gate = service.gate_state(trial.version, trial.sources, trial.bundle)
    body = ApprovePublicationRequest(idempotency_key=key, expected_package_hash=gate['candidate']['package_hash'],
        bundle_hash=trial.bundle, expected_basis_hash=gate['basis_hash'], model_dispositions=[],
        note='User explicitly authorizes a local trial; no model or formal human audit is claimed.')
    result = service.approve(trial.version, body, 1, trial.sources)
    trial.db.commit()
    return result, body


def release(trial):
    approval, body = approve(trial)
    request = PublishPackageRequest(idempotency_key='trial-publish', approval_id=approval['id'],
        expected_approval_hash=approval['approval_hash'], expected_basis_hash=body.expected_basis_hash)
    service = LocalTrialPublicationService(trial.db, trial.sources)
    result = service.publish(trial.version, request, 1, trial.sources)
    trial.db.commit()
    return result


def test_trial_is_distinct_and_never_creates_fake_audits(trial):
    local = LocalTrialPublicationService(trial.db, trial.sources)
    gate = local.gate_state(trial.version, trial.sources, trial.bundle)
    assert gate['can_approve'] and not gate['can_publish']
    assert not any(c['code'] in {'MODEL_AUDIT_COMPLETE', 'MANUAL_AUDIT_COMPLETE'} for c in gate['checks'])
    result = release(trial)
    assert result['publication_mode'] == 'LOCAL_TRIAL'
    approval = local._latest_approval(trial.version)
    assert approval['policy_version'] == LOCAL_TRIAL_POLICY_VERSION
    for model in (AuthoringJob, AuthoringAttempt, ScriptAuditRecord):
        assert trial.db.query(model).count() == 0
    formal = ScriptPublicationService(trial.db, trial.sources)
    assert formal.list_releases() == []
    assert not formal.gate_state(trial.version, trial.sources)['can_publish']
    with pytest.raises(PublicationError):
        formal.get_release(result['id'])


def test_local_scope_catalog_ownership_and_settlement(trial):
    released = release(trial)
    reader = RuntimePublicationReader(trial.db, trial.sources, allow_local_trials=True)
    runtime = PackageRuntimeService(trial.db, reader)
    catalog = runtime.list_releases(2)
    assert len(catalog) == 1 and catalog[0]['local_trial']['model_audit_complete'] is False
    assert 'SYSTEM_TRUTH_SENTINEL' not in str(catalog)
    opening = runtime.create({'release_id':released['id'],'character_id':'a','idempotency_key':'trial-opening'},2)
    assert opening['local_trial']['label'] == '本地试玩版'
    service = PackagePlayService(trial.db, reader)
    play = service.create({'opening_session_id':opening['session_id'],'idempotency_key':'trial-play'},2)
    assert play['local_trial']['ai_dialogue_available'] is False
    assert play['settlement'] is None
    for i, target in enumerate(['find-key','open-case']):
        play = service.act(play['play_id'],{'expected_revision':play['revision'],'idempotency_key':f'act-{i}','action':'PERFORM_ACTION','target':{'action_id':target}},2)
    play = service.act(play['play_id'],action_body(play['revision'],'advance','ADVANCE_PHASE'),2)
    play = service.act(play['play_id'],{'expected_revision':play['revision'],'idempotency_key':'last-action','action':'PERFORM_ACTION','target':{'action_id':'next-search'}},2)
    play = service.act(play['play_id'],action_body(play['revision'],'settle','SETTLE'),2)
    assert play['settled'] and service.get(play['play_id'],2)['settlement'] == play['settlement']
    with pytest.raises(PackagePlayError):
        service.get(play['play_id'],1)


@pytest.mark.parametrize('environment,enabled', [('production','true'),('prod','true'),('development','false')])
def test_trial_is_unavailable_when_disabled_or_in_production(trial, monkeypatch, environment, enabled):
    released = release(trial)
    monkeypatch.setenv('ENV',environment);monkeypatch.setenv('ENABLE_LOCAL_TRIALS',enabled)
    reader = RuntimePublicationReader(trial.db, trial.sources, allow_local_trials=True)
    assert reader.list_releases() == []
    with pytest.raises(PublicationError): reader.get_release(released['id'],require_current=False)
    with pytest.raises(PublicationError): LocalTrialPublicationService(trial.db,trial.sources).gate_state(trial.version,trial.sources,trial.bundle)


def test_remote_or_legacy_reader_cannot_open_local_trial(trial):
    released = release(trial)
    reader = RuntimePublicationReader(trial.db,trial.sources,allow_local_trials=False)
    assert reader.list_releases() == []
    with pytest.raises(PublicationError):reader.get_release(released['id'],require_current=False)
    with pytest.raises(PackageRuntimeError):
        PackageRuntimeService(trial.db,ScriptPublicationService(trial.db,trial.sources)).create(
            {'release_id':released['id'],'character_id':'a','idempotency_key':'legacy'},2)


def test_trial_approval_is_not_sufficient_to_use_formal_publication(trial):
    approval, body = approve(trial)
    request = PublishPackageRequest(idempotency_key='formal-attempt', approval_id=approval['id'],
        expected_approval_hash=approval['approval_hash'],expected_basis_hash=body.expected_basis_hash)
    with pytest.raises(PublicationError):
        ScriptPublicationService(trial.db,trial.sources).publish(trial.version,request,1,trial.sources)


def test_trial_cannot_be_approved_by_a_player(trial):
    service=LocalTrialPublicationService(trial.db,trial.sources)
    gate=service.gate_state(trial.version,trial.sources,trial.bundle)
    body=ApprovePublicationRequest(idempotency_key='player-attempt',expected_package_hash=gate['candidate']['package_hash'],
        bundle_hash=trial.bundle,expected_basis_hash=gate['basis_hash'],model_dispositions=[],note='invalid player attempt')
    with pytest.raises(PublicationError):service.approve(trial.version,body,2,trial.sources)


def test_trial_ai_requests_are_rejected_before_any_model_attempt(trial):
    released=release(trial);reader=RuntimePublicationReader(trial.db,trial.sources,allow_local_trials=True)
    opening=PackageRuntimeService(trial.db,reader).create({'release_id':released['id'],'character_id':'a','idempotency_key':'ai-opening'},2)
    service=PackagePlayService(trial.db,reader)
    play=service.create({'opening_session_id':opening['session_id'],'idempotency_key':'ai-play'},2)
    with pytest.raises(PackagePlayError, match='LOCAL_TRIAL_AI_UNAVAILABLE'):
        asyncio.run(service.ask(play['play_id'],ask_body(),2))
    assert service.get(play['play_id'],2)['revision'] == play['revision']


def test_trial_idempotency_preserves_the_release(trial):
    first=release(trial)
    assert release(trial) == first


def test_trial_http_rejects_remote_spoof_and_non_admin(trial):
    from src.api.routes.local_trial_routes import router
    from src.api.routes.source_bundle_routes import source_store
    app=FastAPI()
    @app.middleware('http')
    async def identity(request:Request,call_next):
        # Explicit test fixture. Production does not derive client IP from headers.
        request.scope['client']=(request.headers.get('test-client','127.0.0.1'),1)
        if request.headers.get('test-user'):
            request.state.current_user=SimpleNamespace(id=1,is_active=True,is_admin=request.headers['test-user']=='admin')
        return await call_next(request)
    app.include_router(router)
    app.dependency_overrides[get_db_session]=lambda:trial.db
    app.dependency_overrides[source_store]=lambda:trial.sources
    client=TestClient(app)
    path=f'/api/admin/fusion/script-packages/{trial.version}/local-trial?bundle_hash={trial.bundle}'
    assert client.get(path).status_code == 401
    assert client.get(path,headers={'test-user':'player'}).status_code == 403
    assert client.get(path,headers={'test-user':'admin','test-client':'198.51.100.7','x-forwarded-for':'127.0.0.1'}).status_code == 403
    assert client.get(path,headers={'test-user':'admin'}).status_code == 200
