"""Resolve Google News RSS article URLs to the publisher's real URL.

Why this exists: Google News RSS items link to `news.google.com/rss/articles/<id>`, where
`<id>` is an opaque token. Old-format tokens carried a base64 protobuf containing the target
URL; the current `AU_yqL...` format does not — it is a server-side handle. Fetching the link
returns a JS shell, so the judging step could not read the article, and every `source_url`
had to be recovered by hand-search. That defect was recorded three times in the wiki dossier
(2026-08-18, 08-20, 08-21) before this file existed.

The resolution path is the same one news.google.com itself uses: read the article shell for
its per-article signature (`data-n-a-sg`) and timestamp (`data-n-a-ts`), then POST them to
the `batchexecute` RPC endpoint with the `Fbv4je` (`garturlreq`) payload, which answers with
the publisher URL.

This is an undocumented internal endpoint and will break when Google changes it. It is
therefore deliberately non-fatal: `resolve()` returns None on any failure and `resolve_all()`
falls back to the original Google URL, so a break degrades the run to the pre-2026-08-23
behaviour rather than killing it.

    python3 -m radar.gnews <url> [<url> ...]
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# The RPC wants the request array as a *string* inside the outer array, hence the nesting.
_GARTURLREQ = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,'
               'null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
               '"%s",%s,"%s"]')


def _http(url, data=None, headers=None, timeout=30):
    hdr = {"User-Agent": UA}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdr)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def is_google_news(url):
    return "news.google.com/rss/articles/" in (url or "")


def resolve(gurl, timeout=30):
    """Publisher URL for a Google News RSS article link, or None if it cannot be resolved."""
    if not is_google_news(gurl):
        return None
    try:
        article_id = gurl.split("/articles/")[1].split("?")[0]
        shell = _http(
            "https://news.google.com/rss/articles/%s?hl=en-US&gl=US&ceid=US:en" % article_id,
            timeout=timeout)
        sig = re.search(r'data-n-a-sg="([^"]+)"', shell)
        ts = re.search(r'data-n-a-ts="([^"]+)"', shell)
        if not (sig and ts):
            return None
        payload = ["Fbv4je", _GARTURLREQ % (article_id, ts.group(1), sig.group(1))]
        body = urllib.parse.urlencode({"f.req": json.dumps([[payload]])}).encode()
        resp = _http(BATCH_URL, data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                     timeout=timeout)
        for line in resp.split("\n"):
            if "garturlres" not in line:
                continue
            for frame in json.loads(line):
                if frame[0] == "wrb.fr":
                    return json.loads(frame[2])[1]
    except (urllib.error.URLError, ValueError, IndexError, KeyError, TypeError):
        return None
    return None


def resolve_all(urls, counts=None):
    """Resolve many, falling back to the original URL. `counts` collects yield if supplied."""
    out = []
    for u in urls:
        r = resolve(u) if is_google_news(u) else None
        if counts is not None:
            counts["gnews_resolved" if r else "gnews_unresolved"] = (
                counts.get("gnews_resolved" if r else "gnews_unresolved", 0) + 1)
        out.append(r or u)
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(resolve(arg) or "UNRESOLVED")
