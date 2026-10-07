# A landing page for a witness

A browser that opens a witness's hostname gets keripy's bare `405` at `/`. `witness landing` renders a static page an operator can serve there instead, saying what the witness is, who runs it, where its terms are, and how to verify it through its OOBI. The decision is `this.i` `@78m6fs3g`; this document is derived from it.

## What it is, and what it is not

It is a pure function from bytes to bytes. The input is one JSON document on stdin, holding the hostname and exactly what the control plane returns for `/v1/witness/identity`, `/v1/witness/tags` and `/v1/witness/attribs`:

```json
{"hostname": "w.example.com",
 "identity": {"aid": "B…", "alias": "witness"},
 "tags": {"tags": [], "source": "seed-file"},
 "attribs": {"attribs": {"operator": "Example", "terms": "https://example.com/terms"}, "source": "seed-file"}}
```

The output is one complete HTML file on stdout. The command opens no port, makes no network call and never touches the database. Rendering happens when you deploy, not when someone visits: your deploy fetches the three documents once, runs this, and puts the file where your web server serves `GET /`. A visitor therefore never causes work in the witness, whose single-threaded loop stalls every controller when anything blocks it.

The page is not served by the witness, and should not be. Serve it from the reverse proxy that already terminates TLS, for `GET` and `HEAD` on `/` only. `POST` and `PUT` on `/` are keripy's event ingest, so a rule that answers them with the page drops every event sent there and reports success.

## Running it

From the image, offline:

```sh
docker run --rm -i --network none --entrypoint witness ghcr.io/bakobo/witness@sha256:… \
    landing < declared.json > index.html.new && mv index.html.new index.html
```

Render to a new file and rename it into place only on success. Redirecting straight onto the served file truncates it before the renderer runs, so a refusal would leave an empty page and even a success has a moment of serving one. The rename is atomic within a directory.

Serve the file with a strict Content-Security-Policy. The page carries no script and fetches nothing, so this is enough:

```
default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

## Branding it

Without `--css`, the page carries a neutral stylesheet built on a handful of CSS custom properties (`--wl-bg`, `--wl-text`, `--wl-accent` and so on). `witness landing --print-default-css > brand.css` writes it out. Change the properties, or anything else, and pass the result back:

```sh
witness landing --css brand.css --logo logo.svg --siblings https://example.com/witnesses/ \
    < declared.json > index.html.new && mv index.html.new index.html
```

- `--css` is repeatable and inlined in order, and it replaces the default stylesheet. Rules that could make the browser fetch something or leave the `<style>` element are refused: `@import`, any `url()` that is not a `data:` URI, `image()` and `image-set()`, a backslash (CSS escapes can spell the others), and `<`. Comments are stripped first. Fonts must be system fonts or `data:` URIs.
- `--logo` takes an SVG, embedded as an `<img>` data URI, so any script in it never runs.
- `--class-prefix` renames every class on the page (default `wl-`), so a stylesheet you already have fits without being rewritten. The classes are `page`, `masthead`, `masthead__label`, `main`, `lede`, `notice__code` and `footer`.
- `--siblings` links a page listing your other witnesses. Use it only where that page says plainly that one operator runs all of them, because a list of links otherwise implies an independence that does not exist.

## What the page shows

It shows the host, the AID, the OOBI, and every declared attribute, with `terms` and `registration` as links and everything else as text. A `testnet` tag becomes a notice, and other tags are listed. It never shows the version, the keripy pin, or any loop, escrow or database figure: the first two map a host onto known bugs, and the rest are an attacker's feedback loop. It says whether the declarations are signed by the witness's key or come from configuration, and tells the reader to verify through the OOBI rather than trust the page.

## Refusals

A refusal writes `<code>: <detail>` to stderr, writes nothing to stdout, and exits 1, so a deploy that redirects stdout into place never installs half a page.

| Code | When |
|---|---|
| `e.input.format.landing.f` | The input is not valid UTF-8 JSON, or not shaped like the control plane's documents, or breaks a declaration rule the control plane enforces (a value that is not short printable ASCII, a malformed name, a `terms` or `registration` that is not an https link; a bare name this release does not define passes only in a signed declaration, which may come from a newer vocabulary, and `production` never passes): a repeated or missing member, an AID keripy does not parse as non-transferable, a hostname that is not a DNS name, a tag that is not a tag name. |
| `e.input.range.landing.f` | The input document, the stylesheets together, or the logo is over its bound (64 KiB each). |
| `e.input.missing.landing.f` | A `--css` or `--logo` file cannot be read, or a stylesheet is not UTF-8. |
| `e.rule.landing.unsafe.f` | A stylesheet could fetch or escape, or `--siblings` is not an absolute https URL with a host. |
