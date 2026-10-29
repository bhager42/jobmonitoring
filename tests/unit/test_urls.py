import pytest

from app.scraping.urls import canonical_url, fingerprint


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Jobs.Example.COM/Careers/123/", "https://jobs.example.com/Careers/123"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a#apply", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        (
            "https://example.com/job?utm_source=x&gh_src=abc&id=7&UTM_Medium=y&lever-source=z",
            "https://example.com/job?id=7",
        ),
        ("https://example.com/job?b=2&a=1", "https://example.com/job?a=1&b=2"),
        ("  https://example.com/x  ", "https://example.com/x"),
    ],
)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_fingerprint_prefers_external_id() -> None:
    a = fingerprint("greenhouse", "123", "https://x.test/jobs/123?gh_src=a")
    b = fingerprint("greenhouse", "123", "https://x.test/other")
    assert a == b
    assert len(a) == 64


def test_fingerprint_falls_back_to_canonical_url() -> None:
    a = fingerprint("heuristic", None, "https://X.test/jobs/1/?utm_source=mail#top")
    b = fingerprint("heuristic", None, "https://x.test/jobs/1")
    assert a == b
    assert a != fingerprint("jsonld", None, "https://x.test/jobs/1")  # scoped per adapter
