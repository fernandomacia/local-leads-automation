"""Tests for scraper/social.py — which network a URL belongs to."""

import pytest

from config import SOCIAL_DOMAINS, SOCIAL_NAMES
from scraper.social import identify_platform


class TestIdentifyPlatform:
    @pytest.mark.parametrize("url, expected", [
        ("https://www.instagram.com/negocio", "instagram"),
        ("https://facebook.com/negocio", "facebook"),
        ("https://www.linkedin.com/company/negocio", "linkedin"),
        ("https://www.tiktok.com/@negocio", "tiktok"),
    ])
    def test_a_profile_is_matched_to_its_platform(self, url, expected):
        assert identify_platform(url) == expected

    def test_a_country_subdomain_is_the_same_network(self):
        # How a Spanish company profile is linked, and what the Maps card carries.
        assert identify_platform("https://es.linkedin.com/company/negocio") == "linkedin"

    @pytest.mark.parametrize("url", ["https://x.com/negocio", "https://www.x.com/negocio",
                                     "https://twitter.com/negocio"])
    def test_x_and_twitter_are_one_platform(self, url):
        # The rename did not split the network, and a lead found under either name has
        # to land in the same column the panel reads.
        assert identify_platform(url) == "twitter"

    @pytest.mark.parametrize("url", [
        "https://www.minegocio.es", "https://minegocio.wixsite.com/tienda",
        "https://notlinkedin.com/x", "https://linkedin.com.phishing.es/x", "", "no-es-una-url",
    ])
    def test_anything_else_is_not_a_profile(self, url):
        # The suffix check is anchored on a dot: a host merely ending in the letters of
        # a network is not that network.
        assert identify_platform(url) is None


class TestPlatformKeys:
    def test_every_platform_has_at_least_one_domain(self):
        assert all(domains for domains in SOCIAL_DOMAINS.values())

    def test_the_keys_are_platforms_and_never_domains(self):
        # Everything that reads this dict — the analyzer's empty analysis, the worker's
        # social fields, the contact-channel check — uses the keys as column names.
        assert not any("." in platform for platform in SOCIAL_DOMAINS)

    def test_every_platform_can_be_named_out_loud(self):
        # A finding that names the network is read to a business owner, so a platform
        # without a name here would reach the call as its own dict key.
        assert set(SOCIAL_NAMES) == set(SOCIAL_DOMAINS)
