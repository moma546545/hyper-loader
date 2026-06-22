from core.network_safety import is_safe_host


def test_is_safe_host_rejects_inconsistent_dns_resolution():
    resolutions = iter(
        [
            ["93.184.216.34"],
            ["203.0.113.55"],
        ]
    )

    def resolver(_host: str):
        return next(resolutions)

    assert (
        is_safe_host(
            "example.com",
            allow_private=False,
            resolver=resolver,
            host_validator=lambda host: host == "example.com",
            resolver_attempts=2,
            require_consistent_resolution=True,
        )
        is False
    )


def test_is_safe_host_accepts_consistent_dns_resolution():
    def resolver(_host: str):
        return ["93.184.216.34"]

    assert (
        is_safe_host(
            "example.com",
            allow_private=False,
            resolver=resolver,
            host_validator=lambda host: host == "example.com",
            resolver_attempts=2,
            require_consistent_resolution=True,
        )
        is True
    )


def test_is_safe_host_uses_default_hostname_validator_when_not_provided():
    def resolver(_host: str):
        return ["93.184.216.34"]

    assert (
        is_safe_host(
            "bad host name",
            allow_private=False,
            resolver=resolver,
        )
        is False
    )


def test_is_safe_host_accepts_resolver_returning_single_string_ip():
    def resolver(_host: str):
        return "93.184.216.34"

    assert (
        is_safe_host(
            "example.com",
            allow_private=False,
            resolver=resolver,
            host_validator=lambda host: host == "example.com",
        )
        is True
    )
