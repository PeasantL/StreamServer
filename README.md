# StreamServer

A small self-hosted video catalogue and streaming server. It scans a folder of
videos, generates thumbnails with ffmpeg, transcodes WebM to MP4 so browsers and
iOS can play it, and serves everything over HTTP with byte-range support so you
can seek.

> **Security note.** The IP allowlist is a network filter, not authentication.
> Anyone who can reach the port from an allowed address can download, rename and
> delete videos. Run it on a LAN or behind a VPN. Do not expose it to the
> internet.

## Requirements

- Python 3.11+ and `ffmpeg` (which also provides `ffprobe`), or Docker.
- On Debian/Ubuntu: `sudo apt-get install ffmpeg`

## Running with Docker

```bash
cp .env.example .env      # then edit PARENT_DIRECTORY, VIDEO_FOLDER, ALLOWED_IPS
docker compose up -d
```

Open <http://localhost:6969>.

`PARENT_DIRECTORY` is the directory that *contains* your video folders; the
buttons across the top of the page switch between its subdirectories.

## Running directly

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config_example.json config.json    # then edit video_dir and allowed_ips
python main.py
```

## Configuration

Values resolve highest-precedence first: environment variable, then
`config.json`, then the built-in default.

| Key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `video_dir` | `VIDEO_DIR` | `videos` | Folder to open on startup. |
| `thumbnail_dir` | `THUMBNAIL_DIR` | `thumbnails` | Where generated thumbnails are written. |
| `db_file` | `DB_FILE` | `video_db.json` | Catalogue database. |
| `allowed_ips` | `ALLOWED_IPS` | `127.0.0.1,::1` | Addresses or CIDR blocks allowed to connect. |
| `trusted_proxies` | `TRUSTED_PROXIES` | *(empty)* | Proxies whose `X-Forwarded-For` may be believed. |
| `ffmpeg_timeout` | `FFMPEG_TIMEOUT` | `30` | Seconds allowed for a thumbnail or probe. |
| `convert_timeout` | `CONVERT_TIMEOUT` | `3600` | Seconds allowed for one WebM→MP4 transcode. |
| `thumbnail_width` | `THUMBNAIL_WIDTH` | `320` | Maximum thumbnail width in pixels. |
| `max_download_bytes` | `MAX_DOWNLOAD_BYTES` | `8589934592` | Ceiling on a single remote download. |
| `download_timeout` | `DOWNLOAD_TIMEOUT` | `30` | Seconds allowed for a remote request. |
| `gelbooru_api_key` | `GELBOORU_API_KEY` | *(empty)* | Optional Gelbooru API key; see Booru post pages. |
| `gelbooru_user_id` | `GELBOORU_USER_ID` | *(empty)* | Optional Gelbooru user id, paired with the key. |
| `host` / `port` | `HOST` / `PORT` | `0.0.0.0` / `6969` | Listen address. |
| — | `PARENT_DIR` | parent of `video_dir` | Directory whose subfolders are offered as switchable libraries. |
| — | `CONFIG_FILE` | `config.json` | Path to the config file itself. |

`config.json` is never written back to. The folder you are currently browsing is
runtime state and is stored in the database file instead.

### About `trusted_proxies`

`X-Forwarded-For` is set by the client unless a proxy you control overwrites it.
Leave `trusted_proxies` empty and the header is ignored entirely. Set it to your
reverse proxy's address and the last hop that proxy appended is used as the
client address.

## Remote downloads

`POST /api/download` fetches a `.mp4` or `.webm` URL. Only `http` and `https`
are accepted, hostnames resolving to private, loopback or link-local addresses
are refused (including across redirects), and the download stops at
`max_download_bytes`.

### Booru post pages

The same box also accepts a booru post *page*, and fetches the original file
that post holds — not the resized "sample" the page displays:

| Site | URL to paste |
| --- | --- |
| Danbooru | `https://danbooru.donmai.us/posts/12345` |
| Gelbooru | `https://gelbooru.com/index.php?page=post&s=view&id=12345` |
| rule34.us | `https://rule34.us/index.php?r=posts/view&id=12345` |
| rule34.xxx | `https://rule34.xxx/index.php?page=post&s=view&id=12345` |
| Safebooru | `https://safebooru.org/index.php?page=post&s=view&id=12345` |

Danbooru and the gelbooru-family sites are read through their JSON APIs; where
the API is unavailable the post page itself is parsed for the original link,
which is the only route rule34.us offers. The entry is titled `gelbooru 12345`
after the post it came from, because boorus name their files by md5 hash, and
the post URL is kept as the description.

**Only video posts can be added.** This is a video catalogue: an `.mp4` or
`.webm` post is stored, and anything else is refused with a message naming the
type it found, rather than downloaded and then discarded.

Resolution is held to the same rules as any other download, plus one more: a
file URL must live on the booru's own domain. Post pages carry user-written
comments, and a comment containing a plausible-looking media URL must not be
able to redirect the fetch somewhere else.

Gelbooru requires credentials on its JSON API. Set `gelbooru_api_key` and
`gelbooru_user_id` (or `GELBOORU_API_KEY` / `GELBOORU_USER_ID`) from *Account
→ Options → API Access Credentials* to use it; without them gelbooru falls
back to reading the post page, which usually still works.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest
```

Tests cover the Range parser, the IP allowlist and forwarded-header handling,
path containment, the SSRF guard, booru URL resolution, and concurrent database
writes. They do not require ffmpeg or a network connection.

## Licence

No licence has been chosen yet, which means default copyright applies and others
have no right to use or redistribute this code. Add a `LICENSE` file if you want
to change that.
