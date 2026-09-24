from fastapi.testclient import TestClient

from ablab.settings import Deployment
from ablab.sites import Registry
from ablab.web import create_target


def setup_target(tmp_path):
    deployment = Deployment(admin_origin='https://admin.lab.example',
                            target_origin='https://origin.lab.example')
    registry = Registry(tmp_path, deployment.host(False))
    site = registry.add('new.example.com')
    target = TestClient(create_target(tmp_path, deployment=deployment),
                        base_url='https://new.example.com', client=('127.0.0.1', 1))
    target.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https'})
    return registry, site, target


def test_registered_host_has_application_owned_route_proof(tmp_path):
    _, site, target = setup_target(tmp_path)
    response = target.get('/.well-known/ab-lab-route/' + site['id'])
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/json'
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'schema': 1, 'site_id': site['id'], 'domain': site['domain']}


def test_route_proof_cannot_be_served_for_another_site_or_method(tmp_path):
    _, site, target = setup_target(tmp_path)
    path = '/.well-known/ab-lab-route/' + ('b' * 32)
    assert target.get(path).status_code == 404
    assert target.head('/.well-known/ab-lab-route/' + site['id']).status_code == 404


def test_unregistered_or_paused_host_has_no_route_proof(tmp_path):
    registry, site, target = setup_target(tmp_path)
    path = '/.well-known/ab-lab-route/' + site['id']
    assert target.get(path, headers={'Host': 'unknown.example.com'}).status_code == 403
    registry.control(site['id'], 'pause')
    assert target.get(path).status_code == 403
