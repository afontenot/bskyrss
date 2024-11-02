# Bluesky to RSS bridge

Bluesky has built in support for RSS feeds on profiles, but these are
pretty much an afterthought.

 * They do not support showing images or videos in posts
 * Links do not work (only the text is shown)
 * External link previews are missing
 * Bluesky doesn't generate an RSS feed for any profile that has the
"require sign-in" option set, even though the full content of every post
is trivially available in JSON form to the public over an open protocol.

This project is intended to solve all of these problems.

There is a demo instance that you may use at
https://bskyrss.liliane.io/. You can subscribe to a Atom feed for any
profile at `https://bskyrss.liliane.io/handle/<fully.qualified.handle>`.

Please note that the demo instance limits downloading new posts to once
per hour - avoid excessively rescraping the feed as it won't get you new
posts any faster. You can always download posts yourself in their
original form using the open API.

## Running the software

This is a Flask application. You should run it behind a reverse proxy
using a WSGI frontend like uwsgi. If you have flask installed locally,
you can run it on your own machine for testing and development purposes
with `flask --app fetch.py run`.

The only dependencies (other than Python itself) are Flask and Requests.

## Limitations

 * Content warnings are not rendered.
 * Posts once downloaded are never updated or deleted.
 * The approach to caching is not very efficient. The best approach to
improving this software would probably be to directly translate it to
Go. It was mostly written on a single weekend so that I could follow
artists who release webcomics via Bluesky.
 * There are undeniably elements of this that are hacked together. I
didn't read more of the very lengthy set of specifications than I had to.
If there's stuff that just doesn't work or corner cases you care about,
please file an issue!

## License

This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
