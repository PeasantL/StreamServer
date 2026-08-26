"""Resolving a booru post *page* into the original file it hosts.

A booru post URL names an HTML page, not a media file, so pasting one into the
download box only ever produced "URL must point at a .mp4 or .webm file". These
resolvers turn a post page into the URL of the **original** upload -- never the
site's downscaled "sample" -- so the catalogue stores the full-quality file.

Two rules keep this from becoming a second SSRF hole:

* Only hosts named in ``SITES`` are treated as boorus at all. Anything else
  goes down the ordinary direct-URL path untouched.
* Whatever a site hands back, by API or by scraping, must live on that site's
  own domain (see ``_assert_on_domain``). Post pages carry user-written
  comments, and a comment containing a plausible-looking media URL must not be
  able to steer the download somewhere else.

The URLs returned here are still passed through ``downloads.assert_safe_url``
by the caller before anything is fetched.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import SplitResult, parse_qs, urlencode, urljoin, urlsplit

import downloads
from config import settings

log = logging.getLogger(__name__)

# Extensions a booru can serve as an original upload. Wider than the
# catalogue's own ALLOWED_EXTENSIONS on purpose: recognising that a post is a
# PNG lets us say so, instead of reporting a generic parse failure.
MEDIA_EXTENSIONS = (".mp4", ".webm", ".mov", ".gif", ".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

# Ceilings on imported tags. A popular post can carry several hundred; the
# catalogue only needs enough of them to search by, and the update API caps a
# tag list at 50 entries anyway.
MAX_TAGS = 50
MAX_TAG_LENGTH = 100

# Originals live under /images/ (gelbooru family, rule34.us) or /original/
# (danbooru); the resized copies the site generates live under /samples/ or
# /thumbnails/. Anchoring on the directory is what keeps the scraper off the
# sample even when both appear on the page.
_CANDIDATE_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?/(?:images|original)/[^\s\"'<>\\]+?"
    r"\.(?:mp4|webm|mov|gif|jpe?g|png|webp)",
    re.IGNORECASE,
)

_DANBOORU_PATH_RE = re.compile(r"^/post(?:s|/show)/(\d+)")


class BooruError(ValueError):
    """A booru URL could not be turned into a downloadable original file."""


@dataclass(frozen=True)
class Resolution:
    """What a per-site resolver found: the file, and the post's tags.

    Tags travel with the file URL rather than being fetched separately because
    both come out of the same API response; a second request to read them
    would double the rate-limit cost of every import.
    """

    file_url: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Post:
    """One resolved post: where it came from and what to fetch."""

    site: str
    post_id: str
    page_url: str
    file_url: str
    tags: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        """Catalogue title. The remote filename is an md5 hash, so it is
        useless as a label; "gelbooru 12345" is what the user can search for."""
        return f"{self.site} {self.post_id}"


@dataclass(frozen=True)
class Site:
    name: str
    hosts: tuple[str, ...]
    # Registrable domain the media must sit on. Boorus serve files from their
    # own CDN subdomains (img3.gelbooru.com, cdn.donmai.us, video.rule34.us).
    media_domain: str
    post_id: Callable[[SplitResult], str | None]
    file_url: Callable[[Site, str, str], Resolution]
    # Shown when a URL is on a booru host but is not a single post page.
    post_url_hint: str


# --- post id extraction ------------------------------------------------------


def _digits(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0].strip()
    return value if value.isdigit() else None


def _query(parts: SplitResult) -> dict[str, list[str]]:
    return parse_qs(parts.query)


def _danbooru_post_id(parts: SplitResult) -> str | None:
    """``/posts/123``, and the legacy ``/post/show/123``."""
    match = _DANBOORU_PATH_RE.match(parts.path)
    return match.group(1) if match else None


def _gelbooru_post_id(parts: SplitResult) -> str | None:
    """``/index.php?page=post&s=view&id=123``."""
    query = _query(parts)
    if query.get("page", [""])[0] != "post" or query.get("s", [""])[0] != "view":
        return None
    return _digits(query.get("id"))


def _rule34us_post_id(parts: SplitResult) -> str | None:
    """``/index.php?r=posts/view&id=123``."""
    query = _query(parts)
    if query.get("r", [""])[0] != "posts/view":
        return None
    return _digits(query.get("id"))


# --- helpers -----------------------------------------------------------------


def _origin(page_url: str) -> str:
    """``https://host[:port]`` for *page_url*.

    Rebuilt from the parsed host rather than from ``netloc`` so that any
    ``user:pass@`` prefix is dropped, and pinned to https because every site in
    the table is https-only: a pasted ``http://`` link must not downgrade the
    API call that follows.
    """
    parts = urlsplit(page_url)
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return f"https://{parts.hostname}{port}"


def _assert_on_domain(site: Site, file_url: str) -> str:
    """Refuse a media URL that does not belong to *site*."""
    parts = urlsplit(file_url)
    if parts.scheme not in ("http", "https"):
        raise BooruError(f"{site.name} returned a non-HTTP media URL")

    host = (parts.hostname or "").lower()
    if host != site.media_domain and not host.endswith(f".{site.media_domain}"):
        # Post pages carry user-written comments; an off-domain URL found in
        # one is not something this post actually hosts.
        raise BooruError(
            f"Refusing {file_url!r}: not hosted on {site.media_domain}"
        )
    return file_url


def _media_extension(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


def _parse_tags(raw: object) -> tuple[str, ...]:
    """Normalise a booru tag field into an ordered, de-duplicated tuple.

    Every site in the table reports tags as one whitespace-separated string
    (danbooru's ``tag_string``, the gelbooru family's ``tags``), but a few
    clones send a list instead, so both shapes are accepted. The result is
    capped because a busy post can carry hundreds of tags and the catalogue
    only needs enough of them to be searchable.
    """
    if isinstance(raw, str):
        parts = raw.split()
    elif isinstance(raw, list):
        parts = [str(part) for item in raw for part in str(item).split()]
    else:
        return ()

    seen: list[str] = []
    for part in parts:
        tag = part.strip().lower()
        if not tag or len(tag) > MAX_TAG_LENGTH or tag in seen:
            continue
        seen.append(tag)
        if len(seen) >= MAX_TAGS:
            break
    return tuple(seen)


def _scrape_file_url(site: Site, page_url: str) -> str:
    """Find the original file linked from the post page itself.

    The fallback for sites with no usable API. Video candidates win over image
    ones because this is a video catalogue and a post page can legitimately
    reference both.
    """
    page = downloads.fetch_text(page_url, headers={"Referer": page_url})

    candidates: list[str] = []
    for match in _CANDIDATE_RE.finditer(page):
        candidate = unescape(match.group(0))
        try:
            _assert_on_domain(site, candidate)
        except BooruError:
            continue
        if candidate not in candidates:
            candidates.append(candidate)

    if not candidates:
        raise BooruError(
            f"Could not find an original file on that {site.name} page"
        )

    for candidate in candidates:
        if _media_extension(candidate) in VIDEO_EXTENSIONS:
            return candidate
    return candidates[0]


# --- per-site resolvers ------------------------------------------------------


def _danbooru_file_url(site: Site, page_url: str, post_id: str) -> Resolution:
    api = f"{_origin(page_url)}/posts/{post_id}.json"
    try:
        data = downloads.fetch_json(api, headers={"Referer": page_url})
    except (ValueError, OSError) as exc:
        raise BooruError(f"Could not read {site.name} post {post_id}: {exc}") from None

    if not isinstance(data, dict):
        raise BooruError(f"Unexpected API response for {site.name} post {post_id}")

    file_url = data.get("file_url")
    if not file_url:
        # Danbooru withholds file_url for posts restricted to logged-in
        # accounts; there is no original to fetch without credentials.
        raise BooruError(
            f"{site.name} post {post_id} does not expose an original file "
            "(it is probably restricted to logged-in accounts)"
        )
    return Resolution(
        file_url=_assert_on_domain(site, urljoin(_origin(page_url), str(file_url))),
        tags=_parse_tags(data.get("tag_string")),
    )


def _gelbooru_posts(data: object) -> list[dict]:
    """Normalise the several shapes the gelbooru-family API replies with.

    ``json=1`` returns ``{"post": [...]}`` on gelbooru itself, a bare list on
    most clones, and ``{"post": {...}}`` when a single post is requested from
    some of them. An empty result omits the key entirely.
    """
    if isinstance(data, list):
        posts = data
    elif isinstance(data, dict):
        found = data.get("post", [])
        posts = found if isinstance(found, list) else [found]
    else:
        return []
    return [post for post in posts if isinstance(post, dict)]


def _gelbooru_file_url(site: Site, page_url: str, post_id: str) -> Resolution:
    origin = _origin(page_url)
    query = {"page": "dapi", "s": "post", "q": "index", "json": "1", "id": post_id}

    # Gelbooru itself has required credentials on the JSON API since 2024.
    # Without them the request comes back empty rather than failing, which is
    # why the scrape below is not merely a belt-and-braces path.
    if site.name == "gelbooru" and settings.gelbooru_api_key and settings.gelbooru_user_id:
        query["api_key"] = settings.gelbooru_api_key
        query["user_id"] = settings.gelbooru_user_id

    api = f"{origin}/index.php?{urlencode(query)}"
    try:
        data = downloads.fetch_json(api, headers={"Referer": page_url})
    except (ValueError, OSError) as exc:
        log.info(
            "%s API lookup for post %s failed (%s); scraping the page",
            site.name, post_id, exc,
        )
        data = None

    for post in _gelbooru_posts(data):
        file_url = post.get("file_url")
        if file_url:
            return Resolution(
                file_url=_assert_on_domain(site, urljoin(origin, str(file_url))),
                tags=_parse_tags(post.get("tags")),
            )

    # The page scrape finds a file but carries no usable tag list, so an import
    # that falls back to it is simply untagged.
    return Resolution(file_url=_assert_on_domain(site, _scrape_file_url(site, page_url)))


def _scrape_only_file_url(site: Site, page_url: str, post_id: str) -> Resolution:
    """For sites with no public API at all (rule34.us)."""
    return Resolution(file_url=_assert_on_domain(site, _scrape_file_url(site, page_url)))


# --- site table --------------------------------------------------------------

_GELBOORU_HINT = "open the post and paste its ...index.php?page=post&s=view&id=... URL"

SITES: tuple[Site, ...] = (
    Site(
        name="danbooru",
        hosts=("danbooru.donmai.us", "safebooru.donmai.us"),
        media_domain="donmai.us",
        post_id=_danbooru_post_id,
        file_url=_danbooru_file_url,
        post_url_hint="open the post and paste its .../posts/<id> URL",
    ),
    Site(
        name="gelbooru",
        hosts=("gelbooru.com",),
        media_domain="gelbooru.com",
        post_id=_gelbooru_post_id,
        file_url=_gelbooru_file_url,
        post_url_hint=_GELBOORU_HINT,
    ),
    # Same software as gelbooru, so the same resolver; their APIs are usually
    # open, and the page scrape covers them when they are not.
    Site(
        name="rule34.xxx",
        hosts=("rule34.xxx",),
        media_domain="rule34.xxx",
        post_id=_gelbooru_post_id,
        file_url=_gelbooru_file_url,
        post_url_hint=_GELBOORU_HINT,
    ),
    Site(
        name="safebooru",
        hosts=("safebooru.org",),
        media_domain="safebooru.org",
        post_id=_gelbooru_post_id,
        file_url=_gelbooru_file_url,
        post_url_hint=_GELBOORU_HINT,
    ),
    Site(
        name="rule34.us",
        hosts=("rule34.us",),
        media_domain="rule34.us",
        post_id=_rule34us_post_id,
        file_url=_scrape_only_file_url,
        post_url_hint="open the post and paste its ...index.php?r=posts/view&id=... URL",
    ),
)


def find_site(url: str) -> Site | None:
    """The booru *url* belongs to, matched on host alone and without network.

    Only the site's own front-end hosts are listed, so a direct CDN link a user
    copied out of a post (img3.gelbooru.com/...) is not matched here and takes
    the ordinary direct-download path.
    """
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    return next((site for site in SITES if host in site.hosts), None)


def resolve_post(url: str) -> Post:
    """Turn a booru post page URL into the original file it hosts."""
    site = find_site(url)
    if site is None:
        raise BooruError(f"{url!r} is not a recognised booru URL")

    post_id = site.post_id(urlsplit(url))
    if post_id is None:
        raise BooruError(
            f"That is a {site.name} URL but not a single post; {site.post_url_hint}."
        )

    resolution = site.file_url(site, url, post_id)
    log.info(
        "Resolved %s post %s to %s (%d tag(s))",
        site.name, post_id, resolution.file_url, len(resolution.tags),
    )
    return Post(
        site=site.name,
        post_id=post_id,
        page_url=url,
        file_url=resolution.file_url,
        tags=resolution.tags,
    )


def supported_sites() -> list[str]:
    return [site.name for site in SITES]
