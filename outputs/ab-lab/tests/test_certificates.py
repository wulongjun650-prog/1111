"""Real test-only CA and signatures; never contact an external CA."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ablab.provisioning import ProvisioningError
from test_panel_sites import IDENTITY

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
PEM = serialization.Encoding.PEM


def key_pem(key):
    return key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def make_cert(name, key, *, issuer=None, issuer_key=None, ca=False, sans=None,
              start=None, end=None, eku=None):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signer = issuer_key or key
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer.subject if issuer else subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(start or NOW - timedelta(days=1))
               .not_valid_after(end or NOW + timedelta(days=90))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                           key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                           encipher_only=None, decipher_only=None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), False))
    if not ca:
        builder = builder.add_extension(x509.ExtendedKeyUsage([eku or ExtendedKeyUsageOID.SERVER_AUTH]), False)
    if sans is not None:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), False)
    return builder.sign(signer, hashes.SHA256())


@pytest.fixture
def material():
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = make_cert('Test Root', root_key, ca=True)
    stage_key = ec.generate_private_key(ec.SECP256R1())
    stage = make_cert('Staging Test Root', stage_key, ca=True)
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    intermediate = make_cert('Intermediate', intermediate_key, issuer=root, issuer_key=root_key, ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())

    def leaf(**overrides):
        params = dict(issuer=intermediate, issuer_key=intermediate_key,
                      sans=[x509.DNSName('new.example.com')])
        params.update(overrides)
        return make_cert('new.example.com', leaf_key, **params)

    return dict(root=root, stage=stage, root_key=root_key, stage_key=stage_key,
                intermediate=intermediate, intermediate_key=intermediate_key,
                key=leaf_key, leaf=leaf)


def verifier(material):
    from ablab.certificates import CertificateVerifier
    return CertificateVerifier(material['root'].public_bytes(PEM), material['stage'].public_bytes(PEM))


def test_real_chain_key_and_metadata_are_verified_without_exposing_secrets(material):
    leaf = material['leaf']()
    chain = leaf.public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    result = verifier(material).verify(IDENTITY, 'production', chain, key_pem(material['key']), now=NOW)
    assert result.domain == 'new.example.com'
    assert result.environment == 'production'
    assert result.fingerprint == leaf.fingerprint(hashes.SHA256()).hex()
    assert result.not_after == NOW + timedelta(days=90)
    assert result.fullchain == chain
    assert result.private_key == key_pem(material['key'])
    assert 'PRIVATE KEY' not in repr(result)
    assert 'CERTIFICATE' not in repr(result)
    assert result.require_production() is None


@pytest.mark.parametrize('changes', [
    {'sans': None}, {'sans': [x509.DNSName('wrong.example.com')]},
    {'sans': [x509.DNSName('new.example.com'), x509.DNSName('other.example.com')]},
    {'sans': [x509.DNSName('*.example.com')]},
    {'sans': [x509.DNSName('new.example.com'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]},
    {'start': NOW - timedelta(days=90), 'end': NOW - timedelta(seconds=1)},
    {'start': NOW + timedelta(seconds=1)},
    {'eku': ExtendedKeyUsageOID.CLIENT_AUTH},
])
def test_invalid_san_dates_and_tls_usage_are_rejected(material, changes):
    chain = material['leaf'](**changes).public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    with pytest.raises(ProvisioningError):
        verifier(material).verify(IDENTITY, 'production', chain, key_pem(material['key']), now=NOW)


@pytest.mark.parametrize('defect', ['wrong-key', 'missing-intermediate', 'wrong-signature', 'extra-cert', 'reversed'])
def test_chain_and_private_key_fail_closed(material, defect):
    leaf = material['leaf']()
    chain = leaf.public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    private = key_pem(material['key'])
    if defect == 'wrong-key':
        private = key_pem(material['root_key'])
    elif defect == 'missing-intermediate':
        chain = leaf.public_bytes(PEM)
    elif defect == 'wrong-signature':
        chain = material['leaf'](issuer_key=material['stage_key']).public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    elif defect == 'extra-cert':
        chain += material['stage'].public_bytes(PEM)
    else:
        chain = material['intermediate'].public_bytes(PEM) + leaf.public_bytes(PEM)
    with pytest.raises(ProvisioningError):
        verifier(material).verify(IDENTITY, 'production', chain, private, now=NOW)


def test_staging_chain_cannot_validate_or_deploy_as_production(material):
    stage_leaf = material['leaf'](issuer=material['stage'], issuer_key=material['stage_key'])
    chain = stage_leaf.public_bytes(PEM)
    verify = verifier(material)
    with pytest.raises(ProvisioningError):
        verify.verify(IDENTITY, 'production', chain, key_pem(material['key']), now=NOW)
    stage = verify.verify(IDENTITY, 'staging', chain, key_pem(material['key']), now=NOW)
    with pytest.raises(ProvisioningError):
        stage.require_production()


@pytest.mark.parametrize('defect', ['empty-chain', 'junk-chain', 'oversize-chain', 'junk-key', 'oversize-key', 'encrypted-key'])
def test_bad_material_is_rejected_with_redacted_errors(material, defect):
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    private = key_pem(material['key'])
    if defect == 'empty-chain':
        chain = b''
    elif defect == 'junk-chain':
        chain += b'secret-marker'
    elif defect == 'oversize-chain':
        chain = b'secret-marker' * 30000
    elif defect == 'junk-key':
        private = b'secret-marker'
    elif defect == 'oversize-key':
        private = b'secret-marker' * 2000
    else:
        private = material['key'].private_bytes(PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.BestAvailableEncryption(b'secret-marker'))
    with pytest.raises(ProvisioningError) as error:
        verifier(material).verify(IDENTITY, 'production', chain, private, now=NOW)
    assert 'secret-marker' not in str(error.value)
    assert error.value.__suppress_context__


def test_root_stores_must_be_ca_certificates_and_environment_disjoint(material):
    from ablab.certificates import CertificateVerifier
    root = material['root'].public_bytes(PEM)
    for first, second in [(root, root), (b'', root), (material['leaf']().public_bytes(PEM), root)]:
        with pytest.raises(ProvisioningError):
            CertificateVerifier(first, second)


def test_unknown_environment_and_naive_time_are_rejected(material):
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    for environment, now in [('other', NOW), ('production', NOW.replace(tzinfo=None))]:
        with pytest.raises(ValueError):
            verifier(material).verify(IDENTITY, environment, chain, key_pem(material['key']), now=now)


def certbot_tree(tmp_path, material):
    base = tmp_path / 'certificates'
    base.mkdir(mode=0o700)
    name = 'ab-' + IDENTITY['site_id']
    config = base / 'production' / 'config'
    live = config / 'live' / name
    archive = config / 'archive' / name
    live.mkdir(parents=True)
    archive.mkdir(parents=True)
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    key = key_pem(material['key'])
    for kind, content in [('fullchain', chain), ('privkey', key)]:
        target = archive / (kind + '1.pem')
        target.write_bytes(content)
        target.chmod(0o600)
        try:
            (live / (kind + '.pem')).symlink_to(Path('../../archive') / name / target.name)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                pytest.skip('Windows symlink privilege unavailable; Linux acceptance still required')
            raise
    return base, live, archive, chain, key


def test_read_same_generation_then_verify_the_exact_bytes(tmp_path, material):
    from ablab.certificates import read_certbot_material
    base, _, _, chain, key = certbot_tree(tmp_path, material)
    actual = read_certbot_material(base, 'production', IDENTITY)
    assert actual == (chain, key)
    assert verifier(material).verify(IDENTITY, 'production', *actual, now=NOW).domain == 'new.example.com'


@pytest.mark.parametrize('defect', ['foreign-site', 'foreign-environment', 'outside', 'mixed-generation',
                                     'target-link', 'plain-live', 'missing', 'directory', 'hard-link',
                                     'oversize-chain', 'oversize-key', 'empty-key'])
def test_unsafe_certbot_files_rejected_without_modifying_material(tmp_path, material, defect):
    from ablab.certificates import read_certbot_material
    base, live, archive, chain, key = certbot_tree(tmp_path, material)
    link = live / 'privkey.pem'
    target = archive / 'privkey1.pem'
    if defect in ('foreign-site', 'foreign-environment', 'outside'):
        if defect == 'foreign-site':
            foreign = archive.parent / ('ab-' + 'f' * 32) / 'privkey1.pem'
        elif defect == 'foreign-environment':
            foreign = base / 'staging' / 'config' / 'archive' / archive.name / 'privkey1.pem'
        else:
            foreign = tmp_path / 'privkey1.pem'
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_bytes(key)
        foreign.chmod(0o600)
        link.unlink()
        link.symlink_to(foreign)
    elif defect == 'mixed-generation':
        (archive / 'privkey2.pem').write_bytes(key)
        (archive / 'privkey2.pem').chmod(0o600)
        link.unlink()
        link.symlink_to(archive / 'privkey2.pem')
    elif defect == 'target-link':
        target.rename(archive / 'privkey2.pem')
        target.symlink_to(archive / 'privkey2.pem')
    elif defect == 'plain-live':
        link.unlink()
        link.write_bytes(key)
    elif defect == 'missing':
        target.unlink()
    elif defect == 'directory':
        target.unlink()
        target.mkdir()
    elif defect == 'hard-link':
        os.link(target, archive / 'copied.pem')
    elif defect == 'oversize-chain':
        (archive / 'fullchain1.pem').write_bytes(b'x' * (256 * 1024 + 1))
    elif defect == 'oversize-key':
        target.write_bytes(b'x' * (16 * 1024 + 1))
    else:
        target.write_bytes(b'')
    with pytest.raises(ProvisioningError):
        read_certbot_material(base, 'production', IDENTITY)
    assert (archive / 'fullchain1.pem').exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission enforcement requires Linux')
@pytest.mark.parametrize('part,mode', [('base', 0o755), ('key', 0o644), ('archive', 0o777)])
def test_public_or_writable_certificate_storage_rejected(tmp_path, material, part, mode):
    from ablab.certificates import read_certbot_material
    base, _, archive, _, _ = certbot_tree(tmp_path, material)
    target = {'base': base, 'key': archive / 'privkey1.pem', 'archive': archive}[part]
    target.chmod(mode)
    with pytest.raises(ProvisioningError):
        read_certbot_material(base, 'production', IDENTITY)


def test_symlink_rotation_during_read_is_not_returned_as_a_snapshot(tmp_path, material, monkeypatch):
    from ablab.certificates import read_certbot_material
    base, live, archive, _, key = certbot_tree(tmp_path, material)
    (archive / 'privkey2.pem').write_bytes(key)
    (archive / 'privkey2.pem').chmod(0o600)
    original = os.readlink
    calls = []

    def rotate(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if Path(path) == live / 'privkey.pem' and not calls:
            calls.append('rotated')
            Path(path).unlink()
            Path(path).symlink_to(archive / 'privkey2.pem')
        return value

    monkeypatch.setattr(os, 'readlink', rotate)
    with pytest.raises(ProvisioningError):
        read_certbot_material(base, 'production', IDENTITY)
    assert calls == ['rotated']


def test_exclusive_published_file_can_be_read_with_stable_metadata(tmp_path):
    from ablab.certificates import _read_archive
    from ablab.nginx_entry import exclusive_text
    path = tmp_path / 'material.pem'
    exclusive_text(path, 'bounded-test-material\n')
    assert _read_archive(path, 128, private=True) == b'bounded-test-material\n'


@pytest.mark.parametrize('changed_api', ['descriptor', 'path'])
def test_ctime_only_change_during_read_is_still_rejected(tmp_path, monkeypatch, changed_api):
    from types import SimpleNamespace
    from ablab.certificates import _read_archive
    from ablab.nginx_entry import exclusive_text
    path = tmp_path / 'material.pem'
    exclusive_text(path, 'bounded-test-material\n')
    original_fd, original_path = os.fstat, Path.lstat
    opened = []

    def changed(info):
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink', 'st_uid')
        value = SimpleNamespace(**{name: getattr(info, name) for name in fields})
        value.st_ctime_ns += 1
        return value

    def fd_stat(fd):
        info = original_fd(fd)
        opened.append(True)
        return changed(info) if changed_api == 'descriptor' and len(opened) > 1 else info

    def path_stat(target, *args, **kwargs):
        info = original_path(target, *args, **kwargs)
        return changed(info) if changed_api == 'path' and opened and target == path else info

    monkeypatch.setattr(os, 'fstat', fd_stat)
    monkeypatch.setattr(Path, 'lstat', path_stat)
    with pytest.raises(ValueError, match='changed while reading'):
        _read_archive(path, 128, private=True)
