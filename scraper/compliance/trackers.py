"""Third-party resources whose presence creates a consent obligation.

A site that loads none of these needs no banner and no cookie policy, so every
cookie finding is gated on this module. Matched against the raw HTML,
lowercased — the loader is in the markup even when the banner it configures is
injected later by JavaScript.
"""

# Non-exempt cookies: analytics, advertising, and embeds that set cookies on load.
_TRACKERS: tuple[str, ...] = (
    # Analytics
    "gtag(", "googletagmanager.com", "google-analytics.com", "_gaq",
    "clarity.ms", "hotjar.com", "static.hotjar.com", "hj(",
    "matomo.js", "piwik.js", "mouseflow.com", "luckyorange.com",
    # Advertising and social pixels
    "connect.facebook.net", "fbq(", "facebook.net/en_us/fbevents.js",
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google.com/ads", "snap.licdn.com", "px.ads.linkedin.com",
    "analytics.tiktok.com", "ads-twitter.com", "static.ads-twitter.com",
    "static.criteo.net", "criteo.com", "taboola.com", "outbrain.com",
    "bat.bing.com", "pinimg.com/ct",
    # Embeds that set cookies on load. youtube-nocookie.com is deliberately
    # absent: it is the privacy-enhanced embed, i.e. the fix we sell.
    "youtube.com/embed", "player.vimeo.com", "google.com/maps/embed",
    "maps.googleapis.com", "addthis.com", "sharethis.com", "disqus.com",
    # reCAPTCHA sets _GRECAPTCHA from Google's own domain; the AEPD does not
    # treat it as exempt, and it sits on almost every WordPress contact form.
    "google.com/recaptcha", "gstatic.com/recaptcha", "recaptcha/api.js",
)

# Signals that leak personal data to a third party without setting a cookie.
# Kept out of the consent trigger on purpose: Google Fonts served from Google's
# CDN is an international-transfer problem (LG München I, 3 O 17493/20), not a
# cookie one, and it is present on the large majority of small-business sites.
# Counting it as a tracker would demand a cookie banner from nearly everyone and
# destroy the credibility of the whole check on the first sales call.
_TRANSFER_SIGNALS: tuple[str, ...] = (
    "fonts.googleapis.com", "fonts.gstatic.com",
)

# Deliberately NOT trackers:
# - plausible.io / usefathom.com — cookieless by design, no consent required
# - youtube-nocookie.com — the privacy-enhanced embed, which is the correct fix


def loads_trackers(html_lower: str) -> bool:
    """True when the page loads a resource that sets non-essential cookies."""
    return any(sig in html_lower for sig in _TRACKERS)


def tracker_hits(html_lower: str) -> list[str]:
    """Return the tracker signatures found, for reporting and debugging."""
    return [sig for sig in _TRACKERS if sig in html_lower]


def transfer_hits(html_lower: str) -> list[str]:
    """Return third-party transfer signals that do not by themselves require consent."""
    return [sig for sig in _TRANSFER_SIGNALS if sig in html_lower]
