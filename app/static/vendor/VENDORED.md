# Vendored browser libraries

Committed in-tree so the interface loads nothing from a CDN at runtime: the only
outbound requests the app makes are to the literature APIs.

- `marked.min.js` — marked 12.0.2, MIT.
  Upstream: https://github.com/markedjs/marked/releases/tag/v12.0.2
- `purify.min.js` — DOMPurify 3.1.6, Apache-2.0 **or** MPL-2.0 (your choice).
  Upstream: https://github.com/cure53/DOMPurify/releases/tag/3.1.6

Full licence texts are in `LICENSE-marked.txt` and `LICENSE-DOMPurify.txt`,
copied verbatim from those releases.

Both are unmodified upstream minified builds. To update one: replace the build
from the upstream release, copy that release's licence file over the one here,
and change the version above. Do not patch them in place.
