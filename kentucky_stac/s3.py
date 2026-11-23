"""Reading assets whose URL is an s3:// address (common outside KyFromAbove, e.g. Earth Search).

QGIS's network stack and GDAL's /vsicurl only speak http(s), so:

- a PUBLIC bucket is read by turning s3://bucket/key into its https address (to_https);
- a PRIVATE or requester-pays bucket needs AWS credentials, so when the user has some configured
  (credentials_available) the file is read through GDAL's /vsis3/ instead, which signs the request
  from the standard sources (AWS_* environment variables, ~/.aws/credentials, AWS_PROFILE) and sends
  the requester-pays header.

Pure Python -- no QGIS import -- so it's unit-testable.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Dict, Mapping, Optional, Tuple

# S3-compatible services that are not Amazon: bucket name -> endpoint, a host (https) or "http://host:port". Their
# s3://bucket/key addresses must be read from that endpoint, not from AWS, and always with the user's keys.
# Copernicus Data Space keeps all of Sentinel in the "eodata" bucket; its keys come with a (free) registered account.
S3_ENDPOINTS: Dict[str, str] = {"eodata": "eodata.dataspace.copernicus.eu"}


def endpoint_for(bucket: str) -> Optional[str]:
    """The S3-compatible endpoint that serves `bucket`, or None for an ordinary AWS bucket."""
    return S3_ENDPOINTS.get(bucket)


def endpoint_options(endpoint: str) -> Dict[str, str]:
    """The GDAL /vsis3/ settings that point it at an S3-compatible `endpoint`: path-style requests to that host."""
    plain = endpoint.lower().startswith("http://")
    host = endpoint.split("://", 1)[-1].rstrip("/")
    return {"AWS_S3_ENDPOINT": host, "AWS_VIRTUAL_HOSTING": "FALSE", "AWS_HTTPS": "NO" if plain else "YES"}


def as_s3(url: str) -> str:
    """The s3:// form of an https address on an S3-compatible service (Copernicus: https://eodata.dataspace.copernicus.eu/
    eodata/<key> is s3://eodata/<key>), so it is read with the user's keys. Any other URL comes back unchanged."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return url
    for bucket, endpoint in S3_ENDPOINTS.items():
        host = endpoint.split("://", 1)[-1].rstrip("/")
        prefix = f"/{bucket}/"
        if parsed.netloc.lower() == host.lower() and (parsed.path.startswith(prefix) or parsed.path == f"/{bucket}"):
            return f"s3://{bucket}/{parsed.path[len(prefix):]}" if parsed.path.startswith(prefix) else f"s3://{bucket}/"
    return url


def is_s3(url: str) -> bool:
    return (url or "").lower().startswith("s3://")


def parse(url: str) -> Optional[Tuple[str, str]]:
    """(bucket, key) of an s3://bucket/key URL, or None if it isn't one."""
    if not is_s3(url):
        return None
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return None
    return parsed.netloc, urllib.parse.unquote(parsed.path.lstrip("/"))


def to_https(url: str) -> str:
    """The https address of an s3:// URL (other URLs pass through unchanged). Virtual-hosted style,
    except for a bucket name with a dot, whose name would not match S3's wildcard certificate: that
    one uses the path style, which S3 redirects to the right region."""
    parts = parse(url)
    if parts is None:
        return url
    bucket, key = parts
    quoted = urllib.parse.quote(key, safe="/")
    endpoint = endpoint_for(bucket)
    if endpoint:  # an S3-compatible service: path style at its own host (a public object is rare there)
        scheme = "http" if endpoint.lower().startswith("http://") else "https"
        return f"{scheme}://{endpoint.split('://', 1)[-1].rstrip('/')}/{bucket}/{quoted}"
    if "." in bucket:
        return f"https://s3.amazonaws.com/{bucket}/{quoted}"
    return f"https://{bucket}.s3.amazonaws.com/{quoted}"


def vsis3_path(url: str) -> Optional[str]:
    """GDAL's virtual path for an s3:// URL, e.g. /vsis3/bucket/key."""
    parts = parse(url)
    return f"/vsis3/{parts[0]}/{parts[1]}" if parts else None


def credentials_available(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> bool:
    """Whether AWS credentials look to be configured: access keys in the environment, or an AWS
    credentials file (AWS_SHARED_CREDENTIALS_FILE, else ~/.aws/credentials)."""
    env = os.environ if env is None else env
    if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
        return True
    path = env.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.join(
        home if home is not None else os.path.expanduser("~"), ".aws", "credentials"
    )
    return os.path.isfile(path)
