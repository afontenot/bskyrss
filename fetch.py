import re
import sqlite3
from datetime import datetime, timezone

import requests
from flask import Flask, Response, abort, g, render_template

MAX_POST_FETCH_SECS = 86400
REFETCH_HANDLES_SECS = 86400 * 7
REFETCH_PROFILES_SECS = 86400 * 7
CACHE_NONEXISTENT_HANDLES_SECS = 86400
CACHE_POSTS_SECS = 3600
CACHE_DIR = "cache"
DATABASE = "bsky.db"

# constants
PROFILE_URL = "https://bsky.app/profile"
BSKY_PUBLIC_API = "https://public.api.bsky.app/xrpc"
VALID_HANDLE_REGEX = re.compile(r"^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

app = Flask(__name__)
iso = datetime.fromisoformat


class BskyXrpcClient:
    def __init__(self):
        self.s = requests.Session()

    def get_posts(self, actor, server_url=BSKY_PUBLIC_API, last=None):
        url = f"{server_url}/app.bsky.feed.getAuthorFeed"
        params = {
            "actor": actor,
            "filter": "posts_and_author_threads",
            "limit": 30,
        }

        now = datetime.now(timezone.utc)
        posts = {}
        while True:
            r = self.s.get(url, params=params)
            r.raise_for_status()
            earliest_post_date = now
            for post in (x["post"] for x in r.json()["feed"]):
                post_stub = post["uri"].split("/")[-1]
                data = {
                    "author": post["author"]["handle"],
                    "authorName": post["author"]["displayName"],
                    "date": iso(post["record"]["createdAt"]),
                    "text": post["record"]["text"],
                    # FIXME: improve this hardcoded link?
                    "url": f"{PROFILE_URL}/{post['author']['did']}/post/{post_stub}",
                }
                if "embed" in post:
                    data["embed"] = post["embed"]
                if "facets" in post["record"]:
                    data["facets"] = post["record"]["facets"]
                if data["date"] < now:
                    earliest_post_date = data["date"]
                posts[post["cid"]] = data

            if not last:
                return posts

            if (now - earliest_post_date).total_seconds() >= MAX_POST_FETCH_SECS:
                return posts

            params.update(
                {"cursor": earliest_post_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
            )

    def get_actor(self, handle, server_url=BSKY_PUBLIC_API):
        url = f"{server_url}/com.atproto.identity.resolveHandle"
        params = {"handle": handle}
        r = self.s.get(url, params=params)
        r.raise_for_status()
        return r.json()["did"]

    def get_profile(self, actor, server_url=BSKY_PUBLIC_API):
        url = f"{server_url}/app.bsky.actor.getProfile"
        params = {
            "actor": actor,
        }
        r = self.s.get(url, params=params)
        r.raise_for_status()
        return r.json()


def post_to_html(post):
    segments = []
    if "text" in post:
        text = post["text"]
        cursor = 0
        if "facets" in post:
            # FIXME: round-trip encoding sucks a lot, but is hard to avoid...
            btext = text.encode("utf-8")
            for facet in sorted(post["facets"], key=lambda x: x["index"]["byteStart"]):
                if facet["features"][0]["$type"] == "app.bsky.richtext.facet#link":
                    segments.append(
                        {
                            "type": "text",
                            "value": btext[cursor : facet["index"]["byteStart"]].decode(
                                "utf-8", "surrogateescape"
                            ),
                        }
                    )
                    url = facet["features"][0]["uri"]
                    link_text = btext[
                        facet["index"]["byteStart"] : facet["index"]["byteEnd"]
                    ].decode("utf-8", "surrogateescape")
                    segments.append(
                        {
                            "type": "link",
                            "text": link_text,
                            "url": url,
                        }
                    )
                    cursor = facet["index"]["byteEnd"]
                elif facet["features"][0]["$type"] == "app.bsky.richtext.facet#mention":
                    segments.append(
                        {
                            "type": "text",
                            "value": btext[cursor : facet["index"]["byteStart"]].decode(
                                "utf-8", "surrogateescape"
                            ),
                        }
                    )
                    did = facet["features"][0]["did"]
                    url = f"{PROFILE_URL}/{did}"
                    link_text = btext[
                        facet["index"]["byteStart"] : facet["index"]["byteEnd"]
                    ].decode("utf-8", "surrogateescape")
                    segments.append(
                        {
                            "type": "link",
                            "text": link_text,
                            "url": url,
                        }
                    )
                    cursor = facet["index"]["byteEnd"]
            segments.append(
                {
                    "type": "text",
                    "value": btext[cursor:].decode("utf-8", "surrogateescape"),
                }
            )
        else:
            segments.append({"type": "text", "value": text})

    if "embed" in post:
        if post["embed"]["$type"] == "app.bsky.embed.images#view":
            for image in post["embed"]["images"]:
                alt = image["alt"]
                src = image["thumb"]
                segments.append(
                    {
                        "type": "image",
                        "url": src,
                        "alt": alt,
                    }
                )
        elif post["embed"]["$type"] == "app.bsky.embed.video#view":
            segments.append(
                {
                    "type": "video",
                    "thumbnail": post["embed"]["thumbnail"],
                    "playlist": post["embed"]["playlist"],
                }
            )
        elif post["embed"]["$type"] == "app.bsky.embed.external#view":
            segments.append(
                {
                    "type": "extlink",
                    "thumbnail": post["embed"]["external"].get("thumb", ""),
                    "url": post["embed"]["external"]["uri"],
                    "text": post["embed"]["external"]["title"],
                    "description": post["embed"]["external"]["description"],
                }
            )

    return render_template("post.html", segments=segments)


def actorfeed(actor: str):
    client = get_client()

    # check last time actor feed was updated
    conn = get_db()
    curs = conn.cursor()
    res = curs.execute("SELECT * FROM profiles WHERE did = ?", (actor,))
    res = res.fetchone()
    now = datetime.now(timezone.utc)
    fetched = None

    # we know about this actor already
    if res:
        profile = {
            "did": res[0],
            "handle": res[1],
            "name": res[2],
            "avatar": res[3],
            "description": res[4],
            "updated": res[5],
            "fetched": res[6],
        }

        # if updated less than an hour ago, return cached file
        if (
            profile["fetched"]
            and (now - iso(profile["fetched"])).total_seconds() < CACHE_POSTS_SECS
        ):
            try:
                # FIXME: a 302 to NGINX cache would be more efficient
                with open(f"{CACHE_DIR}/{actor}.atom.xml", "rb") as f:
                    print("returning cached feed for", actor)
                    return f.read()
            except FileNotFoundError:
                pass

    # never fetched before, verify actor and fetch posts
    if not res or (now - iso(profile["updated"])).total_seconds() > REFETCH_PROFILES_SECS:
        try:
            print("fetching profile for", actor)
            profile = client.get_profile(actor)
        except requests.HTTPError:
            return

        author = profile["displayName"]
        avatar = profile["avatar"]
        description = profile["description"]
        profile = {
            "did": actor,
            "handle": profile["handle"],
            "name": author,
            "avatar": avatar,
            "description": description,
            "updated": now,
            "fetched": None,
        }
        curs.execute(
            "INSERT OR REPLACE INTO profiles VALUES(:did, :handle, :name, :avatar, :description, :updated, :fetched)",
            profile,
        )
        conn.commit()

    try:
        posts = client.get_posts(actor, last=fetched)
    except requests.HTTPError:
        return

    if not posts:
        return

    curs.execute("UPDATE profiles SET fetched = ? WHERE did = ?", (now, actor))

    for cid, post in posts.items():
        # FIXME: consider updating edited posts
        postdata = curs.execute("SELECT EXISTS(SELECT 1 FROM posts WHERE cid = ?)", (cid,))
        postdata = postdata.fetchone()
        if not postdata[0]:
            html = post_to_html(post)
            data = {
                "cid": cid,
                "did": actor,
                "url": post["url"],
                "html": html,
                "date": post["date"],
                "handle": post["author"],
                "name": post["authorName"],
            }
            curs.execute(
                "INSERT INTO posts VALUES(:cid, :did, :url, :html, :date, :handle, :name)",
                data,
            )
            conn.commit()

    posts = curs.execute(
        "SELECT * FROM posts WHERE did = ? ORDER BY date DESC LIMIT 100", (actor,)
    )
    posts = posts.fetchall()

    posts_data = []

    for post in posts:
        posts_data.append(
            {
                "cid": post[0],
                "url": post[2],
                "html": post[3],
                "date": post[4],
                "author": post[6],
            }
        )

    ofs = render_template("atom.xml", profile=profile, posts=posts_data).encode("utf-8")

    with open(f"{CACHE_DIR}/{actor}.atom.xml", "wb") as f:
        f.write(ofs)

    return ofs


def handlefeed(handle):
    conn = get_db()
    curs = conn.cursor()
    res = curs.execute("SELECT did,updated FROM handles WHERE handle = ?", (handle,))
    res = res.fetchone()
    now = datetime.now(timezone.utc)

    if res:
        actor, updated = res
        if not actor:
            if (now - iso(updated)).total_seconds() < CACHE_NONEXISTENT_HANDLES_SECS:
                return
                #raise ValueError("requested cached non-existent handle too soon")

    if not res or (now - iso(updated)).total_seconds() > REFETCH_HANDLES_SECS:
        try:
            actor = get_client().get_actor(handle)
            updated = now
        except requests.HTTPError:
            return

        if actor:
            data = {"actor": actor, "handle": handle, "now": now}
            curs.execute("INSERT OR REPLACE INTO handles VALUES(:handle, :actor, :now)", data)
            conn.commit()
        else:
            data = {"handle": handle, "now": now}
            curs.execute("INSERT OR REPLACE INTO handles VALUES(:handle, :now)", data)
            conn.commit()

    if actor:
        return actorfeed(actor)


def get_client():
    client = getattr(g, "_client", None)
    if client is None:
        client = g._client = BskyXrpcClient()
    return client


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        with open("bsky.schema") as f:
            schema = f.read()
        db.executescript(schema)
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


@app.route("/handle/<handle>")
def handle(handle):
    if len(handle) > 253 or not VALID_HANDLE_REGEX.match(handle):
        abort(404)
    feed = handlefeed(handle)
    if feed is None:
        abort(404)
    return Response(feed, mimetype="application/atom+xml")


@app.route("/actor/<actor>")
def actor(actor):
    if len(actor) != 32 or not actor.startswith("did:plc:") or not actor[8:].isalnum():
        abort(404)
    feed = actorfeed(actor)
    if feed is None:
        abort(404)
    return Response(feed, mimetype="application/atom+xml")
