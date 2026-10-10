#!/usr/bin/env python3
"""Send a message to Feishu, cc-connect first, Feishu Open API as fallback.

cc-connect's send needs an active agent session (idle timeout ~15min), which
fails for unattended early-morning cron reports. The fallback talks to the
Feishu Open API directly with the app credentials from cc-connect's own
config (never copied into the repo).

Usage: feishu_send.py < message   (or --file path)
Exit code 0 if either path succeeds, 1 otherwise.
"""
import json
import subprocess
import sys
import tomllib
import urllib.request

CC_CONFIG = "/opt/homebrew/etc/cc-connect/config.toml"
CC_SOCK = None  # resolved from ~/
CC_BIN = "cc-connect"
PROJECT = "kimi-main"
CHUNK = 3000

import os
CC_SOCK = os.path.expanduser("~/.cc-connect/run/api.sock")
# last-known feishu chat id (oc_...), learned from cc-connect sessions or
# FEISHU_CHAT_ID env; gitignored, not a secret
CHAT_ID_FILE = os.environ.get(
    "FEISHU_CHAT_ID_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".feishu_chat_id"))


def try_cc_connect(text):
    """Returns True on success."""
    if not os.path.exists(CC_SOCK):
        print("cc-connect: socket missing", file=sys.stderr)
        return False
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "5", "--unix-socket", CC_SOCK,
             "http://localhost/sessions"],
            capture_output=True, text=True, timeout=10).stdout
        sessions = json.loads(out)
    except Exception as e:
        print(f"cc-connect: sessions query failed: {e}", file=sys.stderr)
        return False
    if not sessions:
        print("cc-connect: no active session", file=sys.stderr)
        return False
    key = sessions[0]["session_key"]
    # remember the chat id for the direct-API fallback (sessions are
    # idle-timed-out, but the underlying chat persists)
    parts = key.split(":")
    if parts[0] == "feishu" and len(parts) > 1 and parts[1].startswith("oc_"):
        with open(CHAT_ID_FILE, "w") as f:
            f.write(parts[1])
    r = subprocess.run([CC_BIN, "send", "-p", PROJECT, "-s", key, "--stdin"],
                       input=text, capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and "successfully" in (r.stdout + r.stderr).lower():
        print("cc-connect: sent")
        return True
    print(f"cc-connect: send failed: {r.stdout} {r.stderr}", file=sys.stderr)
    return False


def _feishu_credentials():
    with open(CC_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for proj in cfg.get("projects", []):
        if proj.get("name") != PROJECT:
            continue
        open_id = proj.get("admin_from")
        app_id = app_secret = None
        for plat in proj.get("platforms", []):
            if plat.get("type") == "feishu":
                opts = plat.get("options", {})
                app_id = opts.get("app_id")
                app_secret = opts.get("app_secret")
                open_id = plat.get("admin_from") or opts.get("admin_from") or open_id
                break
        if app_id and app_secret and open_id:
            return app_id, app_secret, open_id
    raise RuntimeError(f"no feishu credentials found for project {PROJECT} in {CC_CONFIG}")


def _post(url, payload, token=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def try_feishu_api(text):
    app_id, app_secret, open_id = _feishu_credentials()
    tok = _post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": app_id, "app_secret": app_secret})
    if tok.get("code") != 0:
        print(f"feishu token error: {tok}", file=sys.stderr)
        return False
    token = tok["tenant_access_token"]

    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    targets = [("open_id", open_id)]
    chat_id = os.environ.get("FEISHU_CHAT_ID")
    if not chat_id and os.path.exists(CHAT_ID_FILE):
        chat_id = open(CHAT_ID_FILE).read().strip()
    if chat_id:
        targets.append(("chat_id", chat_id))
    # proactive user messages are often blocked (code 230101); the chat with
    # the bot works — fall through targets until one accepts all chunks
    for id_type, receive_id in targets:
        ok = True
        for i, chunk in enumerate(chunks):
            resp = _post(
                f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={id_type}",
                {"receive_id": receive_id, "msg_type": "text",
                 "content": json.dumps({"text": chunk}, ensure_ascii=False)},
                token=token)
            if resp.get("code") != 0:
                print(f"feishu {id_type} chunk {i + 1}/{len(chunks)} error: "
                      f"{resp.get('code')} {resp.get('msg')}", file=sys.stderr)
                ok = False
                break
        if ok:
            print(f"feishu api ({id_type}): sent {len(chunks)} chunk(s)")
            return True
    return False


def main():
    import argparse
    global PROJECT
    p = argparse.ArgumentParser()
    p.add_argument("--file")
    p.add_argument("--project", default=PROJECT)
    args = p.parse_args()
    PROJECT = args.project

    text = open(args.file).read() if args.file else sys.stdin.read()
    if not text.strip():
        print("empty message", file=sys.stderr)
        sys.exit(1)

    if try_cc_connect(text):
        sys.exit(0)
    print("falling back to feishu open api", file=sys.stderr)
    sys.exit(0 if try_feishu_api(text) else 1)


if __name__ == "__main__":
    main()
