"""submit_urls: the 1 write. It sends URL_UPDATED to the Google Indexing API for opted-in sites."""

import base64
import json
from datetime import UTC, datetime

import httpx2
import pytest

from seo_mcp import config, connection, core, quota, setup
from seo_mcp.providers import _google, indexing

BASE = "example.com"
HOST = f"www.{BASE}"
PROJECT = "demo-project"
EMAIL = f"seo-reader@{PROJECT}.iam.gserviceaccount.com"
ACCESS = "fake-indexing-token-1234"
NOTIFY = "2026-11-16T05:55:14.151Z"
CANARY = "canary-upstream-text-9876"
DOMAIN = {"gsc_property": "sc-domain:example.com", "submit_urls": "on"}
PREFIX = {"gsc_property": "https://www.example.com/jobs/", "submit_urls": "on"}


@pytest.fixture
def owner(tmp_path, private_pem):
    """A service account of a Cloud project, as Google writes its JSON key."""
    path = tmp_path / "owner.json"
    account = {"private_key": private_pem, "private_key_id": "fake-key-id", "client_email": EMAIL}
    path.write_text(json.dumps(account))
    _google._tokens.clear()
    return {"google_service_account": {"file": str(path)}}


def google(publish=None):
    """The token endpoint and the publish endpoint, through the real client code."""

    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx2.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        assert request.url.host == "indexing.googleapis.com"
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        body = json.loads(request.content)
        if publish:
            return publish(body)
        latest = {"url": body["url"], "type": body["type"], "notifyTime": NOTIFY}
        return httpx2.Response(
            200, json={"urlNotificationMetadata": {"url": body["url"], "latestUpdate": latest}}
        )

    return handle


def published(calls):
    return [json.loads(call.content) for call in calls if call.url.host == indexing.HOST]


def scopes(calls):
    found = []
    for call in calls:
        if call.url.host == "oauth2.googleapis.com":
            grant = dict(httpx2.QueryParams(call.content.decode()))["assertion"]
            claims = grant.split(".")[1]
            claims += "=" * (-len(claims) % 4)
            found.append(json.loads(base64.urlsafe_b64decode(claims))["scope"])
    return found


def used():
    return quota.view(indexing.LEDGER, PROJECT)["calls_used_today"]


async def test_a_site_with_no_opt_in_sends_nothing(write_config, fake_net, owner):
    write_config({"gsc_property": "sc-domain:example.com"}, owner)
    calls = fake_net(google())
    result = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    assert result["status"] == "refused"
    assert result["reason"] == core.SUBMIT_OFF.format(host=HOST)
    assert calls == []
    assert not quota.path(indexing.LEDGER, PROJECT).exists()


@pytest.mark.parametrize("value", ["off", "false", "yes", "ON", ""])
def test_only_on_opts_in(write_config, value):
    write_config({"gsc_property": "sc-domain:example.com", "submit_urls": value})
    with pytest.raises(config.ConfigError, match="submit_urls"):
        config.load()


async def test_a_submit_sends_url_updated_with_the_indexing_scope(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    result = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    assert result["status"] == "ok", result
    (row,) = result["rows"]
    assert row["status"] == "submitted"
    assert row["notify_time"] == NOTIFY
    assert row["from_cache"] is False
    assert published(calls) == [{"url": "https://www.example.com/jobs/1", "type": "URL_UPDATED"}]
    assert scopes(calls) == [indexing.SCOPE]
    assert used() == 1
    assert result["calls_left"] == 199
    assert result["reset_stated"] is True
    assert ACCESS not in json.dumps(result)


async def test_a_read_source_keeps_the_read_only_scope(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    token = await _google.token(config.resolve(HOST), _google.GSC_SCOPE)
    assert token == ACCESS
    assert scopes(calls) == [indexing.SCOPE, _google.GSC_SCOPE]


FOREIGN = [
    "https://example.com.evil.test/jobs/1",
    "https://user:s3cr3t-canary@www.example.com/jobs/1",
    "https://www.example.com/jobs/../private",
    "https://www.example.com/jobs/%2e%2e/private",
    "https://www.example.com/jobs\\1",
    "https://www.example.com:8443/jobs/1",
    "https://www.example.com/jobs/1#top",
    "ftp://www.example.com/jobs/1",
    "/jobs/1",
    f"https://{HOST}./jobs/1",
    "https://www.example.com/jobs/1\n",
]


@pytest.mark.parametrize("url", FOREIGN)
async def test_a_url_outside_the_property_is_refused_before_any_call(
    write_config, fake_net, owner, url
):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    result = await core.submit_urls(HOST, [url])
    (row,) = result["rows"]
    assert row["status"] == "refused", row
    assert calls == []
    assert "s3cr3t-canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/blog/1",
        "http://www.example.com/jobs/1",
        "https://example.com/jobs/",
    ],
)
async def test_a_url_prefix_property_takes_only_its_prefix(write_config, fake_net, owner, url):
    write_config(PREFIX, owner)
    calls = fake_net(google())
    (row,) = (await core.submit_urls(HOST, [url]))["rows"]
    assert row["status"] == "refused"
    assert calls == []


async def test_a_domain_property_matches_whole_labels(write_config, fake_net, owner):
    """example.com holds www.example.com, not a host that only ends in the same letters."""
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    # The private scan allows only known hosts, so the test builds this synthetic one.
    (row,) = (await core.submit_urls(HOST, [f"https://not{BASE}/1"]))["rows"]
    assert row["status"] == "refused"
    assert calls == []


async def test_a_domain_property_takes_each_subdomain(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    urls = ["https://example.com/a", "http://jobs.example.com/b"]
    result = await core.submit_urls(HOST, urls)
    assert [row["status"] for row in result["rows"]] == ["submitted", "submitted"]
    assert sorted(item["url"] for item in published(calls)) == sorted(urls)


async def test_a_repeat_inside_the_window_sends_and_spends_nothing(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    url = "https://www.example.com/jobs/1"
    await core.submit_urls(HOST, [url])
    again = await core.submit_urls(HOST, [url, url])
    (row,) = again["rows"]
    assert row["status"] == "submitted"
    assert row["from_cache"] is True
    assert row["notify_time"] == NOTIFY
    assert len(published(calls)) == 1
    assert used() == 1


async def test_at_the_limit_nothing_goes_out(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    today = quota.today(indexing.LEDGER)
    quota.write_json(quota.path(indexing.LEDGER, PROJECT), {"day": today, "used": 200})
    calls = fake_net(google())
    result = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    (row,) = result["rows"]
    assert row["status"] == "refused"
    assert "200" in row["reason"]
    assert published(calls) == []


def moment_at(monkeypatch, moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz or UTC)

    monkeypatch.setattr(quota, "datetime", Frozen)


async def test_the_ledger_day_is_the_pacific_day_with_summer_time(
    write_config, fake_net, owner, monkeypatch
):
    """Google resets at midnight Pacific time. In July that is 07:00 UTC, not 08:00 UTC."""
    write_config(DOMAIN, owner)
    quota.write_json(quota.path(indexing.LEDGER, PROJECT), {"day": "2026-07-14", "used": 200})
    calls = fake_net(google())
    moment_at(monkeypatch, datetime(2026, 7, 15, 6, 30, tzinfo=UTC))
    late = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    assert late["rows"][0]["status"] == "refused"
    moment_at(monkeypatch, datetime(2026, 7, 15, 7, 30, tzinfo=UTC))
    early = await core.submit_urls(HOST, ["https://www.example.com/jobs/2"])
    assert early["rows"][0]["status"] == "submitted"
    assert len(published(calls)) == 1


async def test_a_refused_call_gives_fixed_text_and_no_body(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    body = {"error": {"code": 403, "message": f"Permission denied {CANARY}"}}
    fake_net(google(lambda _: httpx2.Response(403, json=body)))
    result = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    (row,) = result["rows"]
    assert row["status"] == "failed"
    assert row["reason"] == indexing.REASONS[403]
    assert CANARY not in json.dumps(result)


def echo(body):
    """The live shape of an accepted publish: Google echoes the URL, with no latestUpdate."""
    return httpx2.Response(200, json={"urlNotificationMetadata": {"url": body["url"]}})


def echo_with_bad_time(body):
    latest = {"url": body["url"], "type": body["type"], "notifyTime": CANARY}
    return httpx2.Response(
        200, json={"urlNotificationMetadata": {"url": body["url"], "latestUpdate": latest}}
    )


@pytest.mark.parametrize("publish", [echo, echo_with_bad_time])
async def test_an_echo_of_the_url_with_no_notify_time_is_submitted(
    write_config, fake_net, owner, publish
):
    write_config(DOMAIN, owner)
    calls = fake_net(google(publish))
    url = "https://www.example.com/jobs/1"
    result = await core.submit_urls(HOST, [url])
    (row,) = result["rows"]
    assert row["status"] == "submitted", row
    assert row["notify_time"] is None
    assert row["reason"] == indexing.SUBMITTED
    assert result["counts"]["submitted"] == 1
    assert CANARY not in json.dumps(result)
    (again,) = (await core.submit_urls(HOST, [url]))["rows"]
    assert again["status"] == "submitted"
    assert again["from_cache"] is True
    assert again["reason"] == indexing.SAVED
    assert len(published(calls)) == 1
    assert used() == 1


def raw(content, status=200):
    return lambda _: httpx2.Response(status, content=content)


def says(body):
    return lambda _: httpx2.Response(200, json=body)


OTHER_URL = {"urlNotificationMetadata": {"url": "https://www.example.com/jobs/2"}}
NO_URL = {"urlNotificationMetadata": {"latestUpdate": {"notifyTime": CANARY}}}
UNREADABLE = {
    "no url": says(NO_URL),
    "another url": says(OTHER_URL),
    "empty object": says({}),
    "list": says([]),
    "list of text": says([CANARY]),
    "null": says(None),
    "number": says(5),
    "text": says(CANARY),
    "not json": raw(CANARY.encode()),
    "no body": raw(b"", 204),
}


@pytest.mark.parametrize("publish", UNREADABLE.values(), ids=UNREADABLE.keys())
async def test_a_success_that_the_server_cannot_read_is_unconfirmed(
    write_config, fake_net, owner, publish
):
    """Google can have the URL, so the row never says failed, and a repeat sends nothing."""
    write_config(DOMAIN, owner)
    calls = fake_net(google(publish))
    url = "https://www.example.com/jobs/1"
    result = await core.submit_urls(HOST, [url])
    (row,) = result["rows"]
    assert row["status"] == "unconfirmed", row
    assert row["reason"] == indexing.UNREAD
    assert row["notify_time"] is None
    assert result["counts"] == {"submitted": 0, "unconfirmed": 1, "refused": 0, "failed": 0}
    assert CANARY not in json.dumps(result)
    (again,) = (await core.submit_urls(HOST, [url]))["rows"]
    assert again["status"] == "unconfirmed"
    assert again["from_cache"] is True
    assert again["reason"] == indexing.UNREAD_SAVED
    assert len(published(calls)) == 1
    assert used() == 1


async def test_a_timeout_after_the_send_says_that_google_can_have_it(write_config, fake_net, owner):
    write_config(DOMAIN, owner)

    def slow(_):
        raise httpx2.ReadTimeout("slow")

    fake_net(google(slow))
    (row,) = (await core.submit_urls(HOST, ["https://www.example.com/jobs/1"]))["rows"]
    assert row["status"] == "failed"
    assert row["reason"] == indexing.MAYBE_SENT


async def test_a_mixed_list_gives_each_url_its_own_row_in_the_asked_order(
    write_config, fake_net, owner
):
    """A refused URL stands between sent URLs, and each sent URL gets another answer of Google."""
    urls = [f"https://www.example.com/jobs/{number}" for number in (1, 2, 3)]
    outside = "https://www.other.test/jobs/9"

    def publish(body):
        if body["url"] == urls[1]:
            return httpx2.Response(500)
        echo = urls[0] if body["url"] == urls[0] else "https://www.example.com/jobs/0"
        return httpx2.Response(200, json={"urlNotificationMetadata": {"url": echo}})

    write_config(DOMAIN, owner)
    calls = fake_net(google(publish))
    asked = [urls[0], outside, urls[1], urls[0], urls[2]]
    result = await core.submit_urls(HOST, asked)
    rows = result["rows"]
    assert [(row["url"], row["status"]) for row in rows] == [
        (urls[0], "submitted"),
        (outside, "refused"),
        (urls[1], "failed"),
        (urls[2], "unconfirmed"),
    ]
    assert result["counts"] == {"submitted": 1, "unconfirmed": 1, "refused": 1, "failed": 1}
    assert sorted(body["url"] for body in published(calls)) == urls
    assert used() == 3


@pytest.mark.parametrize("urls", [[], [1], None, "https://www.example.com/jobs/1"])
async def test_a_bad_list_is_an_error(write_config, fake_net, owner, urls):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    result = await core.submit_urls(HOST, urls)
    assert result["status"] == "error"
    assert calls == []


async def test_too_many_urls_is_an_error(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    urls = [f"https://www.example.com/jobs/{n}" for n in range(core.MAX_SUBMIT + 1)]
    result = await core.submit_urls(HOST, urls)
    assert result["status"] == "error"
    assert str(core.MAX_SUBMIT) in result["reason"]
    assert calls == []


async def test_an_opted_in_site_with_no_account_is_not_connected(write_config, fake_net):
    write_config(DOMAIN)
    calls = fake_net(google())
    result = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    assert result["status"] == "not_connected"
    assert calls == []


async def test_an_account_with_no_cloud_project_is_not_connected(write_config, fake_net, account):
    """The ledger counts the Cloud project of the signed email, never a label in the file."""
    write_config(DOMAIN, account)
    calls = fake_net(google())
    result = await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    assert result["status"] == "not_connected"
    assert calls == []


async def test_2_sites_of_1_project_share_1_ledger(
    write_config, fake_net, owner, tmp_path, monkeypatch
):
    path = write_config(DOMAIN, owner)
    body = json.loads(path.read_text())
    other = {**DOMAIN, "host": "jobs.example.test", "gsc_property": "https://jobs.example.test/"}
    body["sites"][str(tmp_path / "other")] = other
    path.write_text(json.dumps(body))
    fake_net(google())
    await core.submit_urls(HOST, ["https://www.example.com/jobs/1"])
    await core.submit_urls("jobs.example.test", ["https://jobs.example.test/1"])
    assert used() == 2


def test_list_sites_shows_the_opt_in(write_config, owner):
    write_config({"gsc_property": "sc-domain:example.com"}, owner)
    (row,) = core.list_sites()["rows"]
    assert row["connected"]["indexing"] is False
    assert row["missing"]["indexing"] == ["submit_urls is off for this site"]


def test_an_off_site_stays_ready_in_setup_status(write_config, owner, monkeypatch):
    write_config({"gsc_property": "sc-domain:example.com"}, owner)
    (site,) = config.load()
    assert connection.state(site, "indexing") == connection.OFF
    report = setup.status()
    (row,) = [p for p in report["sites"][0]["providers"] if p["provider"] == "indexing"]
    assert row["state"] == connection.OFF
    assert row["gaps"] == []


def test_query_api_has_no_method_that_writes():
    entry = core.providers.APIS["indexing"]
    assert entry.methods == {}


async def test_query_api_cannot_reach_the_publish(write_config, fake_net, owner):
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    result = await core.query_api(HOST, "indexing", "publish", {"url": "https://www.example.com/x"})
    assert result["status"] == "error"
    assert calls == []


async def test_a_receipt_of_the_old_day_opens_no_call(write_config, fake_net, owner, monkeypatch):
    """The Pacific day ends between the count and the send: the call stays counted, unsent."""
    write_config(DOMAIN, owner)
    calls = fake_net(google())
    # With no ledger file, quota.spend reads the day 1 time, then the send reads it again.
    moments = [datetime(2026, 7, 15, 6, 59, 59, tzinfo=UTC)]
    moments.append(datetime(2026, 7, 15, 7, 0, 1, tzinfo=UTC))

    class Midnight(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = moments.pop(0) if moments else datetime(2026, 7, 15, 7, 0, 2, tzinfo=UTC)
            return moment.astimezone(tz or UTC)

    monkeypatch.setattr(quota, "datetime", Midnight)
    (row,) = (await core.submit_urls(HOST, ["https://www.example.com/jobs/1"]))["rows"]
    assert row["status"] == "failed"
    assert row["reason"] == indexing.DAY_ENDED
    assert published(calls) == []


def test_list_sites_reads_no_secret_for_the_write(write_config, tmp_path):
    """The ledger key comes from the key file, so list_sites shows only the limits.

    A read of a Keychain item for list_sites would ask the user each time.
    """
    locked = tmp_path / "locked.json"
    locked.write_text("{}")
    locked.chmod(0)
    write_config(DOMAIN, {"google_service_account": {"file": str(locked)}})
    (row,) = core.list_sites()["rows"]
    assert row["connected"]["indexing"] is True
    assert row["quota"]["indexing"] == {
        "tracked": False,
        "limits": list(indexing.APIS["indexing"].quota.limits),
    }


async def test_a_url_with_a_key_shape_keeps_its_status_on_a_repeat(write_config, fake_net, owner):
    """The saved answer is scrubbed, so a key= query differs from the sent URL on a repeat."""
    write_config(DOMAIN, owner)
    calls = fake_net(google(echo))
    url = "https://www.example.com/jobs/1?key=abc"
    (first,) = (await core.submit_urls(HOST, [url]))["rows"]
    (again,) = (await core.submit_urls(HOST, [url]))["rows"]
    assert first["status"] == "submitted"
    assert again["status"] == "submitted", again
    assert again["from_cache"] is True
    assert len(published(calls)) == 1
