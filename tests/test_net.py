import httpx2
import pytest

from seo_mcp import net

READ = (net.Endpoint("GET", "api.example.test", r"/v1/read"),)


async def test_allowed_request_returns_json(fake_net):
    calls = fake_net(lambda _: httpx2.Response(200, json={"a": 1}))
    assert await net.request(READ, "GET", "https://api.example.test/v1/read") == {"a": 1}
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://api.example.test/v1/read"),
        ("GET", "https://api.example.test/v1/read/extra"),
        ("GET", "https://api.example.test/v1/write"),
        ("GET", "https://other.example.test/v1/read"),
        ("GET", "http://api.example.test/v1/read"),
        ("GET", "https://api.example.test:8443/v1/read"),
        ("GET", "https://user@api.example.test/v1/read"),
    ],
)
async def test_unlisted_request_stops_before_any_io(fake_net, method, url):
    calls = fake_net(lambda _: httpx2.Response(200, json={}))
    with pytest.raises(net.NotAllowed):
        await net.request(READ, method, url)
    assert calls == []


async def test_redirect_is_not_followed(fake_net):
    target = {"location": "https://evil.example.test/"}
    calls = fake_net(lambda _: httpx2.Response(302, headers=target))
    with pytest.raises(net.ApiError) as caught:
        await net.request(READ, "GET", "https://api.example.test/v1/read")
    assert caught.value.status == 302
    assert len(calls) == 1


async def test_error_carries_no_url_or_body(fake_net):
    fake_net(lambda _: httpx2.Response(403, text="the-upstream-body"))
    with pytest.raises(net.ApiError) as caught:
        await net.request(READ, "GET", "https://api.example.test/v1/read", params={"apikey": "k1"})
    assert "the-upstream-body" not in str(caught.value)
    assert "k1" not in str(caught.value)


async def test_transport_failure_hides_the_url(fake_net):
    def fail(request):
        raise httpx2.ConnectError(f"cannot reach {request.url}")

    fake_net(fail)
    with pytest.raises(net.ApiError) as caught:
        await net.request(READ, "GET", "https://api.example.test/v1/read", params={"apikey": "k1"})
    assert "k1" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
