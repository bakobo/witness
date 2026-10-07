"""``witness landing``: a static landing page from what a witness declares (@78m6fs3g).

Most of these are refusals, because the page's value is what it will not do: fetch anything, run
anything, or link anywhere but https. Moved here from bakobo/infra's landing/render.py, whose
hostile and Copilot review findings are kept as regressions below.
"""

from __future__ import annotations

import io
import json
import re

import pytest

from witness import cli, landing
from witness.config import LandingConfig
from witness.errors import (
    InvalidArguments,
    LandingInput,
    LandingMissing,
    LandingTooLarge,
    LandingUnsafe,
)

AID = "BIen8GwnCDRSFJ7osngxZcNaGSiX0TuKLN2dKmPR0IqN"
LOGO = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


def declared(tags=(), attribs=None, source="seed-file", hostname="ca.wit.example.com"):
    return {
        "hostname": hostname,
        "identity": {"aid": AID, "alias": "witness"},
        "tags": {"tags": list(tags), "source": source},
        "attribs": {"attribs": attribs if attribs is not None else {}, "source": source},
    }


def stdin(text):
    """A stdin whose .buffer carries the bytes, as the real one does."""
    return io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")


def run(args, document, capsys):
    """Run ``witness landing`` through the real CLI and return (exit code, stdout, stderr)."""
    code = cli.main(["landing", *args], stdin=stdin(document))
    out = capsys.readouterr()
    return code, out.out, out.err


class TestPage:
    def test_the_default_page_renders(self):
        page = landing.render(declared(attribs={"operator": "Example", "contact": "ops@example.com"}),
                              [("default", landing.default_css())], logo=LOGO,
                              siblings="https://example.com/witnesses/")
        assert page.startswith("<!DOCTYPE html>")
        assert "ca.wit.example.com is a KERI witness." in page
        assert AID in page
        assert f"https://ca.wit.example.com/oobi/{AID}/controller" in page
        assert "Example" in page and "ops@example.com" in page
        assert 'href="https://example.com/witnesses/"' in page
        assert "data:image/svg+xml;base64," in page
        assert "--wl-accent" in page, "the default stylesheet must be inlined"

    def test_the_page_carries_no_script_and_fetches_nothing(self):
        page = landing.render(declared(attribs={"operator": "Example"}),
                              [("default", landing.default_css())], logo=LOGO)
        assert "<script" not in page.lower()
        assert "<link" not in page.lower()
        assert not re.search(r"""(?:src|href)="(?!https://|data:)""", page)
        assert "@import" not in page

    def test_the_page_names_no_version_or_internals(self):
        page = landing.render(declared(), [])
        for leak in ("keripy", "version", "escrow", "sha-", "Ioflo"):
            assert leak not in page

    def test_the_class_prefix_names_every_class(self):
        page = landing.render(declared(tags=["testnet"]), [], class_prefix="bk-")
        classes = re.findall(r'class="([^"]+)"', page)
        assert classes and all(name.startswith("bk-") for name in classes)
        assert "bk-page" in classes and "bk-masthead" in classes

    def test_the_default_prefix_matches_the_default_stylesheet(self):
        css = landing.default_css()
        page = landing.render(declared(tags=["testnet"]), [])
        for name in set(re.findall(r'class="([^"]+)"', page)):
            assert f".{name}" in css, f"the default stylesheet does not style {name}"

    def test_testnet_is_a_notice_not_a_list_item(self):
        page = landing.render(declared(tags=["testnet", "example.lab"]), [])
        assert "declares itself a test witness" in page
        assert "<dt>Tags</dt><dd>example.lab</dd>" in page

    def test_no_testnet_no_notice_and_no_empty_tag_row(self):
        page = landing.render(declared(), [])
        assert "test witness" not in page
        assert "<dt>Tags</dt>" not in page

    def test_terms_and_registration_are_links(self):
        page = landing.render(
            declared(attribs={"terms": "https://example.com/terms",
                              "registration": "https://example.com/register"}), [])
        assert '<a href="https://example.com/terms">' in page
        assert '<a href="https://example.com/register">' in page
        assert "offers registration" in page
        # Registration explains how to register; it must not claim the witness refuses others.
        assert "only from" not in page

    def test_no_registration_no_registration_notice(self):
        assert "registration" not in landing.render(declared(), []).lower()

    def test_contact_is_text_even_when_it_is_a_url(self):
        page = landing.render(declared(attribs={"contact": "https://example.com/abuse"}), [])
        assert "<dd>https://example.com/abuse</dd>" in page

    def test_defined_keys_come_first_then_vendor_keys_sorted(self):
        page = landing.render(
            declared(attribs={"example.z": "1", "contact": "c", "example.a": "2", "operator": "o"}),
            [])
        order = [page.index(label) for label in
                 ("<dt>Operator</dt>", "<dt>Contact</dt>", "<dt>example.a</dt>", "<dt>example.z</dt>")]
        assert order == sorted(order)

    def test_values_are_escaped(self):
        page = landing.render(
            declared(attribs={"operator": "<script>alert(1)</script>", "example.x": '"><img src=x>'}),
            [])
        assert "<script>alert" not in page
        assert "&lt;script&gt;" in page
        assert '"><img' not in page

    def test_provenance_says_whether_the_details_are_signed(self):
        assert "not yet signed" in landing.render(declared(source="seed-file"), [])
        assert "reports the operator details above as signed" in landing.render(
            declared(source="signed-reply"), [])

    def test_a_signed_claim_is_attributed_to_the_control_plane_not_asserted(self):
        """The renderer holds no signature, only the control plane's word that one exists, so
        the page reports that word and sends the reader to the OOBI rather than vouching itself."""
        page = landing.render(declared(source="signed-reply"), [])
        assert "are signed by this witness's own key" not in page
        assert "resolving the OOBI" in page

    def test_the_operator_names_the_page_when_there_is_one(self):
        assert "run by Example" in landing.render(declared(attribs={"operator": "Example"}), [])
        assert "run by its operator" in landing.render(declared(), [])

    def test_no_siblings_no_siblings_paragraph(self):
        assert "Other witnesses" not in landing.render(declared(), [])

    def test_no_logo_no_image(self):
        assert "<img" not in landing.render(declared(), [])


class TestCss:
    def test_the_default_stylesheet_passes_its_own_rules(self):
        assert landing.admit_css("default", landing.default_css())

    def test_comments_are_stripped_before_the_checks(self):
        """Joined first, so a keyword split by a comment is still caught."""
        with pytest.raises(LandingUnsafe):
            landing.admit_css("x.css", "a { background: u/**/rl(https://evil.example/) }")

    def test_comments_do_not_reach_the_page(self):
        assert landing.admit_css("x.css", "/* <link> @import */ a { color: red }") == " a { color: red }"

    @pytest.mark.parametrize(
        "hostile",
        [
            "@import 'https://evil.example/x.css';",
            "@IMPORT url(x.css);",
            "a { background: url(https://evil.example/t.gif) }",
            "a { background: URL( 'https://evil.example/t.gif') }",
            "@font-face { src: url(/font.woff2) }",
            "a { background: image-set('https://evil.example/t.png' 1x) }",
            "a { background: u\\72 l(https://evil.example/) }",
            "a { content: '</style><script>alert(1)</script>' }",
        ],
    )
    def test_anything_that_fetches_or_escapes_is_refused(self, hostile):
        with pytest.raises(LandingUnsafe) as caught:
            landing.admit_css("brand.css", hostile)
        assert "brand.css" in str(caught.value)

    @pytest.mark.parametrize("ok", ["a { background: url(data:image/png;base64,AAAA) }",
                                    "a { background: url( 'data:image/svg+xml,x') }"])
    def test_a_data_uri_is_admitted(self, ok):
        assert landing.admit_css("x.css", ok) == ok

    def test_an_oversize_stylesheet_is_refused(self):
        with pytest.raises(LandingTooLarge):
            landing.admit_css("big.css", "a{}" * landing.MAX_CSS_BYTES)

    def test_a_refused_stylesheet_refuses_the_page(self):
        with pytest.raises(LandingUnsafe):
            landing.render(declared(), [("x.css", "@import 'y.css';")])


class TestLinks:
    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(1)",
            "data:text/html,<p>",
            "http://example.com/terms",
            "HTTPS://example.com/terms",
            "//example.com/terms",
            "https://",
            "https://example.com@evil.example/",
            "https://example.com/a b",
            "https://example.com/\x00",
            "https://example.com:bad/terms",
            "https://[::1",
            5,
        ],
    )
    def test_anything_but_https_with_a_host_is_refused(self, hostile):
        with pytest.raises(LandingUnsafe):
            landing.render(declared(attribs={"terms": hostile}), [])

    def test_the_siblings_link_is_held_to_the_same_rule(self):
        with pytest.raises(LandingUnsafe):
            landing.render(declared(), [], siblings="javascript:alert(1)")


class TestDocument:
    @pytest.mark.parametrize(
        "broken",
        [
            [],
            {"identity": {"aid": AID}, "tags": {"tags": []}, "attribs": {"attribs": {}}},
            {**declared(), "hostname": "CA.wit.example.com"},
            {**declared(), "hostname": "ca.wit.example.com/<x>"},
            {**declared(), "identity": {}},
            {**declared(), "identity": "B..."},
            {**declared(), "tags": {"tags": "testnet"}},
            {**declared(), "attribs": {"attribs": []}},
            {**declared(), "identity": {"aid": "../../ext/oobi"}},             # a path, not an AID
            {**declared(), "identity": {"aid": AID + "x"}},                    # wrong length
            {**declared(), "tags": {"tags": ["other", 4]}},                    # not all text
            {**declared(), "tags": {"tags": ["Not A Tag"]}},                   # not a tag's shape
            {**declared(), "identity": {"aid": "A" * 44}},                     # right shape, no code
            {**declared(), "identity": {"aid": "E" + "A" * 43}},               # transferable
            {**declared(), "hostname": ".."},                                   # not a hostname
            {**declared(), "hostname": "-a.example.com"},                       # label edge hyphen
            {**declared(), "hostname": "localhost"},                            # no dot
            {**declared(), "hostname": "a" * 64 + ".example.com"},              # label over 63
        ],
    )
    def test_a_document_not_shaped_like_the_control_plane_is_refused(self, broken):
        with pytest.raises(LandingInput):
            landing.render(broken, [])

    def test_an_oversize_logo_is_refused(self):
        with pytest.raises(LandingTooLarge):
            landing.render(declared(), [], logo=b"x" * (landing.MAX_LOGO_BYTES + 1))


class TestConfig:
    def test_the_defaults(self):
        from witness import config
        subcommand, cfg = config.parse_args(["landing"])
        assert subcommand == "landing"
        assert cfg == LandingConfig(css=(), logo=None, siblings=None, class_prefix="wl-",
                                    print_default_css=False)

    @pytest.mark.parametrize("prefix", ["", "Bk-", "bk", "bk_", "-bk-", "b k-", "1b-"])
    def test_a_class_prefix_that_is_not_a_name_and_a_hyphen_is_refused(self, prefix):
        from witness import config
        with pytest.raises(InvalidArguments):
            config.parse_args(["landing", "--class-prefix", prefix])

    def test_too_many_stylesheets_are_refused(self):
        from witness import config
        with pytest.raises(InvalidArguments):
            config.parse_args(["landing"] + ["--css", "a.css"] * (landing.MAX_CSS_FILES + 1))


class TestCommand:
    def test_renders_from_stdin_with_the_default_stylesheet(self, capsys):
        code, out, err = run([], json.dumps(declared()), capsys)
        assert code == 0 and err == ""
        assert "is a KERI witness." in out
        assert "--wl-accent" in out

    def test_named_stylesheets_replace_the_default(self, capsys, tmp_path):
        css = tmp_path / "brand.css"
        css.write_text(".bk-page { color: red }")
        logo = tmp_path / "logo.svg"
        logo.write_bytes(LOGO)
        code, out, _ = run(["--css", str(css), "--logo", str(logo), "--class-prefix", "bk-",
                            "--siblings", "https://example.com/w/"], json.dumps(declared()), capsys)
        assert code == 0
        assert ".bk-page { color: red }" in out
        assert "--wl-accent" not in out
        assert 'class="bk-page"' in out

    def test_print_default_css_writes_the_stylesheet_and_reads_nothing(self, capsys):
        code, out, _ = run(["--print-default-css"], "not json at all", capsys)
        assert code == 0
        assert out == landing.default_css()

    def test_invalid_json_is_a_coded_failure(self, capsys):
        code, out, err = run([], "{", capsys)
        assert code == 1 and out == ""
        assert err.startswith(LandingInput.code + ": ")

    def test_a_document_that_is_not_utf8_is_a_coded_failure(self, capsys):
        code = cli.main(["landing"], stdin=io.TextIOWrapper(io.BytesIO(b"\xff{}"), encoding="utf-8"))
        assert code == 1
        assert capsys.readouterr().err.startswith(LandingInput.code + ": ")

    def test_the_document_bound_counts_bytes_not_characters(self, capsys):
        """20,000 emoji are 20,000 characters and 80,000 bytes."""
        doc = json.dumps(declared(attribs={"example.x": "\U0001F600" * 20000}), ensure_ascii=False)
        assert len(doc) < landing.MAX_DOCUMENT_BYTES < len(doc.encode("utf-8"))
        code, out, err = run([], doc, capsys)
        assert code == 1 and out == ""
        assert err.startswith(LandingTooLarge.code + ": ")

    def test_a_refusal_prints_its_code_and_writes_no_page(self, capsys, tmp_path):
        bad = tmp_path / "bad.css"
        bad.write_text("@import 'x';")
        code, out, err = run(["--css", str(bad)], json.dumps(declared()), capsys)
        assert code == 1 and out == ""
        assert err.startswith(LandingUnsafe.code + ": ")

    @pytest.mark.parametrize("flag", ["--css", "--logo"])
    def test_a_missing_brand_file_is_a_coded_failure(self, capsys, flag):
        code, out, err = run([flag, "/does/not/exist"], json.dumps(declared()), capsys)
        assert code == 1 and out == ""
        assert err.startswith(LandingMissing.code + ": ")
        assert "/does/not/exist" in err

    def test_an_oversize_logo_file_is_refused_without_being_read_whole(self, capsys, tmp_path):
        big = tmp_path / "big.svg"
        big.write_bytes(b"x" * (landing.MAX_LOGO_BYTES + 1))
        code, _, err = run(["--logo", str(big)], json.dumps(declared()), capsys)
        assert code == 1
        assert err.startswith(LandingTooLarge.code + ": ")

    def test_the_css_bound_is_on_the_total_not_each_file(self, capsys, tmp_path):
        part = "a{}" * (landing.MAX_CSS_BYTES // 6 + 1)
        args = []
        for n in range(3):
            path = tmp_path / f"{n}.css"
            path.write_text(part)
            args += ["--css", str(path)]
        code, _, err = run(args, json.dumps(declared()), capsys)
        assert code == 1
        assert err.startswith(LandingTooLarge.code + ": ")

    def test_a_stylesheet_that_is_not_utf8_is_a_coded_failure(self, capsys, tmp_path):
        path = tmp_path / "a.css"
        path.write_bytes(b"\xff")
        code, _, err = run(["--css", str(path)], json.dumps(declared()), capsys)
        assert code == 1
        assert err.startswith(LandingMissing.code + ": ")

    def test_a_repeated_member_is_refused_rather_than_resolved_last_one_wins(self, capsys):
        """json.loads keeps the last of two members, so a document with "tags": ["testnet"] then
        "tags": [] would render a page with no testnet notice."""
        doc = json.dumps(declared(tags=["testnet"]))
        doc = doc.replace('"tags": ["testnet"]', '"tags": ["testnet"], "tags": []')
        code, out, err = run([], doc, capsys)
        assert code == 1 and out == ""
        assert err.startswith(LandingInput.code + ": ")
        assert "tags" in err
