import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from django.db import migrations, models


_SCP_REMOTE = re.compile(r"^[A-Za-z0-9._-]+@(?P<host>[A-Za-z0-9.-]+):[^\s]+$")
_BUILT_INS = {"bitbucket.org", "github.com"}


def _hostname(value):
    try:
        candidate = unicodedata.normalize("NFKC", str(value or "")).rstrip(".")
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is not None:
        return address.compressed.casefold()
    try:
        encoded = candidate.encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        return None
    labels = encoded.split(".")
    if (
        not encoded
        or len(encoded) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    ):
        return None
    return encoded


def _origin(value):
    raw = str(value or "").strip()
    match = _SCP_REMOTE.fullmatch(raw)
    if match is not None:
        hostname = _hostname(match.group("host"))
        port = 443
    else:
        try:
            parsed = urlsplit(raw)
            hostname = _hostname(parsed.hostname)
            parsed_port = parsed.port
        except ValueError:
            return None
        if parsed.scheme.casefold() not in {"https", "ssh"} or hostname is None:
            return None
        port = (parsed_port or 443) if parsed.scheme.casefold() == "https" else 443
    if hostname is None or hostname in _BUILT_INS or not 1 <= port <= 65_535:
        return None
    rendered = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{rendered}:{port}", hostname, port


def backfill_referenced_hosts(apps, schema_editor):
    Repository = apps.get_model("bitbucket_search", "BitbucketRepository")
    Credential = apps.get_model("bitbucket_search", "BitbucketHTTPSCredential")
    TrustedHost = apps.get_model("bitbucket_search", "TrustedRepositoryHost")
    candidates = {
        normalized
        for value in (
            *Repository.objects.values_list("remote_url", flat=True),
            *Credential.objects.values_list("origin", flat=True),
        )
        if (normalized := _origin(value)) is not None
    }
    for canonical_origin, hostname, port in sorted(candidates):
        if TrustedHost.objects.filter(hostname=hostname, port=port).exists():
            continue
        TrustedHost.objects.create(
            canonical_origin=canonical_origin,
            hostname=hostname,
            port=port,
            source="legacy",
            enabled=True,
        )


def remove_backfilled_hosts(apps, schema_editor):
    TrustedHost = apps.get_model("bitbucket_search", "TrustedRepositoryHost")
    TrustedHost.objects.filter(source="legacy").delete()


class Migration(migrations.Migration):
    dependencies = [("bitbucket_search", "0017_adaptive_pdf_pipeline")]

    operations = [
        migrations.AddField(
            model_name="trustedrepositoryhost",
            name="source",
            field=models.CharField(
                choices=[
                    ("ui", "Added in Settings"),
                    ("legacy", "Migrated compatibility approval"),
                ],
                db_index=True,
                default="ui",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_referenced_hosts, remove_backfilled_hosts),
    ]
