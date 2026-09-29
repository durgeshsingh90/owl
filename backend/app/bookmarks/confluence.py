"""Confluence Data Center PAT connection and page metadata retrieval."""

import base64
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx
from app.core.config import atomic_private, config_dir
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import BaseModel, SecretStr, field_validator


class ConfluenceSettings(BaseModel):
    base_url: str
    token: SecretStr
    verify_ssl: bool = True

    @field_validator("base_url")
    @classmethod
    def valid_base(cls, value):
        parsed = valid_url(value)
        if parsed.query or parsed.fragment:
            raise ValueError(
                "Enter the Confluence base URL without a query or fragment."
            )
        path = re.sub(r"/rest/api/?$", "", parsed.path.rstrip("/"))
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, "", ""))

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("A PAT is required.")
        return value


def valid_url(value):
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Use a complete HTTP or HTTPS URL without embedded credentials."
        )
    return parsed


def load():
    directory = config_dir()
    if not (directory / "confluence.enc").exists():
        raise ValueError("Save Confluence settings first.")
    return ConfluenceSettings.model_validate_json(
        Fernet((directory / "confluence.key").read_bytes()).decrypt(
            (directory / "confluence.enc").read_bytes()
        )
    )


def save(settings):
    directory = config_dir()
    key = directory / "confluence.key"
    if not key.exists():
        atomic_private(key, Fernet.generate_key())
    data = settings.model_dump(mode="json")
    data["token"] = settings.token.get_secret_value()
    atomic_private(
        directory / "confluence.enc",
        Fernet(key.read_bytes()).encrypt(json.dumps(data).encode()),
    )


class TextContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignore += 1
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignore = max(0, self.ignore - 1)
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        if not self.ignore:
            self.parts.append(data)

    def text(self):
        return "\n".join(
            line.strip() for line in "".join(self.parts).splitlines() if line.strip()
        )


class ConfluenceRequestError(HTTPException):
    def __init__(self, upstream_status, detail):
        super().__init__(502, detail)
        self.upstream_status = upstream_status


async def get(settings, path, params=None):
    url = settings.base_url + "/rest/api/" + path
    try:
        async with httpx.AsyncClient(
            verify=settings.verify_ssl, timeout=25, follow_redirects=False
        ) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": "Bearer " + settings.token.get_secret_value(),
                    "Accept": "application/json",
                },
            )
        if response.status_code != 200:
            explanation = {
                401: "PAT is invalid or expired",
                403: "access denied",
                404: "page or endpoint not found",
            }.get(response.status_code, "request failed")
            raise ConfluenceRequestError(
                response.status_code,
                f"Confluence HTTP {response.status_code}: {explanation}. Request: {url}",
            )
        data = response.json()
        if not isinstance(data, dict):
            raise HTTPException(502, "Confluence returned an unexpected response.")
        return data
    except httpx.TimeoutException:
        raise HTTPException(
            502,
            f"Confluence request timed out. Check VPN/network access and retry. Request: {url}",
        ) from None
    except httpx.RequestError as error:
        raise HTTPException(
            502,
            f"Cannot connect to Confluence ({type(error).__name__}). Check VPN/network, base URL and SSL settings. Request: {url}",
        ) from None
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(
            502,
            "Confluence returned an unexpected response; check the base URL and authentication.",
        ) from None


async def test(settings):
    user = await get(settings, "user/current")
    if user.get("type") == "anonymous" or not (
        user.get("name")
        or user.get("username")
        or user.get("accountId")
        or user.get("userKey")
    ):
        raise HTTPException(502, "Confluence did not authenticate the PAT.")
    return {
        "ok": True,
        "detail": "Connected as "
        + user.get("displayName", user.get("name", "Confluence user")),
    }


def explicit_ids(url):
    decoded = unquote(url)
    return list(
        dict.fromkeys(
            re.findall(
                r"(?:page[_-]?id|content[_-]?id)[=:/\s]+(\d+)|/(?:pages|content)/(\d+)(?:[/#?]|$)",
                decoded,
                flags=re.IGNORECASE,
            )
        )
    )


def named_ids(url):
    return [a or b for a, b in explicit_ids(url)]


class PageIdentity(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.canonical = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name", "").lower() in {
            "ajs-page-id",
            "ajs-content-id",
        }:
            value = attrs.get("content", "")
            if value.isdecimal():
                self.ids.append(value)
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonical.append(attrs.get("href", ""))


def belongs_to_server(settings, url):
    parsed, base = valid_url(url), urlsplit(settings.base_url)
    return (parsed.scheme, parsed.netloc.lower()) == (
        base.scheme,
        base.netloc.lower(),
    ) and (
        parsed.path == base.path or parsed.path.startswith(base.path.rstrip("/") + "/")
    )


async def identity_from_url(settings, url):
    """Follow only configured-server redirects; never forward the PAT elsewhere."""
    current, seen = url, set()
    async with httpx.AsyncClient(
        verify=settings.verify_ssl, timeout=25, follow_redirects=False
    ) as client:
        for _ in range(6):
            if not belongs_to_server(settings, current) or current in seen:
                raise ValueError(
                    "Confluence URL redirects outside the configured server or loops."
                )
            seen.add(current)
            try:
                response = await client.get(
                    current,
                    headers={
                        "Authorization": "Bearer " + settings.token.get_secret_value(),
                        "Accept": "text/html",
                    },
                )
            except httpx.RequestError:
                raise HTTPException(
                    502, "Cannot retrieve the Confluence page URL."
                ) from None
            if response.status_code in {301, 302, 303, 307, 308}:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            if response.status_code != 200:
                raise HTTPException(
                    502, f"Confluence page URL returned HTTP {response.status_code}."
                )
            parser = PageIdentity()
            parser.feed(response.text)
            ids = parser.ids or named_ids(current)
            for link in parser.canonical:
                canonical = urljoin(current, link)
                if belongs_to_server(settings, canonical):
                    ids.extend(named_ids(canonical))
            return list(dict.fromkeys(ids))
    raise ValueError("Confluence URL exceeded the redirect limit.")


def decode_short_link(code):
    """Confluence /x/<code> links are little-endian page IDs in URL-safe base64."""
    try:
        return str(
            int.from_bytes(
                base64.b64decode(code + "=" * (-len(code) % 4), altchars=b"-_", validate=True),
                "little",
            )
        )
    except ValueError:
        raise ValueError("Invalid Confluence short link.") from None


async def resolve_page_id(settings, value):
    """Page ID for a page ID, any page URL form, a short link or a space URL (its home page)."""
    target = value.strip()
    if re.fullmatch(r"[0-9]{1,20}", target) and int(target):
        return str(int(target))
    if not belongs_to_server(settings, target):
        raise ValueError("Use a page on the configured Confluence server.")
    parsed = urlsplit(target)
    relative = parsed.path[len(urlsplit(settings.base_url).path.rstrip("/")) :]
    query = {key.lower(): item for key, item in parse_qsl(parsed.query)}
    ids = list(dict.fromkeys(named_ids(target)))
    if len(ids) == 1:
        return ids[0]
    short = re.fullmatch(r"/x/([A-Za-z0-9_-]+)/?", relative)
    if short:
        return decode_short_link(short[1])
    space = title = None
    if query.get("spacekey") and query.get("title"):
        space, title = query["spacekey"], query["title"]
    named = re.fullmatch(r"/display/([^/]+)/(.+?)/?", relative)
    if named:
        space, title = unquote(named[1]), unquote(named[2].replace("+", " "))
    if space and title:
        data = await get(
            settings, "content", {"spaceKey": space, "title": title, "type": "page", "limit": 2}
        )
        matches = data.get("results", [])
        if len(matches) == 1:
            return str(matches[0]["id"])
        raise ValueError(f'No single page titled "{title}" in space {space}.')
    key = query.get("key") if relative.endswith("/viewspace.action") else None
    home = re.fullmatch(r"/(?:display|spaces)/([^/]+)(?:/overview)?/?", relative)
    key = key or (unquote(home[1]) if home else None)
    if key:
        data = await get(settings, "space/" + key, {"expand": "homepage"})
        page = (data.get("homepage") or {}).get("id")
        if not page:
            raise ValueError(f"Space {key} has no home page.")
        return str(page)
    found = await identity_from_url(settings, target)
    if len(found) == 1:
        return str(found[0])
    raise ValueError(
        "Could not find a Confluence page for this URL. Copy the page link from your browser."
    )


async def top_most_page(settings, page_id):
    """The highest ancestor of a page (usually the space home page), or the page itself."""
    data = await get(settings, "content/" + str(page_id), {"expand": "ancestors"})
    if data.get("type") not in (None, "page"):
        raise ValueError("This Confluence link is not a page.")
    ancestors = data.get("ancestors") or []
    return str(ancestors[0]["id"] if ancestors else data["id"])


async def resolved_content(settings, url, page_id):
    params = {"expand": "body.view,body.storage,space,history,version,ancestors"}

    async def fetch(candidate):
        data = await get(settings, "content/" + str(candidate), params)
        if (
            str(data.get("id")) != str(candidate)
            or data.get("type") != "page"
            or data.get("status", "current") != "current"
        ):
            raise ValueError("This Confluence link is not a current page.")
        return data

    if page_id and str(page_id).isdecimal():
        return await fetch(page_id)
    # Prefer identity supplied by the actual page over arbitrary numbers in its URL.
    try:
        identities = await identity_from_url(settings, url)
    except (HTTPException, ValueError) as error:
        identities = []
        url_error = error
    else:
        url_error = None
    if len(identities) == 1:
        return await fetch(identities[0])
    if len(identities) > 1:
        raise ValueError("Confluence returned conflicting page IDs for this URL.")
    candidates = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6,20}(?!\d)", unquote(url))))
    if len(candidates) > 12:
        raise ValueError(
            "Too many numeric candidates in this URL. Use the page's canonical link."
        )
    found = []
    for candidate in candidates:
        try:
            found.append(await fetch(candidate))
        except HTTPException as error:
            if "HTTP 404:" not in str(error.detail):
                raise
        except ValueError:
            continue
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ValueError(
            "Several numbers in this URL match Confluence pages. Use the canonical page link."
        )
    if url_error:
        raise url_error
    raise ValueError(
        "Could not identify a Confluence page from this URL or its HTML. Copy the page's canonical link."
    )


async def metadata(url, *, settings=None, include_raw=False):
    parsed = valid_url(url)
    result = {
        "url": url,
        "domain": parsed.hostname.lower(),
        "title": unquote(parsed.path.rstrip("/").split("/")[-1]) or parsed.hostname,
        "sourceType": "web",
        "description": "Saved web bookmark",
    }
    try:
        settings = settings or load()
    except ValueError:
        if "confluence" in parsed.hostname.lower():
            raise ValueError(
                "Save Confluence settings before adding this page."
            ) from None
        return result
    base = urlsplit(settings.base_url)
    if (parsed.scheme, parsed.netloc.lower()) != (
        base.scheme,
        base.netloc.lower(),
    ) or not (
        parsed.path == base.path or parsed.path.startswith(base.path.rstrip("/") + "/")
    ):
        return result
    relative = parsed.path[len(base.path) :]
    page_id = next(iter(named_ids(url)), None)
    if not page_id:
        match = re.search(r"/(?:pages|content)/(\d+)(?:/|$)", relative)
        page_id = match[1] if match else None
    if not page_id and relative.startswith("/x/"):
        try:
            encoded = relative.split("/")[2]
            page_id = str(
                int.from_bytes(
                    base64.b64decode(
                        encoded + "=" * (-len(encoded) % 4),
                        altchars=b"-_",
                        validate=True,
                    ),
                    "little",
                )
            )
        except ValueError:
            raise ValueError("Invalid Confluence short link.") from None
    if not page_id:
        match = re.fullmatch(r"/display/([^/]+)/(.+)", relative)
        if match:
            data = await get(
                settings,
                "content",
                {
                    "spaceKey": unquote(match[1]),
                    "title": unquote(match[2].replace("+", " ")),
                    "type": "page",
                    "limit": 2,
                },
            )
            matches = data.get("results", [])
            if len(matches) == 1:
                page_id = matches[0]["id"]
    data = await resolved_content(settings, url, page_id)
    body = data.get("body", {})
    parser = TextContent()
    parser.feed(
        body.get("view", {}).get("value") or body.get("storage", {}).get("value", "")
    )
    text = parser.text()
    version, history = data.get("version", {}), data.get("history", {})
    ancestors = [
        {
            "page_id": str(a["id"]),
            "title": a.get("title", ""),
            "url": settings.base_url + "/pages/viewpage.action?pageId=" + str(a["id"]),
        }
        for a in data.get("ancestors", [])
    ]
    stamp = datetime.now(timezone.utc).isoformat()
    result.update(
        title=data.get("title", result["title"]),
        sourceType="confluence",
        page_id=str(data["id"]),
        confluenceBaseUrl=settings.base_url,
        space=data.get("space", {}).get("name", ""),
        spaceKey=data.get("space", {}).get("key", ""),
        author=history.get("createdBy", {}).get("displayName", ""),
        lastEditor=version.get("by", {}).get("displayName", ""),
        version=version.get("number"),
        writtenAt=history.get("createdDate"),
        confluenceUpdatedAt=version.get("when"),
        ancestors=ancestors,
        breadcrumb=[a["title"] for a in ancestors],
        contentText=text,
        pageTextSizeBytes=len(text.encode()),
        description=text[:250],
        lastRefreshed=stamp,
        updatedInOwlAt=stamp,
    )
    if include_raw:
        result["rawMetadata"] = data
    return result
