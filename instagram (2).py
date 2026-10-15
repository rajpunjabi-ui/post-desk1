"""Instagram API with Instagram Login: connect accounts and publish (no Facebook Page needed)."""
import time
from urllib.parse import urlencode

import requests

GRAPH_VERSION = "v25.0"   # bump when Meta deprecates this version
BASE = f"https://graph.instagram.com/{GRAPH_VERSION}"
VIDEO_EXT = (".mp4", ".mov")
SCOPES = "instagram_business_basic,instagram_business_content_publish"


class GraphError(Exception):
    pass


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {"error_message": r.text}


def _errmsg(d):
    e = d.get("error")
    if isinstance(e, dict):
        return e.get("error_user_msg") or e.get("message") or str(e)
    return d.get("error_message") or str(e or d)


def api(method, path, token, **params):
    params["access_token"] = token
    url = path if path.startswith("http") else f"{BASE}/{path}"
    last = ""
    for attempt in range(3):
        try:
            if method == "GET":
                r = requests.get(url, params=params, timeout=60)
            else:
                r = requests.post(url, data=params, timeout=60)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
            continue
        data = _json(r)
        if r.ok and "error" not in data:
            return data
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        last = f"{_errmsg(data)} (code {err.get('code')})"
        if err.get("is_transient") or r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        raise GraphError(last)
    raise GraphError(f"Instagram did not respond: {last}")


# ------------------------------------------------------------- Instagram login
def login_url(app_id, redirect_uri, state):
    return "https://www.instagram.com/oauth/authorize?" + urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "state": state, "force_reauth": "true"})


def code_to_token(app_id, app_secret, redirect_uri, code):
    d = _json(requests.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": app_id, "client_secret": app_secret,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
        "code": code.split("#")[0]}, timeout=60))
    if isinstance(d.get("data"), list) and d["data"]:
        d = d["data"][0]
    if "access_token" not in d:
        raise GraphError(_errmsg(d))
    return d["access_token"]


def long_lived(app_secret, token):
    """Short-lived (1 hour) token -> long-lived (60 days). Returns (token, seconds)."""
    d = _json(requests.get("https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": app_secret,
        "access_token": token}, timeout=60))
    if "access_token" not in d:
        raise GraphError(_errmsg(d))
    return d["access_token"], int(d.get("expires_in", 5184000))


def refresh(token):
    """Extend a long-lived token by another 60 days. Returns (token, seconds)."""
    d = _json(requests.get("https://graph.instagram.com/refresh_access_token", params={
        "grant_type": "ig_refresh_token", "access_token": token}, timeout=60))
    if "access_token" not in d:
        raise GraphError(_errmsg(d))
    return d["access_token"], int(d.get("expires_in", 5184000))


def account_info(token):
    d = api("GET", "me", token, fields="user_id,username,profile_picture_url,account_type")
    return {"ig_user_id": str(d.get("user_id") or d["id"]),
            "username": d.get("username", ""),
            "picture": d.get("profile_picture_url", ""),
            "account_type": d.get("account_type", "")}


def quota(ig_user_id, token):
    q = api("GET", f"{ig_user_id}/content_publishing_limit", token,
            fields="quota_usage,config")
    d = (q.get("data") or [{}])[0]
    return d.get("quota_usage", 0), d.get("config", {}).get("quota_total", "?")


# ------------------------------------------------------------- publishing
def is_video(url):
    return url.lower().split("?")[0].endswith(VIDEO_EXT)


def wait_until_ready(container_id, token, timeout=900):
    start = time.time()
    while time.time() - start < timeout:
        s = api("GET", container_id, token, fields="status_code,status")
        code = s.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise GraphError(f"Instagram couldn't process the media: {s.get('status')}")
        time.sleep(10)
    raise GraphError("Timed out waiting for Instagram to process the media")


def _container(uid, tok, ptype, urls, caption, cover_url):
    if ptype == "carousel":
        children = []
        for u in urls:
            p = {"is_carousel_item": "true"}
            if is_video(u):
                p.update(media_type="VIDEO", video_url=u)
            else:
                p["image_url"] = u
            cid = api("POST", f"{uid}/media", tok, **p)["id"]
            wait_until_ready(cid, tok)
            children.append(cid)
        return api("POST", f"{uid}/media", tok, media_type="CAROUSEL",
                   children=",".join(children), caption=caption)["id"]
    url = urls[0]
    if ptype == "image":
        p = {"image_url": url, "caption": caption}
    elif ptype == "reel":
        p = {"media_type": "REELS", "video_url": url, "caption": caption,
             "share_to_feed": "true"}
        if cover_url:
            p["cover_url"] = cover_url
    elif ptype == "story":
        p = {"media_type": "STORIES"}
        p["video_url" if is_video(url) else "image_url"] = url
    else:
        raise GraphError(f"Unknown post type: {ptype}")
    return api("POST", f"{uid}/media", tok, **p)["id"]


def publish(ig_user_id, token, ptype, urls, caption="", cover_url=""):
    cid = _container(ig_user_id, token, ptype, urls, caption, cover_url)
    wait_until_ready(cid, token)
    media_id = api("POST", f"{ig_user_id}/media_publish", token, creation_id=cid)["id"]
    try:
        link = api("GET", media_id, token, fields="permalink").get("permalink", "")
    except GraphError:
        link = ""
    return media_id, link
