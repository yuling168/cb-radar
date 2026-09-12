from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import tpex_tls
from tpex_tls import (
    TpexCABootstrapError,
    TpexCABundle,
    TpexHTTPSession,
    create_tpex_ca_bundle,
    get_tpex_full_response,
    validate_twca_intermediate,
)


def _name(common_name: str, organizational_unit: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TAIWAN-CA"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])


def _certificate(
    *,
    subject: x509.Name | None = None,
    issuer: x509.Name | None = None,
    is_ca: bool = True,
    key_cert_sign: bool = True,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> x509.Certificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject or _name("TWCA SSL Certification Authority", "SSL Sub-CA"))
        .issuer_name(issuer or _name("TWCA CYBER Root CA", "Root CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(days=1))
        .not_valid_after(not_after or now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=0 if is_ca else None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=key_cert_sign,
                crl_sign=key_cert_sign, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )


def test_validates_expected_intermediate_metadata():
    validate_twca_intermediate(_certificate())


@pytest.mark.parametrize(("certificate", "message"), [
    (_certificate(subject=_name("wrong", "SSL Sub-CA")), "subject"),
    (_certificate(issuer=_name("wrong", "Root CA")), "issuer"),
    (_certificate(is_ca=False), "not a CA"),
    (_certificate(key_cert_sign=False), "cannot sign"),
    (_certificate(not_before=datetime.now(timezone.utc) - timedelta(days=3), not_after=datetime.now(timezone.utc) - timedelta(days=2)), "not currently valid"),
])
def test_rejects_wrong_or_invalid_intermediate(certificate, message):
    with pytest.raises(TpexCABootstrapError, match=message):
        validate_twca_intermediate(certificate)


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


def _bootstrap_inputs(monkeypatch, certificate: x509.Certificate, tmp_path: Path):
    der = certificate.public_bytes(tpex_tls.Encoding.DER)
    monkeypatch.setattr(tpex_tls, "TWCA_INTERMEDIATE_SHA256", hashlib.sha256(der).hexdigest().upper())
    certifi_bundle = tmp_path / "certifi.pem"
    certifi_bundle.write_text("CERTIFI-ROOT\n", encoding="ascii")
    verified = []
    monkeypatch.setattr(tpex_tls, "_verify_intermediate_chain", lambda intermediate, root, **_kwargs: verified.append((intermediate, root)))
    return der, certifi_bundle, verified


def test_creates_temporary_bundle_from_verified_intermediate(monkeypatch, tmp_path):
    der, certifi_bundle, verified = _bootstrap_inputs(monkeypatch, _certificate(), tmp_path)
    bundle = create_tpex_ca_bundle(
        downloader=lambda url, **kwargs: _Response(der),
        sleep=lambda _seconds: None,
        certifi_bundle=certifi_bundle,
    )
    try:
        assert bundle.directory.parent != Path.cwd()
        assert bundle.bundle_path.read_bytes().startswith(b"CERTIFI-ROOT")
        assert b"BEGIN CERTIFICATE" in bundle.bundle_path.read_bytes()
        assert verified == [(bundle.intermediate_path, certifi_bundle)]
    finally:
        bundle.cleanup()


def test_fingerprint_mismatch_fails_closed(monkeypatch, tmp_path):
    der, certifi_bundle, _ = _bootstrap_inputs(monkeypatch, _certificate(), tmp_path)
    monkeypatch.setattr(tpex_tls, "TWCA_INTERMEDIATE_SHA256", "0" * 64)
    with pytest.raises(TpexCABootstrapError, match="fingerprint"):
        create_tpex_ca_bundle(
            downloader=lambda url, **kwargs: _Response(der), sleep=lambda _seconds: None,
            certifi_bundle=certifi_bundle,
        )


def test_bootstrap_https_failure_retries_then_hard_fails(monkeypatch):
    calls = []
    sleeps = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise requests.exceptions.SSLError("official HTTPS validation failed")

    with pytest.raises(TpexCABootstrapError, match="download failed"):
        create_tpex_ca_bundle(downloader=fail, sleep=sleeps.append)
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_invalid_chain_fails_closed(monkeypatch, tmp_path):
    der, certifi_bundle, _ = _bootstrap_inputs(monkeypatch, _certificate(), tmp_path)
    monkeypatch.setattr(
        tpex_tls,
        "_verify_intermediate_chain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TpexCABootstrapError("untrusted chain")),
    )
    with pytest.raises(TpexCABootstrapError, match="untrusted chain"):
        create_tpex_ca_bundle(
            downloader=lambda url, **kwargs: _Response(der), sleep=lambda _seconds: None,
            certifi_bundle=certifi_bundle,
        )


def test_certifi_bundle_is_never_modified(monkeypatch, tmp_path):
    der, certifi_bundle, _ = _bootstrap_inputs(monkeypatch, _certificate(), tmp_path)
    before = hashlib.sha256(certifi_bundle.read_bytes()).hexdigest()
    bundle = create_tpex_ca_bundle(
        downloader=lambda url, **kwargs: _Response(der), sleep=lambda _seconds: None,
        certifi_bundle=certifi_bundle,
    )
    try:
        assert hashlib.sha256(certifi_bundle.read_bytes()).hexdigest() == before
    finally:
        bundle.cleanup()


def test_session_applies_bundle_only_to_tpex_https(monkeypatch, tmp_path):
    bundle = TpexCABundle(tmp_path, tmp_path / "bundle.pem", tmp_path / "intermediate.pem")
    bundle.bundle_path.write_text("bundle", encoding="ascii")
    calls = []

    def fake_request(self, method, url, *args, **kwargs):
        calls.append((method, url, kwargs))
        return object()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session = TpexHTTPSession(bundle)
    session.get("https://www.tpex.org.tw/www/zh-tw/bond/cbDaily")
    session.get("https://openapi.twse.com.tw/v1/opendata/t187ap04_L")
    assert calls[0][2]["verify"] == str(bundle.bundle_path)
    assert "verify" not in calls[1][2]
    with pytest.raises(TpexCABootstrapError, match="may not disable"):
        session.get("https://www.tpex.org.tw/test", verify=False)


def test_expected_official_source_is_https_only():
    assert tpex_tls.TWCA_INTERMEDIATE_URL == "https://sslserver.twca.com.tw/cacert/Cyber_SSL_2023.crt"
    assert "http://" not in tpex_tls.TWCA_INTERMEDIATE_URL


def test_production_helper_contains_no_insecure_tls_bypass():
    source = Path(tpex_tls.__file__).read_text(encoding="utf-8")
    assert "verify=False" not in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source


class _FullBodyResponse:
    def __init__(self, content: bytes | Exception = b"ok", *, status_code: int = 200):
        self._content = content
        self.status_code = status_code
        self.closed = False

    @property
    def content(self):
        if isinstance(self._content, Exception):
            raise self._content
        return self._content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class _FullBodySession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _tpex_url() -> str:
    return "https://www.tpex.org.tw/openapi/v1/bond_ISSBD5_data"


def test_full_body_retry_reissues_get_after_chunked_body_failure():
    first = _FullBodyResponse(requests.exceptions.ChunkedEncodingError("truncated"))
    session = _FullBodySession([first, _FullBodyResponse(b"complete")])
    sleeps = []

    result = get_tpex_full_response(session, _tpex_url(), sleep=sleeps.append)

    assert result.content == b"complete"
    assert first.closed
    assert len(session.calls) == 2
    assert all(call[1]["stream"] is True for call in session.calls)
    assert sleeps == [1]


def test_full_body_retry_succeeds_on_third_transport_attempt():
    session = _FullBodySession([
        _FullBodyResponse(requests.exceptions.ChunkedEncodingError("one")),
        _FullBodyResponse(requests.exceptions.ChunkedEncodingError("two")),
        _FullBodyResponse(b"complete"),
    ])
    sleeps = []

    assert get_tpex_full_response(session, _tpex_url(), sleep=sleeps.append).content == b"complete"
    assert len(session.calls) == 3
    assert sleeps == [1, 2]


@pytest.mark.parametrize("error", [
    requests.exceptions.ChunkedEncodingError("truncated"),
    requests.exceptions.ConnectionError("connection"),
    requests.exceptions.Timeout("timeout"),
])
def test_full_body_retry_hard_fails_only_after_three_transport_attempts(error):
    session = _FullBodySession([_FullBodyResponse(error) for _ in range(3)])
    sleeps = []

    with pytest.raises(type(error)):
        get_tpex_full_response(session, _tpex_url(), sleep=sleeps.append)
    assert len(session.calls) == 3
    assert sleeps == [1, 2]


def test_full_body_retry_never_retries_http_or_json_application_failures():
    session = _FullBodySession([_FullBodyResponse(b"not json")])
    response = get_tpex_full_response(session, _tpex_url(), sleep=lambda _: pytest.fail("sleep"))
    with pytest.raises(ValueError):
        __import__("json").loads(response.content)
    assert len(session.calls) == 1

    status_session = _FullBodySession([_FullBodyResponse(b"error", status_code=503)])
    with pytest.raises(requests.HTTPError):
        get_tpex_full_response(status_session, _tpex_url())
    assert len(status_session.calls) == 1


def test_full_body_retry_rejects_non_tpex_without_requesting_it():
    session = _FullBodySession([_FullBodyResponse(b"unexpected")])
    with pytest.raises(ValueError, match="only www.tpex.org.tw"):
        get_tpex_full_response(session, "https://openapi.twse.com.tw/v1/test")
    assert session.calls == []


def test_full_body_retry_preserves_tpex_bundle_on_every_attempt(monkeypatch, tmp_path):
    bundle = TpexCABundle(tmp_path, tmp_path / "bundle.pem", tmp_path / "intermediate.pem")
    bundle.bundle_path.write_text("bundle", encoding="ascii")
    responses = [
        _FullBodyResponse(requests.exceptions.ChunkedEncodingError("truncated")),
        _FullBodyResponse(b"complete"),
    ]
    verifies = []

    def fake_request(self, method, url, *args, **kwargs):
        verifies.append(kwargs["verify"])
        return responses.pop(0)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session = TpexHTTPSession(bundle)
    assert get_tpex_full_response(session, _tpex_url(), sleep=lambda _: None).content == b"complete"
    assert verifies == [str(bundle.bundle_path), str(bundle.bundle_path)]
