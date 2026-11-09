"""Google Analytics 4: organic traffic and organic landing pages."""

import json
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from seo_mcp import net, quota, shared
from seo_mcp.api import Api, Method, Quota, Run, answer, row_limit, whole
from seo_mcp.config import Site
from seo_mcp.params import (
    DATES,
    SLACK_DAYS,
    BadInput,
    Params,
    bounds,
    days_ago,
    iso_day,
    moved,
    moved_note,
    today,
    too_late,
)
from seo_mcp.providers import _google
from seo_mcp.results import Refused, ok

HOST = "analyticsdata.googleapis.com"
ENDPOINTS = (
    net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runReport"),
    net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runPivotReport"),
    net.Endpoint("POST", HOST, r"/v1beta/properties/\d+:runRealtimeReport"),
    net.Endpoint("GET", HOST, r"/v1beta/properties/\d+/metadata"),
)
WINDOW_DAYS = 28
# No field of the API marks a day complete. The help page says that processing can take 24-48
# hours, so the last hit of a day is done at the start of the 3rd day after it. A relative date
# goes out as text, and GA4 reads it in the property zone, so the server needs no zone.
LAG_DAYS = 3
LAST_COMPLETE = f"{LAG_DAYS}daysAgo"
ESTIMATE_HOW = (
    f"An estimate: {LAST_COMPLETE}, which GA4 reads in the property zone. The GA4 help says that "
    "processing can take 24-48 hours, and the API has no field for this. For a YYYY-MM-DD end, "
    "partial_days holds each day that can be partial in some zone, from the UTC date of the "
    "answer."
)
NO_ZONE = "The property zone: the reply names none."
THIN_SESSIONS = 100
MAX_DAILY_ROWS = 100
PAGE_MATCH = "Sessions that start on this exact path, on every host of the property."
# The totals of a range with no rows. GA4 leaves them out at 0 sessions. A count is a real 0,
# but engagementRate is engagedSessions / sessions, so with 0 sessions it is 0/0: no rate.
EMPTY_TOTALS: dict[str, float | None] = {
    "sessions": 0.0,
    "totalUsers": 0.0,
    "engagementRate": None,
    "keyEvents": 0.0,
}
METRICS = tuple(EMPTY_TOTALS)
# The first day on which the property reports any event, of any channel. GA4 leaves out a row
# whose metrics are all 0, and a day with 0 organic sessions can be a real 0: this day is the
# best fact that tells "no data collected" from a real 0. The docs give no earliest date. GA4
# started as App + Web in 2019, so this floor misses no GA4 event; a refused read gives no delta.
FIRST_EVENT_FLOOR = "2019-01-01"
FIRST_EVENT = {
    "dateRanges": [{"startDate": FIRST_EVENT_FLOOR, "endDate": "today"}],
    "dimensions": [{"name": "date"}],
    "metrics": [{"name": "eventCount"}],
    "orderBys": [{"dimension": {"dimensionName": "date"}, "desc": False}],
    "limit": 1,
    "returnPropertyQuota": True,
}
ORGANIC = {
    "filter": {
        "fieldName": "sessionDefaultChannelGroup",
        "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
    }
}


# The docs state that daily quotas refresh at midnight Pacific Standard Time. A fixed offset
# also keeps the server able to start on a host with no timezone data.
RESET_ZONE = timezone(timedelta(hours=-8))
DAILY_TOKENS = {"standard": 200_000, "analytics_360": 2_000_000}
LOW_DAY_TOKENS = 20_000
LOW_HOUR_TOKENS = 4_000
POOLS = {"runReport": "core", "runPivotReport": "core", "runRealtimeReport": "realtime"}
COUNTED = (
    "tokens_left comes from the API, at seen_at. tokens_used_today sums the tokens of the calls "
    "of this machine, through this cache folder."
)
NO_READING = "The server has no reading since the last reset: the next GA4 report gives one."
# The docs: kind "is always the fixed string" of the method. A real empty report has no rows,
# so only kind tells it from a reply such as {"message": "try later"}.
REPORT = shared.holds("kind")
# Each property has standard dimensions and metrics, so {} is a failure.
METADATA = shared.has_any("name", "dimensions", "metrics", "comparisons")


def reading_path(prop: str, pool: str) -> Path:
    return quota.folder() / f"ga4-quota-{quote(prop, safe='')}-{pool}.json"


def _pacific_day() -> str:
    return datetime.now(RESET_ZONE).date().isoformat()


def _next_reset() -> str:
    next_day = datetime.now(RESET_ZONE).date() + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, tzinfo=RESET_ZONE).isoformat()


def _stored(file: Path) -> dict[str, Any]:
    try:
        stored = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) and stored.get("day") == _pacific_day() else {}


def _left(reply_quota: dict[str, Any], name: str) -> Any:
    return (reply_quota.get(name) or {}).get("remaining")


async def _record(prop: str, pool: str, reply: dict[str, Any]) -> None:
    seen = reply.get("propertyQuota")
    if not isinstance(seen, dict) or not isinstance(seen.get("tokensPerDay"), dict):
        return
    spent = seen["tokensPerDay"].get("consumed") or 0
    file = reading_path(prop, pool)
    async with quota.locked(file):
        total = _stored(file).get("tokens_used_today", 0) + spent
        quota.write_json(
            file,
            {
                "day": _pacific_day(),
                "seen_at": datetime.now(RESET_ZONE).isoformat(),
                "tokens_used_today": total,
                "tokens_spent": spent,
                "tokens_left": _left(seen, "tokensPerDay"),
                "tokens_left_this_hour": _left(seen, "tokensPerHour"),
                "tokens_left_for_project_this_hour": _left(seen, "tokensPerProjectPerHour"),
            },
        )


def _low(left: Any, hour_left: Any) -> str | None:
    if isinstance(left, int) and left < LOW_DAY_TOKENS:
        return f"Only {left} GA4 tokens are left for this day."
    if isinstance(hour_left, int) and hour_left < LOW_HOUR_TOKENS:
        return f"Only {hour_left} GA4 tokens are left for this hour."
    return None


def _view(prop: str, pool: str) -> dict[str, Any]:
    stored = _stored(reading_path(prop, pool))
    fields: dict[str, Any] = {
        "tokens_used_today": stored.get("tokens_used_today", 0),
        "daily_token_limit": DAILY_TOKENS,
        "tokens_left": stored.get("tokens_left"),
        "tokens_left_this_hour": stored.get("tokens_left_this_hour"),
        "tokens_left_for_project_this_hour": stored.get("tokens_left_for_project_this_hour"),
        "tokens_spent": stored.get("tokens_spent"),
        "seen_at": stored.get("seen_at"),
        "resets_at": _next_reset(),
        "reset_stated": True,
        "counted": COUNTED,
    }
    if not stored:
        fields["note"] = NO_READING
    warning = _low(fields["tokens_left"], fields["tokens_left_this_hour"])
    return {**fields, "quota_warning": warning} if warning else fields


async def _post(
    site: Site, prop: str, call: str, body: dict[str, Any], token: str, *, explain: bool = False
) -> Any:
    pool = POOLS[call]
    quota.show(lambda: _view(prop, pool))
    url = f"https://{HOST}/v1beta/properties/{prop}:{call}"

    async def send(_: None) -> Any:
        reply = await net.request(
            ENDPOINTS,
            "POST",
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            explain=explain,
        )
        # The answer is paid for: a full disk or a stuck reading file must not lose it.
        with suppress(OSError, TypeError, AttributeError, Refused):
            await _record(prop, pool, reply)
        return reply

    keep = shared.REALTIME_SECONDS if pool == "realtime" else shared.ANSWER_SECONDS
    path = shared.entry(site, "POST", url, body, explain)
    return await shared.once(path, send, good=REPORT, until=shared.after(keep))


def _check_order(name: str, start_text: Any, end_text: Any, utc_day: date) -> None:
    """Refuse a range with a relative date only when its start is after its end in every zone.

    GA4 reads 2 relative dates in 1 zone, so their N values give the order. With 1 YYYY-MM-DD
    date, the property day can be U-1, U, or U+1: a range that is valid in 1 of them goes out.
    """
    first, last = days_ago(start_text), days_ago(end_text)
    if first is not None and last is not None:
        if first < last:
            raise BadInput(
                f"{name} must not be after the end date: the start is fewer days ago than "
                "the end. The server sent nothing."
            )
        return
    begin, finish = bounds(start_text, utc_day), bounds(end_text, utc_day)
    if begin and finish and begin[0] > finish[1]:
        raise BadInput(
            f"{name} must not be after the end date: the start is after the end in every time "
            "zone of the property. The server sent nothing."
        )


def _newest_end(name: str, start_text: Any, end_text: Any) -> Any:
    """Give the end to send. The server reads no zone, and the day of the property is P.

    P is the UTC date U, U-1, or U+1. An end of U+1 or later is on or after P, so "today" is
    exactly the earlier of the end and P: GA4 reads it in the property zone, and no valid day
    is cut. A start of U or later can be after P, so "today" could reverse the range: then the
    end stops at U+1, the newest day of any zone. An end of U or earlier goes out as it is.
    """
    start, end = iso_day(start_text), iso_day(end_text)
    if start and end and start > end:
        raise BadInput(f"{name} must not be after the end date.")
    utc_day = today()
    _check_order(name, start_text, end_text, utc_day)
    furthest = utc_day + timedelta(days=SLACK_DAYS)
    if start and start > furthest:
        raise too_late(name, furthest)
    if not end or end < furthest:
        return end_text
    if start and start >= utc_day:
        return furthest.isoformat()
    return "today"


def _window(params: Params) -> dict[str, str]:
    if params.start and params.end:
        start, end = params.start.isoformat(), params.end.isoformat()
        return {"startDate": start, "endDate": _newest_end("start_date", start, end)}
    days = params.last_days or WINDOW_DAYS
    return {"startDate": f"{days + LAG_DAYS - 1}daysAgo", "endDate": f"{LAG_DAYS}daysAgo"}


def _days(params: Params) -> int:
    if params.start and params.end:
        return (params.end - params.start).days + 1
    return params.last_days or WINDOW_DAYS


def _filter(params: Params) -> dict[str, Any]:
    if not params.page:
        return ORGANIC
    parts = urlsplit(params.page)
    # landingPage has no query string, so a page with one needs the other dimension.
    field = "landingPagePlusQueryString" if parts.query else "landingPage"
    value = f"{parts.path}?{parts.query}" if parts.query else parts.path
    page = {"fieldName": field, "stringFilter": {"matchType": "EXACT", "value": value}}
    return {"andGroup": {"expressions": [ORGANIC, {"filter": page}]}}


def _zone(reply: dict[str, Any]) -> str:
    """The zone name of the reply, as a label only: the server reads no zone."""
    metadata = reply.get("metadata")
    name = metadata.get("timeZone") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else NO_ZONE


def _first_partial(end: str, day: date) -> str | None:
    """The first day that can be partial, in the terms of the end, or None for a complete range.

    A relative end is exact: GA4 reads both in 1 zone. For a YYYY-MM-DD end the property day is
    U-1 at the earliest, so a day after U-1-3 can be partial in some zone.
    """
    ago = days_ago(end)
    if ago is not None:
        return f"{LAG_DAYS - 1}daysAgo" if ago < LAG_DAYS else None
    first = day - timedelta(days=LAG_DAYS)
    found = iso_day(end)
    return first.isoformat() if found and found >= first else None


def _partial(start: str, end: str, day: date) -> dict[str, str] | None:
    first = _first_partial(end, day)
    if first is None:
        return None
    start_ago, first_ago = days_ago(start), days_ago(first)
    if start_ago is not None and first_ago is not None:
        before = start_ago > first_ago
    else:
        begin, other = bounds(start, day), bounds(first, day)
        before = bool(begin and other and begin[1] < other[0])
    # A start that can be inside the partial days starts them: more days partial, never fewer.
    return {"from": first if before else start, "to": end}


def _coverage(reply: dict[str, Any], window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    start, end = window.get("startDate"), window.get("endDate")
    day = _google.answer_day()
    if not (isinstance(start, str) and isinstance(end, str)):
        return None
    if not (bounds(start, day) and bounds(end, day)):
        return None
    partial = _partial(start, end, day)
    return _google.described(
        start, end, LAST_COMPLETE, "estimate", ESTIMATE_HOW, _zone(reply), partial
    )


def _day(row: Any) -> str | None:
    """The YYYY-MM-DD day of a row of the date dimension, or None for a row of another shape."""
    with suppress(KeyError, IndexError, TypeError, ValueError):
        return datetime.strptime(row["dimensionValues"][0]["value"], "%Y%m%d").date().isoformat()
    return None


async def _first_event(site: Site, prop: str, token: str) -> dict[str, Any]:
    """Give the first day of any event, and how the read went: found, none, or failed.

    The body is fixed, so each range and each GA4 source of 1 call share 1 saved answer.
    """
    try:
        reply = await _post(site, prop, "runReport", FIRST_EVENT, token)
    except (net.ApiError, Refused):
        return {"first_data_day": None, "first_data_read": "failed"}
    rows = reply.get("rows") or []
    if not rows:
        return {"first_data_day": None, "first_data_read": "none"}
    first = _day(rows[0])
    return {"first_data_day": first, "first_data_read": "found" if first else "failed"}


def _gap_at_start(dimension: str, rows: list[dict[str, Any]], params: Params) -> bool:
    """A range of a date comparison with no row on its first day can start before GA4 data.

    A landing page report has no day rows, so it cannot show that the first day has data.
    """
    if not (params.compared and params.start):
        return False
    return dimension != "date" or params.start.strftime("%Y%m%d") not in {
        row["date"] for row in rows
    }


async def _report(
    site: Site, dimension: str, limit: int, fetch: int, params: Params
) -> dict[str, Any]:
    prop = site.field("ga4_property_id")
    window = _window(params)
    token = await _google.token(site, _google.GA4_SCOPE)
    body = {
        "dateRanges": [window],
        "dimensions": [{"name": dimension}],
        "metrics": [{"name": name} for name in METRICS],
        "dimensionFilter": _filter(params),
        "metricAggregations": ["TOTAL"],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": fetch,
        "returnPropertyQuota": True,
    }
    reply = await _post(site, prop, "runReport", body, token)
    names = [header["name"] for header in reply.get("metricHeaders", [])]
    rows = [
        {
            dimension: row["dimensionValues"][0]["value"],
            **{
                name: float(cell["value"])
                for name, cell in zip(names, row["metricValues"], strict=True)
            },
        }
        for row in reply.get("rows", [])
    ]
    # GA4 leaves out rows and totals when the window has no sessions.
    total_cells = (reply.get("totals") or [{}])[0].get("metricValues", [])
    totals: dict[str, float | None] | None = {
        name: float(cell["value"]) for name, cell in zip(names, total_cells, strict=False)
    }
    coverage = _coverage(reply, window)
    partial = coverage["partial_days"] if coverage else None
    notes = [] if rows else ["GA4 reports no organic sessions in the window."]
    if not rows and not totals:
        # No row on a partial day can mean that GA4 has not processed it yet, so it is no 0.
        unknown = partial is not None and partial["from"] == window["startDate"]
        totals = None if unknown else dict(EMPTY_TOTALS)
        notes += [_google.UNKNOWN_REASON] if unknown else []
    thin = ((totals or {}).get("sessions") or 0) < THIN_SESSIONS
    extra: dict[str, Any] = {"page_match": PAGE_MATCH} if params.page else {}
    if _gap_at_start(dimension, rows, params):
        extra |= await _first_event(site, prop, token)
    if params.end and coverage and window["endDate"] != params.end.isoformat():
        extra["date_moved"] = moved(params.end, coverage["end_date"])
        notes.append(moved_note(extra["date_moved"]))
    return ok(
        rows,
        thin=thin,
        reason=" ".join(notes) or None,
        limit=limit,
        window=window,
        totals=totals,
        coverage=coverage,
        **extra,
    )


async def ga4_organic(site: Site, params: Params) -> dict[str, Any]:
    limit = min(_days(params), MAX_DAILY_ROWS)
    # 1 more row than the limit shows that a long range has more days than the rows.
    result = await _report(site, "date", limit, limit + 1, params)
    result["rows"].sort(key=lambda row: row["date"])
    return result


async def ga4_landing_pages(site: Site, params: Params) -> dict[str, Any]:
    return await _report(site, "landingPage", 25, 25, params)


SOURCES = {"ga4_organic": ga4_organic, "ga4_landing_pages": ga4_landing_pages}
ACCEPTS = {name: DATES | {"last_days", "page"} for name in SOURCES}

SHARED_FIELDS = frozenset(
    {
        "dimensions",
        "metrics",
        "dimensionFilter",
        "metricFilter",
        "returnPropertyQuota",
    }
)
HISTORY_FIELDS = SHARED_FIELDS | {
    "dateRanges",
    "currencyCode",
    "cohortSpec",
    "keepEmptyRows",
    "comparisons",
}
LIST_FIELDS = frozenset({"limit", "metricAggregations", "orderBys"})
SCHEMA_DOCS = "https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema"
NAMES_TAKEN_ON = "2026-11-07"
# The tables of SCHEMA_DOCS, so that an agent needs no metadata call for a standard name. Only
# query_api(api="ga4") shows them: the tool text loads in each session.
STANDARD_DIMENSIONS = tuple(
    """
    achievementId adFormat adSourceName adUnitName appVersion audienceId audienceName
    audienceResourceName brandingInterest browser browserVersion campaignId campaignName character
    city cityId cm360AccountId cm360AccountName cm360AdvertiserId cm360AdvertiserName
    cm360CampaignId cm360CampaignName cm360CreativeFormat cm360CreativeId cm360CreativeName
    cm360CreativeType cm360CreativeTypeId cm360CreativeVersion cm360Medium
    cm360PlacementCostStructure cm360PlacementId cm360PlacementName cm360RenderingId cm360SiteId
    cm360SiteName cm360Source cm360SourceMedium cohort cohortNthDay cohortNthMonth cohortNthWeek
    contentGroup contentId contentType continent continentId country countryId currencyCode date
    dateHour dateHourMinute day dayOfWeek dayOfWeekName defaultChannelGroup deviceCategory
    deviceModel dv360AdvertiserId dv360AdvertiserName dv360CampaignId dv360CampaignName
    dv360CreativeFormat dv360CreativeId dv360CreativeName dv360ExchangeId dv360ExchangeName
    dv360InsertionOrderId dv360InsertionOrderName dv360LineItemId dv360LineItemName dv360Medium
    dv360PartnerId dv360PartnerName dv360Source dv360SourceMedium eventName fileExtension fileName
    firstSessionDate firstUserCampaignId firstUserCampaignName firstUserCm360AccountId
    firstUserCm360AccountName firstUserCm360AdvertiserId firstUserCm360AdvertiserName
    firstUserCm360CampaignId firstUserCm360CampaignName firstUserCm360CreativeFormat
    firstUserCm360CreativeId firstUserCm360CreativeName firstUserCm360CreativeType
    firstUserCm360CreativeTypeId firstUserCm360CreativeVersion firstUserCm360Medium
    firstUserCm360PlacementCostStructure firstUserCm360PlacementId firstUserCm360PlacementName
    firstUserCm360RenderingId firstUserCm360SiteId firstUserCm360SiteName firstUserCm360Source
    firstUserCm360SourceMedium firstUserDefaultChannelGroup firstUserDv360AdvertiserId
    firstUserDv360AdvertiserName firstUserDv360CampaignId firstUserDv360CampaignName
    firstUserDv360CreativeFormat firstUserDv360CreativeId firstUserDv360CreativeName
    firstUserDv360ExchangeId firstUserDv360ExchangeName firstUserDv360InsertionOrderId
    firstUserDv360InsertionOrderName firstUserDv360LineItemId firstUserDv360LineItemName
    firstUserDv360Medium firstUserDv360PartnerId firstUserDv360PartnerName firstUserDv360Source
    firstUserDv360SourceMedium firstUserGoogleAdsAccountName firstUserGoogleAdsAdGroupId
    firstUserGoogleAdsAdGroupName firstUserGoogleAdsAdNetworkType firstUserGoogleAdsCampaignId
    firstUserGoogleAdsCampaignName firstUserGoogleAdsCampaignType firstUserGoogleAdsCreativeId
    firstUserGoogleAdsCustomerId firstUserGoogleAdsKeyword firstUserGoogleAdsQuery
    firstUserManualAdContent firstUserManualCampaignId firstUserManualCampaignName
    firstUserManualCreativeFormat firstUserManualMarketingTactic firstUserManualMedium
    firstUserManualSource firstUserManualSourceMedium firstUserManualSourcePlatform
    firstUserManualTerm firstUserMedium firstUserPrimaryChannelGroup firstUserSa360AdGroupId
    firstUserSa360AdGroupName firstUserSa360CampaignId firstUserSa360CampaignName
    firstUserSa360CreativeFormat firstUserSa360EngineAccountId firstUserSa360EngineAccountName
    firstUserSa360EngineAccountType firstUserSa360KeywordText firstUserSa360ManagerAccountId
    firstUserSa360ManagerAccountName firstUserSa360Medium firstUserSa360Query firstUserSa360Source
    firstUserSa360SourceMedium firstUserSource firstUserSourceMedium firstUserSourcePlatform
    fullPageUrl googleAdsAccountName googleAdsAdGroupId googleAdsAdGroupName googleAdsAdNetworkType
    googleAdsCampaignId googleAdsCampaignName googleAdsCampaignType googleAdsCreativeId
    googleAdsCustomerId googleAdsKeyword googleAdsQuery groupId hostName hour isKeyEvent isoWeek
    isoYear isoYearIsoWeek itemAffiliation itemBrand itemCategory itemCategory2 itemCategory3
    itemCategory4 itemCategory5 itemId itemListId itemListName itemListPosition itemLocationID
    itemName itemPromotionCreativeName itemPromotionCreativeSlot itemPromotionId itemPromotionName
    itemVariant landingPage landingPagePlusQueryString language languageCode level linkClasses
    linkDomain linkId linkText linkUrl manualAdContent manualCampaignId manualCampaignName
    manualCreativeFormat manualMarketingTactic manualMedium manualSource manualSourceMedium
    manualSourcePlatform manualTerm medium method minute mobileDeviceBranding
    mobileDeviceMarketingName mobileDeviceModel month newVsReturning nthDay nthHour nthMinute
    nthMonth nthWeek nthYear operatingSystem operatingSystemVersion operatingSystemWithVersion
    orderCoupon outbound pageLocation pagePath pagePathPlusQueryString pageReferrer pageTitle
    percentScrolled platform platformDeviceCategory primaryChannelGroup region sa360AdGroupId
    sa360AdGroupName sa360CampaignId sa360CampaignName sa360CreativeFormat sa360EngineAccountId
    sa360EngineAccountName sa360EngineAccountType sa360KeywordText sa360ManagerAccountId
    sa360ManagerAccountName sa360Medium sa360Query sa360Source sa360SourceMedium screenResolution
    searchTerm sessionCampaignId sessionCampaignName sessionCm360AccountId sessionCm360AccountName
    sessionCm360AdvertiserId sessionCm360AdvertiserName sessionCm360CampaignId
    sessionCm360CampaignName sessionCm360CreativeFormat sessionCm360CreativeId
    sessionCm360CreativeName sessionCm360CreativeType sessionCm360CreativeTypeId
    sessionCm360CreativeVersion sessionCm360Medium sessionCm360PlacementCostStructure
    sessionCm360PlacementId sessionCm360PlacementName sessionCm360RenderingId sessionCm360SiteId
    sessionCm360SiteName sessionCm360Source sessionCm360SourceMedium sessionDefaultChannelGroup
    sessionDv360AdvertiserId sessionDv360AdvertiserName sessionDv360CampaignId
    sessionDv360CampaignName sessionDv360CreativeFormat sessionDv360CreativeId
    sessionDv360CreativeName sessionDv360ExchangeId sessionDv360ExchangeName
    sessionDv360InsertionOrderId sessionDv360InsertionOrderName sessionDv360LineItemId
    sessionDv360LineItemName sessionDv360Medium sessionDv360PartnerId sessionDv360PartnerName
    sessionDv360Source sessionDv360SourceMedium sessionGoogleAdsAccountName
    sessionGoogleAdsAdGroupId sessionGoogleAdsAdGroupName sessionGoogleAdsAdNetworkType
    sessionGoogleAdsCampaignId sessionGoogleAdsCampaignName sessionGoogleAdsCampaignType
    sessionGoogleAdsCreativeId sessionGoogleAdsCustomerId sessionGoogleAdsKeyword
    sessionGoogleAdsQuery sessionManualAdContent sessionManualCampaignId sessionManualCampaignName
    sessionManualCreativeFormat sessionManualMarketingTactic sessionManualMedium sessionManualSource
    sessionManualSourceMedium sessionManualSourcePlatform sessionManualTerm sessionMedium
    sessionPrimaryChannelGroup sessionSa360AdGroupId sessionSa360AdGroupName sessionSa360CampaignId
    sessionSa360CampaignName sessionSa360CreativeFormat sessionSa360EngineAccountId
    sessionSa360EngineAccountName sessionSa360EngineAccountType sessionSa360Keyword
    sessionSa360ManagerAccountId sessionSa360ManagerAccountName sessionSa360Medium sessionSa360Query
    sessionSa360Source sessionSa360SourceMedium sessionSource sessionSourceMedium
    sessionSourcePlatform shippingTier signedInWithUserId source sourceMedium sourcePlatform
    streamId streamName testDataFilterId testDataFilterName transactionId unifiedPagePathScreen
    unifiedPageScreen unifiedScreenClass unifiedScreenName userAgeBracket userGender videoProvider
    videoTitle videoUrl virtualCurrencyName visible week year yearMonth yearWeek
    """.split()
)
STANDARD_METRICS = tuple(
    """
    active1DayUsers active28DayUsers active7DayUsers activeUsers adUnitExposure addToCarts
    advertiserAdClicks advertiserAdCost advertiserAdCostPerClick advertiserAdCostPerKeyEvent
    advertiserAdImpressions averagePurchaseRevenue averagePurchaseRevenuePerPayingUser
    averagePurchaseRevenuePerUser averageRevenuePerUser averageSessionDuration bounceRate
    cartToViewRate checkouts cohortActiveUsers cohortTotalUsers crashAffectedUsers
    crashFreeUsersRate dauPerMau dauPerWau ecommercePurchases engagedSessions engagementRate
    eventCount eventCountPerUser eventValue eventsPerSession firstTimePurchaserRate
    firstTimePurchasers firstTimePurchasersPerNewUser grossItemRevenue grossPurchaseRevenue
    itemDiscountAmount itemListClickEvents itemListClickThroughRate itemListViewEvents
    itemPromotionClickThroughRate itemRefundAmount itemRevenue itemViewEvents itemsAddedToCart
    itemsCheckedOut itemsClickedInList itemsClickedInPromotion itemsPurchased itemsViewed
    itemsViewedInList itemsViewedInPromotion keyEvents newUsers organicGoogleSearchAveragePosition
    organicGoogleSearchClickThroughRate organicGoogleSearchClicks organicGoogleSearchImpressions
    promotionClicks promotionViews publisherAdClicks publisherAdImpressions purchaseRevenue
    purchaseToViewRate purchaserRate refundAmount returnOnAdSpend screenPageViews
    screenPageViewsPerSession screenPageViewsPerUser scrolledUsers sessionKeyEventRate sessions
    sessionsPerUser shippingAmount taxAmount totalAdRevenue totalPurchasers totalRevenue totalUsers
    transactions transactionsPerPurchaser userEngagementDuration userKeyEventRate wauPerMau
    """.split()
)
PASSED_ON = ("metadata", "propertyQuota", "pivotHeaders", "aggregates")


def _flat_row(row: dict[str, Any], dimensions: list[str], metrics: list[str]) -> dict[str, Any]:
    cells = zip(metrics, row.get("metricValues", []), strict=False)
    labels = zip(dimensions, row.get("dimensionValues", []), strict=False)
    return {
        **{name: cell.get("value") for name, cell in labels},
        **{name: _number(cell.get("value")) for name, cell in cells},
    }


def _number(text: Any) -> Any:
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


def _flat(reply: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dimensions = [header["name"] for header in reply.get("dimensionHeaders", [])]
    metrics = [header["name"] for header in reply.get("metricHeaders", [])]
    return [_flat_row(row, dimensions, metrics) for row in rows]


def _sent_range(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    end = _newest_end("dateRanges startDate", item.get("startDate"), item.get("endDate"))
    return {**item, "endDate": end} if "endDate" in item else item


def _reader(call: str, paging: bool, limits: bool = True) -> Run:
    async def run(site: Site, request: dict[str, Any]) -> dict[str, Any]:
        prop = site.field("ga4_property_id")
        body = dict(request)
        asked = request.get("dateRanges")
        ranges = asked if isinstance(asked, list) else []
        if ranges:
            body["dateRanges"] = [_sent_range(item) for item in ranges]
        offset = 0
        if limits:
            body["limit"] = row_limit(
                "limit", request.get("limit"), "offset" if paging else "a filter"
            )
        if "offset" in request:
            offset = whole("offset", request["offset"], 0, 2**53)
        body.setdefault("returnPropertyQuota", True)
        token = await _google.token(site, _google.GA4_SCOPE)
        reply = await _post(site, prop, call, body, token, explain=True)
        rows = _flat(reply, reply.get("rows", []))
        count = reply.get("rowCount")
        more = "possible" if count is None else "yes" if offset + len(rows) < count else "no"
        extra = {name: reply[name] for name in PASSED_ON if name in reply}
        for name in ("totals", "maximums", "minimums"):
            if name in reply:
                extra[name] = _flat(reply, reply[name])
        notes: list[str] = []
        if ranges:
            extra["coverage"] = [_coverage(reply, item) for item in body["dateRanges"]]
            shifts = [
                moved(item["endDate"], shown["end_date"])
                for item, sent, shown in zip(
                    ranges, body["dateRanges"], extra["coverage"], strict=True
                )
                if shown and item != sent
            ]
            notes = [moved_note(item) for item in shifts]
            if shifts:
                extra["date_moved"] = shifts
        return answer(
            rows,
            more_rows=more,
            row_count=count,
            reason=" ".join(notes) or None,
            applied=body,
            **extra,
        )

    return run


async def _metadata(site: Site, request: dict[str, Any]) -> dict[str, Any]:
    prop = site.field("ga4_property_id")
    token = await _google.token(site, _google.GA4_SCOPE)
    url = f"https://{HOST}/v1beta/properties/{prop}/metadata"
    headers = {"Authorization": f"Bearer {token}"}
    reply = await shared.request(
        site, ENDPOINTS, "GET", url, headers=headers, explain=True, good=METADATA
    )

    def brief(kind: str, item: dict[str, Any]) -> dict[str, Any]:
        row = {"kind": kind, "apiName": item.get("apiName")}
        if item.get("category"):
            row["category"] = item["category"]
        if item.get("customDefinition"):
            row["custom"] = True
        return row

    rows = [brief("dimension", item) for item in reply.get("dimensions", [])]
    rows += [brief("metric", item) for item in reply.get("metrics", [])]
    return answer(rows, more_rows="no", row_count=len(rows))


def now(site: Site) -> dict[str, Any]:
    prop = site.field("ga4_property_id")
    return {pool: _view(prop, pool) for pool in sorted(set(POOLS.values()))}


APIS = {
    "ga4": Api(
        title="Google Analytics 4 Data API",
        summary="Visits, events, and conversions on the site, also in real time.",
        docs="https://developers.google.com/analytics/devguides/reporting/data/v1/rest",
        quota=Quota(
            "https://developers.google.com/analytics/devguides/reporting/data/v1/quotas",
            (
                "Each of the core and realtime pools: 200,000 tokens for each property each "
                "day, 40,000 each hour, 14,000 for each project each hour. Analytics 360: "
                "2,000,000, 400,000, and 140,000.",
                "10 concurrent requests for each property, 50 on Analytics 360.",
                "All daily quotas refresh at midnight Pacific Standard Time. Hourly quotas "
                "refresh within an hour.",
                "The token cost of a call is known only after it runs, so the server warns "
                "and does not refuse. Most calls cost 10 tokens or fewer.",
                "The API does not say the tier of a property, so the server shows both limits.",
            ),
            frozenset({"ga4_property_id", "google_service_account"}),
            now,
        ),
        methods={
            "run_report": Method(
                _reader("runReport", paging=True),
                "A report. dateRanges takes 2 periods for a comparison.",
                HISTORY_FIELDS | LIST_FIELDS | {"offset"},
            ),
            "pivot_report": Method(
                _reader("runPivotReport", paging=False, limits=False),
                "A pivot report. Each pivot has its own limit.",
                HISTORY_FIELDS | {"pivots"},
            ),
            "realtime_report": Method(
                _reader("runRealtimeReport", paging=False),
                "The last 30 minutes.",
                SHARED_FIELDS | LIST_FIELDS | {"minuteRanges"},
            ),
            "metadata": Method(
                _metadata, "All dimensions and metrics of this property, with custom ones."
            ),
        },
        options={
            "dimensions_and_metrics": f"dimensions and metrics list the standard names of "
            f"{SCHEMA_DOCS}, as of {NAMES_TAKEN_ON}. Call the method metadata only for custom "
            "names or for a name that this list does not hold: it is a real API request.",
            "dimensions": list(STANDARD_DIMENSIONS),
            "metrics": list(STANDARD_METRICS),
            "filters": "dimensionFilter and metricFilter take a FilterExpression: andGroup, "
            "orGroup, notExpression, or filter. A filter has fieldName and 1 of stringFilter, "
            "inListFilter, numericFilter, betweenFilter, or emptyFilter.",
            "string_match_types": [
                "EXACT",
                "BEGINS_WITH",
                "ENDS_WITH",
                "CONTAINS",
                "FULL_REGEXP",
                "PARTIAL_REGEXP",
            ],
            "dates": "YYYY-MM-DD, NdaysAgo, yesterday, or today.",
            "row_limit": "1 to 1000 for each call here. Read more rows with offset.",
        },
        gaps=(
            "Explorations and funnel reports: the v1beta API has no method for them, and the "
            "funnel method is only in v1alpha.",
            "Rows below the thresholds of GA4: metadata.subjectToThresholding shows this.",
            "Rows rolled into (other): metadata.dataLossFromOtherRow shows this.",
        ),
    )
}
