"""Build a narrowly scoped, verified TLS trust bundle for TPEx requests.

TPEx currently omits its TWCA intermediate from the server certificate chain on
some GitHub-hosted runners.  This module never disables TLS verification: it
downloads one pinned, official intermediate through ordinary verified HTTPS,
validates it, proves it chains to certifi's trusted root, and applies the
resulting temporary bundle only to ``www.tpex.org.tw`` requests.
"""

from __future__ import annotations

import atexit
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import certifi
import requests
from requests.adapters import HTTPAdapter
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID


TWCA_INTERMEDIATE_URL = "https://sslserver.twca.com.tw/cacert/Cyber_SSL_2023.crt"
TWCA_INTERMEDIATE_SHA256 = "01AF2324D098098F5E0CDF6FAABADA430B21CCE777F47EACB26248B2FDA3E531"
TPEX_HOST = "www.tpex.org.tw"
BOOTSTRAP_MAX_ATTEMPTS = 3
BOOTSTRAP_TIMEOUT_SECONDS = 30
TPEx_GET_MAX_ATTEMPTS = 3
TPEx_GET_TRANSIENT_EXCEPTIONS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

_EXPECTED_SUBJECT = {
    NameOID.COMMON_NAME: "TWCA SSL Certification Authority",
    NameOID.ORGANIZATIONAL_UNIT_NAME: "SSL Sub-CA",
    NameOID.ORGANIZATION_NAME: "TAIWAN-CA",
    NameOID.COUNTRY_NAME: "TW",
}
_EXPECTED_ISSUER = {
    NameOID.COMMON_NAME: "TWCA CYBER Root CA",
    NameOID.ORGANIZATIONAL_UNIT_NAME: "Root CA",
    NameOID.ORGANIZATION_NAME: "TAIWAN-CA",
    NameOID.COUNTRY_NAME: "TW",
}


class TpexCABootstrapError(RuntimeError):
    """The pinned TPEx intermediate cannot be safely used."""


@dataclass
class TpexCABundle:
    """A temporary CA bundle and its private temporary workspace."""

    directory: Path
    bundle_path: Path
    intermediate_path: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def _name_matches(name: x509.Name, expected: dict[NameOID, str]) -> bool:
    values = {attribute.oid: attribute.value for attribute in name}
    return len(values) == len(expected) and values == expected


def _validity_boundary(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def validate_twca_intermediate(certificate: x509.Certificate, *, now: datetime | None = None) -> None:
    """Fail closed unless this is the expected, currently valid CA certificate."""
    if not _name_matches(certificate.subject, _EXPECTED_SUBJECT):
        raise TpexCABootstrapError("TWCA intermediate subject does not match the pinned identity")
    if not _name_matches(certificate.issuer, _EXPECTED_ISSUER):
        raise TpexCABootstrapError("TWCA intermediate issuer does not match the pinned identity")
    try:
        basic_constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise TpexCABootstrapError("TWCA intermediate is missing required CA extensions") from exc
    if not basic_constraints.ca:
        raise TpexCABootstrapError("TWCA intermediate is not a CA certificate")
    if not key_usage.key_cert_sign:
        raise TpexCABootstrapError("TWCA intermediate cannot sign certificates")
    current = now or datetime.now(timezone.utc)
    not_before_value = (
        certificate.not_valid_before_utc
        if hasattr(certificate, "not_valid_before_utc")
        else certificate.not_valid_before
    )
    not_after_value = (
        certificate.not_valid_after_utc
        if hasattr(certificate, "not_valid_after_utc")
        else certificate.not_valid_after
    )
    not_before = _validity_boundary(not_before_value)
    not_after = _validity_boundary(not_after_value)
    if current < not_before or current > not_after:
        raise TpexCABootstrapError("TWCA intermediate is not currently valid")


def _download_intermediate(
    downloader: Callable[..., requests.Response], sleep: Callable[[float], None]
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, BOOTSTRAP_MAX_ATTEMPTS + 1):
        try:
            # Deliberately use requests' normal, verified HTTPS trust path here.
            response = downloader(TWCA_INTERMEDIATE_URL, timeout=BOOTSTRAP_TIMEOUT_SECONDS)
            response.raise_for_status()
            if not response.content:
                raise TpexCABootstrapError("Official TWCA intermediate download was empty")
            return response.content
        except (requests.RequestException, TpexCABootstrapError) as exc:
            last_error = exc
            if attempt == BOOTSTRAP_MAX_ATTEMPTS:
                break
            sleep(attempt)
    raise TpexCABootstrapError(
        f"Official TWCA intermediate download failed after {BOOTSTRAP_MAX_ATTEMPTS} attempts"
    ) from last_error


def _verify_intermediate_chain(
    intermediate_path: Path,
    certifi_bundle: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise TpexCABootstrapError("OpenSSL is required to verify the TWCA intermediate chain")
    result = runner(
        [openssl, "verify", "-CAfile", str(certifi_bundle), str(intermediate_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown OpenSSL verification failure").strip()
        raise TpexCABootstrapError(f"TWCA intermediate chain is not trusted by certifi: {detail}")


def create_tpex_ca_bundle(
    *,
    downloader: Callable[..., requests.Response] = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    certifi_bundle: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TpexCABundle:
    """Create a temporary certifi-plus-pinned-intermediate bundle.

    The workspace is outside the repository and is removed on normal process
    exit.  Any bootstrap validation failure removes it immediately.
    """
    directory = Path(tempfile.mkdtemp(prefix="cb-radar-tpex-ca-"))
    try:
        der = _download_intermediate(downloader, sleep)
        fingerprint = hashlib.sha256(der).hexdigest().upper()
        if fingerprint != TWCA_INTERMEDIATE_SHA256:
            raise TpexCABootstrapError("TWCA intermediate fingerprint does not match the pinned value")
        try:
            certificate = x509.load_der_x509_certificate(der)
        except ValueError as exc:
            raise TpexCABootstrapError("Official TWCA download is not a DER certificate") from exc
        validate_twca_intermediate(certificate, now=now)

        intermediate_path = directory / "twca-intermediate.pem"
        intermediate_path.write_bytes(certificate.public_bytes(Encoding.PEM))
        trusted_bundle = certifi_bundle or Path(certifi.where())
        if not trusted_bundle.is_file():
            raise TpexCABootstrapError("certifi CA bundle is unavailable")
        _verify_intermediate_chain(intermediate_path, trusted_bundle, runner=runner)

        bundle_path = directory / "certifi-plus-twca.pem"
        with trusted_bundle.open("rb") as source, bundle_path.open("wb") as destination:
            shutil.copyfileobj(source, destination)
            destination.write(b"\n")
            destination.write(intermediate_path.read_bytes())
        bundle = TpexCABundle(directory, bundle_path, intermediate_path)
        atexit.register(bundle.cleanup)
        return bundle
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


class TpexHTTPSession(requests.Session):
    """Session that uses a verified temporary bundle only for the TPEx host."""

    def __init__(self, ca_bundle: TpexCABundle):
        super().__init__()
        self.ca_bundle = ca_bundle

    def request(self, method: str, url: str, *args: object, **kwargs: object) -> requests.Response:
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname == TPEX_HOST:
            requested_verify = kwargs.get("verify")
            if requested_verify is False:
                raise TpexCABootstrapError("TPEx requests may not disable TLS verification")
            kwargs["verify"] = str(self.ca_bundle.bundle_path)
        return super().request(method, url, *args, **kwargs)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self.ca_bundle.cleanup()


def build_tpex_session() -> TpexHTTPSession:
    """Return a session whose TPEx requests use the verified temporary bundle."""
    return TpexHTTPSession(create_tpex_ca_bundle())


def disable_tpex_adapter_retries(session: requests.Session) -> None:
    """Keep TPEx GET retry ownership at the full-response boundary.

    Some callers retain a generic urllib3 adapter for non-TPEx sources.  The
    exact TPEx-host mount prevents it from nesting below the application retry
    below, so one logical GET has at most three network attempts.
    """
    session.mount(f"https://{TPEX_HOST}/", HTTPAdapter(max_retries=0))


def get_tpex_full_response(
    session: requests.Session,
    url: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = TPEx_GET_MAX_ATTEMPTS,
    retry_exceptions: tuple[type[BaseException], ...] = TPEx_GET_TRANSIENT_EXCEPTIONS,
    **kwargs: object,
) -> requests.Response:
    """Fetch one TPEx HTTPS GET, retrying only failed *full-body* reads.

    ``stream=True`` deliberately makes complete body consumption explicit.
    Each retry issues a new GET; no partial bytes are retained or combined.
    HTTP status and JSON/schema failures remain caller-owned semantics.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != TPEX_HOST:
        raise ValueError("TPEx full-response retry accepts only www.tpex.org.tw HTTPS URLs")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    for attempt in range(1, max_attempts + 1):
        response: requests.Response | None = None
        try:
            response = session.get(url, stream=True, **kwargs)
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                # Real requests errors already carry this. Keep that useful
                # status context for callers using lightweight test/session
                # adapters too, without making HTTP errors retryable.
                if getattr(exc, "response", None) is None:
                    exc.response = response
                raise
            # Do not return until requests has fully consumed and cached this
            # one response body. ChunkedEncodingError belongs to this attempt.
            _ = response.content
        except retry_exceptions as exc:
            if response is not None:
                response.close()
            if attempt == max_attempts:
                raise
            print(
                "TPEx full-body transport retry: "
                f"host={parsed.hostname} exception={type(exc).__name__} "
                f"attempt={attempt}/{max_attempts}",
                file=sys.stderr,
            )
            sleep(attempt)
            continue
        if attempt > 1:
            print(
                "TPEx full-body request recovered after retry: "
                f"host={parsed.hostname} attempt={attempt}/{max_attempts}",
                file=sys.stderr,
            )
        return response
    raise AssertionError("unreachable TPEx full-response retry state")
