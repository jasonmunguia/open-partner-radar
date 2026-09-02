"""Named-account Gmail sender for the partner-radar digest. Bring your own app password.

Credentials live in the macOS Keychain (never in a file, never in git):
    service = "gmail-app-password", account = <email address>
Friendly names -> addresses live in ~/.claude/tools/mail_accounts.json (created on first `add`).

Usage (from the repo root):
    python3 tools/mailer.py add <name> <sender-address>      # then paste the Gmail app password
                                                             # (https://myaccount.google.com/apppasswords)
    python3 tools/mailer.py list
    python3 tools/mailer.py test <name> <recipient-address>  # proves SMTP end to end
    python3 tools/mailer.py send <name> --to recipient@example.com --subject "Hi" --html-file body.html

    from mailer import send
    send("<name>", "recipient@example.com", "Subject", "<p>html</p>")

Sender and recipient must differ — Gmail buries self-sends.
"""
import json, os, smtplib, ssl, subprocess, sys
from email.message import EmailMessage

REGISTRY = os.path.expanduser("~/.claude/tools/mail_accounts.json")
SERVICE = "gmail-app-password"

DEFAULT_ACCOUNTS = {}   # nothing baked in: register senders with `add`

def _load():
    if os.path.exists(REGISTRY):
        try:
            return json.load(open(REGISTRY))
        except Exception:
            pass
    return dict(DEFAULT_ACCOUNTS)

ACCOUNTS = _load()

def _save(d):
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    json.dump(d, open(REGISTRY, "w"), indent=2)

def resolve(name):
    """Accept a friendly name ('personal') or a raw email address."""
    a = _load()
    if name in a:
        return a[name]
    if "@" in name:
        return name
    raise KeyError(f"unknown account '{name}'. Known: {list(a)}")

def get_password(name):
    addr = resolve(name)
    r = subprocess.run(["security", "find-generic-password", "-a", addr, "-s", SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"no app password stored for {addr}. Run: mailer.py add {name} {addr}")
    return r.stdout.strip().replace(" ", "")     # Google displays it in 4-char groups

def store_password(name, addr, password):
    a = _load()
    a[name] = addr
    _save(a)
    subprocess.run(["security", "add-generic-password", "-a", addr, "-s", SERVICE,
                    "-w", password.replace(" ", ""), "-U"], check=True)
    return True

def has_password(name):
    try:
        get_password(name)
        return True
    except Exception:
        return False

def _ssl_context():
    """This Mac's stock Python lacks a usable CA bundle; prefer certifi, then SSL_CERT_FILE."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    cf = os.environ.get("SSL_CERT_FILE")
    if cf and os.path.exists(cf):
        return ssl.create_default_context(cafile=cf)
    return ssl.create_default_context()

def _plain_text(msg):
    """First text/plain part, decoded. Empty string when the message has none."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        if p.get_content_type() == "text/plain":
            raw = p.get_payload(decode=True) or b""
            return raw.decode(p.get_content_charset() or "utf-8", "replace")
    return ""

def search_inbox(account, subject_token, since_ts=0):
    """Messages in `account`'s INBOX whose Subject contains subject_token (must be
    plain ASCII — IMAP SUBJECT search does not see RFC2047-encoded words). Same
    Keychain app password as SMTP. Read-only: BODY.PEEK leaves unread flags alone.
    Returns [{from, subject, ts, text}] oldest-first."""
    import email as _email
    import imaplib
    from email.utils import parsedate_to_datetime
    addr = resolve(account)
    pw = get_password(account)
    out = []
    with imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=_ssl_context()) as im:
        im.login(addr, pw)
        im.select("INBOX", readonly=True)
        typ, data = im.search(None, "SUBJECT", f'"{subject_token}"')
        for num in (data[0].split() if typ == "OK" and data and data[0] else []):
            typ, md = im.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = _email.message_from_bytes(md[0][1])
            try:
                ts = int(parsedate_to_datetime(msg["Date"]).timestamp())
            except (TypeError, ValueError):     # absent or malformed Date header
                ts = 0
            if ts < since_ts:
                continue
            out.append({"from": msg.get("From", ""), "subject": msg.get("Subject", ""),
                        "ts": ts, "text": _plain_text(msg)})
    return sorted(out, key=lambda r: r["ts"])

def send(account, to, subject, html, text=None, cc=None):
    """Send AS `account` (friendly name or address). Returns the sender address on success."""
    addr = resolve(account)
    pw = get_password(account)
    m = EmailMessage()
    m["From"] = addr
    m["To"] = to if isinstance(to, str) else ", ".join(to)
    if cc:
        m["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    m["Subject"] = subject                      # EmailMessage handles emoji/UTF-8 headers
    m.set_content(text or "This message is HTML. Open in an HTML-capable client.")
    m.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=_ssl_context(), timeout=30) as s:
        s.login(addr, pw)
        s.send_message(m)
    return addr

def _cli():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        for n, a in _load().items():
            print(f"  {n:10} {a:32} {'✅ password stored' if has_password(n) else '— no password yet'}")
    elif cmd == "add":
        name, addr = sys.argv[2], sys.argv[3]
        pw = sys.argv[4] if len(sys.argv) > 4 else input("app password: ")
        store_password(name, addr, pw)
        print(f"stored for {name} ({addr})")
    elif cmd == "test":
        name = sys.argv[2]
        if len(sys.argv) < 4:
            sys.exit("usage: mailer.py test <name> <recipient-address>")
        to = sys.argv[3]
        who = send(name, to, f"✅ Mailer test — sending as {resolve(name)}",
                   f"<p>Success. This was sent from <b>{resolve(name)}</b> "
                   f"via the named channel <code>{name}</code>.</p>")
        print(f"sent as {who} -> {to}")
    elif cmd == "send":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("account"); p.add_argument("--to", required=True)
        p.add_argument("--subject", required=True); p.add_argument("--html-file", required=True)
        p.add_argument("--cc")
        a = p.parse_args(sys.argv[2:])
        who = send(a.account, a.to, a.subject, open(a.html_file).read(), cc=a.cc)
        print(f"sent as {who} -> {a.to}")
    else:
        print(__doc__)

if __name__ == "__main__":
    _cli()
