"""Instagram Graph API helpers: login, account discovery, publishing."""
import time
from urllib.parse import urlencode

import requests

GRAPH_VERSION = "v21.0"   # bump when Meta deprecates this version
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
VIDEO_EXT = (".mp4", ".mov")
SCOPES = ("instagram_basic,instagram_content_publish,pages_show_list,"
          "pages_read_engagement,business_management")


class GraphError(Exception):
    pass


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
        data = r.json() if r.content else {}
        if r.ok and "error" not in data:
            return data
        err = data.get("error", {})
        last = f"{err.get('error_user_msg') or err.get('message') or r.text} (code {err.get('code')})"
        if err.get("is_transient") or r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        raise GraphError(last)
    raise GraphError(f"Instagram did not respond: {last}")


# ------------------------------------------------------------- login
def login_url(app_id, redirect_uri, state, config_id=None):
    p = {"client_id": app_id, "redirect_uri": redirect_uri,
         "state": state, "response_type": "code"}
    if config_id:
        p["config_id"] = config_id          # Facebook Login for Business
    else:
        p["scope"] = SCOPES
    return f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{urlencode(p)}"


def code_to_token(app_id, app_secret, redirect_uri, code):
    d = requests.get(f"{BASE}/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code}, timeout=60).json()
    if "access_token" not in d:
        raise GraphError(d.get("error", {}).get("message", str(d)))
    return d["access_token"]


def long_lived(app_id, app_secret, token):
    d = requests.get(f"{BASE}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": token}, timeout=60).json()
    if "access_token" not in d:
        raise GraphError(d.get("error", {}).get("message", str(d)))
    return d["access_token"]


def list_ig_accounts(user_token):
    """Every Instagram account linked to the user's Facebook Pages."""
    out, skipped = [], []
    data = api("GET", "me/accounts", user_token, limit=100,
               fields="name,access_token,instagram_business_account"
                      "{id,username,profile_picture_url}")
    while True:
        for page in data.get("data", []):
            igb = page.get("instagram_business_account")
            if not igb:
                skipped.append(page["name"])
                continue
            out.append({"ig_user_id": igb["id"],
                        "username": igb.get("username") or page["name"],
                        "picture": igb.get("profile_picture_url", ""),
                        "page_name": page["name"],
                        "access_token": page["access_token"]})
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            break
        data = requests.get(nxt, timeout=60).json()
    return out, skipped


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
