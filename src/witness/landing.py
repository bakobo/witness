"""``witness landing``: a static landing page from what a witness declares (@78m6fs3g).

A browser that opens a witness's hostname gets keripy's bare 405 at ``/``. This renders the page an
operator can serve there instead. It is a pure function from bytes to bytes: the input is one JSON
document holding exactly what ``/v1/witness/identity``, ``/tags`` and ``/attribs`` return, plus the
hostname, and the output is one complete HTML file. It opens no port, makes no network call and
never touches the database. The operator's deploy fetches the three documents once and serves the
file, so nothing a visitor does reaches hio, whose single thread is the asset this design protects.

Every value is HTML-escaped, and the two link attribs are re-checked as https URLs (@3syf5w8x) even
though the control plane already refused anything else, so the page stays script-free without
depending on a door it did not run. Branding is CSS and an SVG logo, inlined. CSS that could fetch
anything or end its own <style> element is refused rather than sanitised, because a sanitiser is a
promise about a grammar and a refusal is a promise about a rule.

Usage, with the document assembled by the operator's deploy:

    witness landing --css brand.css --logo logo.svg \\
        --siblings https://example.com/witnesses/ < declared.json > index.html

where declared.json is {"hostname": ..., "identity": {...}, "tags": {...}, "attribs": {...}}.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from html import escape
from importlib import resources
from urllib.parse import urlsplit

from keri import kering
from keri.core import coring

from .errors import LandingInput, LandingMissing, LandingTooLarge, LandingUnsafe, WitnessError

#: Flood guards, far past a real brand (a few KB of CSS, a 10 KB logo) and far below anything a
#: visitor would notice on a page whose whole job is to be small.
MAX_CSS_BYTES = 64 * 1024
MAX_LOGO_BYTES = 64 * 1024
#: Repeatable --css is bounded in count as well as in total size, which MAX_CSS_BYTES applies to.
MAX_CSS_FILES = 8
#: The whole declaration document. The control plane bounds a witness to 16 tags and 16 attribs of
#: at most 256 characters, so a real document is a few kilobytes.
MAX_DOCUMENT_BYTES = 64 * 1024

#: One DNS label: letters, digits and inner hyphens, at most 63 characters.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
#: A tag name in the shape decls.py admits: lowercase dot-separated segments.
_TAG = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*")

#: The tag that changes what a visitor should conclude, so it is shown as a notice, not a list item.
TESTNET = "testnet"

#: The attribs shown as links. Everything else is text, contact included: a bare email address is
#: a legitimate contact, and a mailto: link is one more URL scheme to reason about.
LINK_ATTRIBS = ("terms", "registration")

#: Display order and labels for the defined vocabulary. Vendor-prefixed keys follow, sorted.
LABELS = {
    "operator": "Operator",
    "contact": "Contact",
    "pool": "Pool",
    "terms": "Terms of service",
    "registration": "Registration",
}

# Each pattern is a way a stylesheet can make the browser fetch something or leave the style
# element. Case-insensitive because CSS keywords are. A backslash is refused outright because CSS
# escapes let any of the others be spelled so that no pattern sees them: `u\72 l(` is url(.
_CSS_REFUSALS = (
    (re.compile(r"\\"), "a backslash, which lets a CSS escape spell any of the other refusals"),
    (re.compile(r"<"), "a '<', which could end the style element the CSS is inlined into"),
    (re.compile(r"@import", re.I), "an @import, which fetches another stylesheet"),
    (re.compile(r"url\((?!\s*[\"']?\s*data:)", re.I), "a url() that is not a data: URI"),
    (re.compile(r"image-set\(", re.I), "an image-set(), which fetches by bare string"),
)

#: A CSS comment. Comments do not nest, so the shortest match is exact.
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def default_css() -> str:
    """The neutral stylesheet shipped with the package, styled for the ``wl-`` class prefix."""
    return resources.files("witness").joinpath("landing.css").read_text(encoding="utf-8")


def admit_css(name: str, text: str) -> str:
    """Return ``text`` without comments if it can neither fetch nor escape its element.

    Comments are removed BEFORE the checks, and the stripped text is what gets inlined, so the
    checks see exactly what the browser will. That ordering is what makes removal safe: a comment
    splitting a keyword, as in ``u/**/rl(``, is joined up before anything looks for ``url(``.
    """
    if len(text.encode("utf-8")) > MAX_CSS_BYTES:
        raise LandingTooLarge(
            f"The stylesheet {name} is larger than {MAX_CSS_BYTES} bytes. A landing page needs a "
            "fraction of that, so this is more likely the wrong file than a large brand."
        )
    text = _CSS_COMMENT.sub("", text)
    for pattern, what in _CSS_REFUSALS:
        if pattern.search(text):
            raise LandingUnsafe(
                f"The stylesheet {name} contains {what}. Landing-page CSS is inlined and may not "
                "reach anything outside the page; put fonts and images in as data: URIs, or "
                "leave them out."
            )
    return text


def admit_link(key: str, value: object) -> str:
    """Return ``value`` if it is an absolute https URL with a plain host, or refuse it."""
    if not isinstance(value, str):
        raise LandingUnsafe(f"The attribute {key!r} is not text, so it cannot be a link.")
    try:
        parts = urlsplit(value)
        # Read for its side effect: urlsplit is lazy about the port and raises only on access.
        parts.port
    except ValueError:
        # An unclosed IPv6 literal, or a port that is not a number in range.
        parts = None
    if (
        parts is None
        or not value.startswith("https://")
        or not parts.hostname
        or "@" in parts.netloc
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        raise LandingUnsafe(
            f"The attribute {key!r} is not an absolute https URL naming a host, so the page will "
            "not link to it. Correct the declaration rather than the page."
        )
    return value


def _member(document: dict, name: str, inner: str, kind: type) -> object:
    """``document[name][inner]``, checked to be a ``kind``, or a refusal naming the path."""
    outer = document.get(name)
    value = outer.get(inner) if isinstance(outer, dict) else None
    if not isinstance(value, kind):
        raise LandingInput(
            f"The declaration document has no {kind.__name__} at {name}.{inner}. Pipe in the "
            f"control plane's /v1/witness/{name} response unchanged."
        )
    return value


def _is_hostname(value: object) -> bool:
    """A lowercase DNS name of at least two labels, at most 253 characters in all."""
    if not isinstance(value, str) or len(value) > 253:
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(_LABEL.fullmatch(label) for label in labels)


def _is_witness_aid(value: str) -> bool:
    """Whether keripy parses ``value`` as a non-transferable prefix, which every witness AID is.

    keripy rather than a regex, because the shape alone -- 44 URL-safe characters -- admits strings
    no KERI software would resolve, and the page tells its reader to resolve this one.
    """
    try:
        prefixer = coring.Prefixer(qb64=value)
    except (kering.KeriError, ValueError, TypeError):
        return False
    # Prefixer reads the code's full size and ignores what follows, so a trailing character would
    # otherwise pass and then land in the OOBI link.
    return prefixer.qb64 == value and not prefixer.transferable


def _logo_tag(logo: bytes | None) -> str:
    if logo is None:
        return ""
    if len(logo) > MAX_LOGO_BYTES:
        raise LandingTooLarge(
            f"The logo is larger than {MAX_LOGO_BYTES} bytes, which is past anything a masthead "
            "needs."
        )
    # An <img> rather than inline SVG: script inside an SVG never runs when it is loaded as an
    # image, so the logo file needs no inspection at all.
    encoded = base64.b64encode(logo).decode("ascii")
    return f'<img src="data:image/svg+xml;base64,{encoded}" alt="" height="30">'


def _attrib_rows(attribs: dict) -> list[str]:
    defined = [key for key in LABELS if key in attribs]
    vendor = sorted(key for key in attribs if key not in LABELS)
    rows = []
    for key in defined + vendor:
        value = attribs[key]
        label = escape(LABELS.get(key, key))
        if key in LINK_ATTRIBS:
            href = escape(admit_link(key, value))
            rows.append(f"<dt>{label}</dt><dd><a href=\"{href}\">{href}</a></dd>")
        else:
            rows.append(f"<dt>{label}</dt><dd>{escape(str(value))}</dd>")
    return rows


def render(
    declared: dict,
    css: list[tuple[str, str]],
    logo: bytes | None = None,
    siblings: str | None = None,
    class_prefix: str = "wl-",
) -> str:
    """The landing page as a complete HTML document.

    ``css`` is a list of (name, text) pairs inlined in order. ``class_prefix`` is glued to every
    class the page uses; config.py has already checked it is a lowercase name and a hyphen.
    """
    if not isinstance(declared, dict):
        raise LandingInput("The declaration document must be a JSON object.")
    hostname = declared.get("hostname")
    if not _is_hostname(hostname):
        raise LandingInput(
            "The declaration document needs the witness's hostname: lowercase DNS labels joined by "
            "dots, at least two of them, as in 'w.example.com'. The OOBI link is built from it."
        )
    aid = _member(declared, "identity", "aid", str)
    if not _is_witness_aid(aid):
        raise LandingInput(
            "The identity's aid is not a non-transferable qb64 identifier, which a witness's always "
            "is, so the page will not build an OOBI link from it."
        )
    tags = _member(declared, "tags", "tags", list)
    if not all(isinstance(tag, str) and _TAG.fullmatch(tag) for tag in tags):
        raise LandingInput("Every tag must be a lowercase tag name, as /v1/witness/tags returns them.")
    attribs = _member(declared, "attribs", "attribs", dict)
    signed = declared["attribs"].get("source") == "signed-reply"
    styles = "\n".join(admit_css(name, text) for name, text in css)
    sibling_link = admit_link("siblings", siblings) if siblings is not None else None

    c = class_prefix
    host = escape(hostname)
    oobi = escape(f"https://{hostname}/oobi/{aid}/controller")
    operator = attribs.get("operator")

    notices = []
    if TESTNET in tags:
        notices.append(
            f'<p class="{c}notice__code">testnet</p>'
            "<p><strong>This witness declares itself a test witness.</strong> It belongs to an "
            "experimental, test or demonstration environment, and nothing with real-world "
            "consequence should depend on it.</p>"
        )
    if "registration" in attribs:
        notices.append(
            "<p>Its operator offers registration to the controllers that use it. What registering "
            "involves, and what it changes, is explained at the registration link below.</p>"
        )

    other_tags = sorted(tag for tag in tags if tag != TESTNET)
    rows = "\n        ".join(
        [
            f"<dt>Host</dt><dd>{host}</dd>",
            f"<dt>Identifier (AID)</dt><dd><code>{escape(aid)}</code></dd>",
            f'<dt>OOBI</dt><dd><a href="{oobi}">{oobi}</a></dd>',
            *_attrib_rows(attribs),
            *([f"<dt>Tags</dt><dd>{escape(', '.join(other_tags))}</dd>"] if other_tags else []),
        ]
    )
    # Attributed, never asserted: this function holds no signature, only the control plane's word
    # that keripy verified one. So the page reports that word and points at the check.
    provenance = (
        "Its control plane reports the operator details above as signed by its own key, and "
        "resolving the OOBI is how to check that."
        if signed
        else "The operator details above come from the witness's configuration and are not yet "
        "signed by its key."
    )
    siblings_paragraph = (
        f'<p>Other witnesses run by the same operator, and what running all of them means for the '
        f'assurance they give, are listed at <a href="{escape(sibling_link)}">'
        f"{escape(sibling_link)}</a>.</p>"
        if sibling_link
        else ""
    )
    by = escape(str(operator)) if operator else "its operator"
    footer = escape(str(operator)) if operator else host

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="{host} is a KERI witness operated by {by}.">
  <title>{host} — KERI witness</title>
  <style>
{styles}
  </style>
</head>
<body>
  <div class="{c}page">
    <header class="{c}masthead">
      {_logo_tag(logo)}
      <span class="{c}masthead__label">KERI witness</span>
    </header>
    <main class="{c}main">
      <h1>{host} is a KERI witness.</h1>
      <p class="{c}lede">A witness receives key events from the controllers that chose it,
      checks them, and signs a receipt for each, so that anyone can later confirm the events were
      published and not rewritten. It is run by {by}. There is nothing here for a browser to do;
      software talks to this host over KERI.</p>
      {"".join(notices)}
      <dl>
        {rows}
      </dl>
      <h2>Do not trust this page</h2>
      <p>Nothing on this page is authoritative, including the identifier. The way to know this
      witness is the one a key event names is to resolve the OOBI above with KERI software, which
      checks the witness's own signature. {provenance}</p>
      {siblings_paragraph}
    </main>
    <footer class="{c}footer">
      <p>{footer}</p>
    </footer>
  </div>
</body>
</html>
"""


def _read_bounded(path: str, limit: int, what: str) -> bytes:
    """At most ``limit`` bytes of ``path``, refusing a larger file without reading the rest of it.

    The size checks in admit_css and _logo_tag would come too late on their own: by then a
    misconfigured path naming a large file has already been read whole into memory.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise LandingMissing(
            f"A branding file could not be read ({exc.filename or path}). Check the --css and "
            "--logo paths name files that exist."
        ) from exc
    if len(data) > limit:
        raise LandingTooLarge(f"The {what} must fit in {limit} bytes, and {path} does not.")
    return data


def _no_repeats(pairs):
    """An object hook that refuses a repeated member, which json.loads would resolve last-one-wins.

    Last-one-wins would let a document carrying "tags": ["testnet"] and then "tags": [] render a
    page with no testnet notice, and nothing about the output would say so.
    """
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise LandingInput(
                f"The declaration document repeats the member {name!r}, and which one wins would "
                "otherwise depend on the order the bytes happen to be in."
            )
        seen[name] = value
    return seen


def _page(cfg, source) -> str:
    # Bytes, not characters: the bound is on what crossed the boundary, and four-byte UTF-8 would
    # otherwise let a document four times the limit through.
    raw = source.buffer.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise LandingTooLarge(
            f"The declaration document is larger than {MAX_DOCUMENT_BYTES} bytes, far past "
            "anything a witness's control plane returns."
        )
    try:
        declared = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_repeats)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LandingInput(f"The declaration document is not valid UTF-8 JSON: {exc}.") from exc
    if cfg.css:
        css, budget = [], MAX_CSS_BYTES
        for path in cfg.css:
            data = _read_bounded(path, budget, "stylesheets, together,")
            budget -= len(data)
            try:
                css.append((path, data.decode("utf-8")))
            except UnicodeError as exc:
                raise LandingMissing(f"The stylesheet {path} is not UTF-8 text.") from exc
    else:
        css = [("the default stylesheet", default_css())]
    logo = _read_bounded(cfg.logo, MAX_LOGO_BYTES, "logo") if cfg.logo else None
    return render(declared, css, logo=logo, siblings=cfg.siblings, class_prefix=cfg.class_prefix)


def run(cfg, stdin=None) -> int:
    """Write the page, or the default stylesheet, to stdout; 0 on success, 1 on a refusal.

    A refusal prints ``<code>: <detail>`` to stderr and writes nothing to stdout, so a deploy that
    redirects stdout into place never installs half a page.
    """
    if cfg.print_default_css:
        sys.stdout.write(default_css())
        return 0
    try:
        page = _page(cfg, stdin if stdin is not None else sys.stdin)
    except WitnessError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(page)
    return 0
