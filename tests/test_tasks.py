"""An agent asks for a thing by name and gets the call. Each call here runs for real."""

import json
from datetime import date, timedelta
from pathlib import Path

import httpx2
import pytest

from seo_mcp import core, providers, server, tasks
from test_query_api import HOST, SITE, answer

README = Path(__file__).parent.parent / "README.md"
FILLED = {"<page>": "/a", "<page_url>": f"https://{HOST}/a"}
STEPS = [
    pytest.param(task, recipe.api, at, step, id=f"{task}-{recipe.api}-{at}")
    for task, entry in tasks.TASKS.items()
    for recipe in entry.recipes
    for at, step in enumerate(recipe.steps)
]


@pytest.fixture
def live(write_config, fake_net, account, monkeypatch):
    monkeypatch.setenv("CLARITY_ENV", "clarity-token-value-1234")
    monkeypatch.setenv("GOOGLE_KEY_ENV", "google-key-value-1234")
    monkeypatch.setenv("BING_KEY_ENV", "bing-key-value-5678")
    shared = {
        **account,
        "google_api_key": {"env": "GOOGLE_KEY_ENV"},
        "bing_api_key": {"env": "BING_KEY_ENV"},
    }
    write_config(SITE, shared)
    return fake_net(answer)


def fill(value):
    text = json.dumps(value)
    for mark, real in FILLED.items():
        text = text.replace(mark, real)
    return json.loads(text)


@pytest.mark.parametrize(
    ("asked", "name"),
    [
        ("dead clicks", "dead_clicks"),
        ("dead_clicks", "dead_clicks"),
        ("Dead-Click", "dead_clicks"),
        ("deadclicks", "dead_clicks"),
        ("which pages have dead clicks", "dead_clicks"),
        ("rage clicks", "rage_clicks"),
        ("scroll depth by page", "scroll_depth_by_page"),
        ("how far do people scroll", "scroll_depth_by_page"),
        ("most clicked elements", "most_clicked_elements"),
        ("what do people click most", "most_clicked_elements"),
        ("quickback clicks", "quickback_clicks"),
        ("quick back", "quickback_clicks"),
        ("1 page over 2 periods", "page_over_two_periods"),
        ("compare a page between two periods", "page_over_two_periods"),
        ("search queries of 1 page", "queries_of_page"),
        ("which queries bring people to this page", "queries_of_page"),
        ("core web vitals", "core_web_vitals"),
    ],
)
async def test_an_ask_finds_its_task(asked, name):
    data = await core.query_api(task=asked)
    assert data["status"] == "ok"
    assert {row["task"] for row in data["rows"]} == {name}


async def test_an_ask_can_find_2_tasks():
    data = await core.query_api(task="dead clicks and rage clicks")
    assert {row["task"] for row in data["rows"]} == {"dead_clicks", "rage_clicks"}


@pytest.mark.parametrize("asked", ["the weather", "click", " ", "page"])
async def test_an_unknown_ask_lists_the_tasks_and_guesses_no_call(asked):
    data = await core.query_api(task=asked)
    assert data["status"] == "unsupported"
    assert data["accepts"] == sorted(tasks.TASKS)
    assert data["rows"] == []


async def test_discovery_needs_no_network_and_no_sites_file(fake_net, monkeypatch, tmp_path):
    monkeypatch.setenv("SEO_MCP_CONFIG", str(tmp_path / "missing.json"))
    calls = fake_net(answer)
    for name in tasks.TASKS:
        assert (await core.query_api(task=name))["status"] == "ok"
    assert (await core.query_api())["tasks"]
    assert not calls


async def test_a_task_goes_with_no_method_and_no_request():
    for extra in ({"method": "recordings"}, {"request": {"count": 1}}):
        data = await core.query_api(api="clarity", task="dead clicks", **extra)
        assert data["status"] == "error"
        assert "task" in data["reason"]


async def test_api_keeps_1_provider_of_a_task():
    data = await core.query_api(api="ga4", task="scroll depth")
    assert [row["api"] for row in data["rows"]] == ["ga4"]
    wrong = await core.query_api(api="bing", task="dead clicks")
    assert wrong["status"] == "unsupported"
    assert await core.query_api(api="nope", task="dead clicks") == await core.query_api(api="nope")


async def test_dead_clicks_ask_the_count_first_then_the_elements():
    data = await core.query_api(site=HOST, task="which pages have dead clicks")
    (row,) = data["rows"]
    first, second = row["steps"]
    assert first["args"]["method"] == "dashboard_query"
    assert first["args"]["site"] == HOST
    assert "Dead click count per page between" in first["args"]["request"]["query"]
    assert "TotalDeadClicks" in first["read"]
    assert second["args"]["method"] == "recordings"
    assert second["args"]["request"]["filters"]["deadClickPresent"] is True
    assert '"Dead click"' in second["read"]
    assert "sample" in second["read"]
    assert not row["needs"]


def test_dates_are_real_and_a_window_has_the_named_length():
    filled = tasks.build("dead_clicks", "", "", today=date(2026, 11, 6))
    assert filled["window"] == {"start": "2026-08-09", "end": "2026-11-06", "days": 90}
    steps = filled["rows"][0]["steps"]
    assert "2026/08/09 and 2026/11/06" in steps[0]["args"]["request"]["query"]
    span = steps[1]["args"]["request"]["filters"]["date"]
    assert span == {"start": "2026-08-09T00:00:00.000Z", "end": "2026-11-06T23:59:59.999Z"}


def test_2_periods_have_the_same_length_and_do_not_overlap():
    filled = tasks.build("page_over_two_periods", "", "", today=date(2026, 11, 6))
    assert filled["window"] == {
        "start": "2026-10-06",
        "end": "2026-11-02",
        "days": 28,
        "compare_start": "2026-09-08",
        "compare_end": "2026-10-05",
    }


@pytest.mark.parametrize("today", [date(2026, 3, 18), date(2026, 3, 19)])
def test_a_task_end_with_a_lag_is_complete_in_each_zone(today):
    task = tasks.TASKS["page_over_two_periods"]
    end = date.fromisoformat(
        tasks.build("page_over_two_periods", "", "", today=today)["window"]["end"]
    )
    # The day of a place is the UTC date, the day before, or the day after.
    for place in (today - timedelta(days=1), today, today + timedelta(days=1)):
        assert end <= place - timedelta(days=task.lag), place


def test_a_ga4_task_step_sends_relative_dates_that_google_reads_in_the_property_zone():
    built = [
        tasks.build("scroll_depth_by_page", "", "ga4", today=day)
        for day in (date(2026, 3, 18), date(2026, 3, 19))
    ]
    ranges = [
        row["steps"][0]["args"]["request"]["dateRanges"] for item in built for row in item["rows"]
    ]
    assert ranges[0] == ranges[1] == [{"startDate": "89daysAgo", "endDate": "today"}]


async def test_a_step_names_the_input_that_it_needs():
    data = await core.query_api(task="queries_of_page")
    needs = {row["api"]: row["needs"] for row in data["rows"]}
    assert needs == {"search_console": ["<page>"], "bing": ["<page_url>"], "ga4": []}


async def test_a_provider_with_no_answer_says_why_and_gives_no_call():
    data = await core.query_api(task="queries_of_page")
    (row,) = [row for row in data["rows"] if row["api"] == "ga4"]
    assert row["steps"] == []
    assert "search quer" in row["not_available"]


# The catalog cannot drift: each step runs through the public tools, with only the network fake.
@pytest.mark.parametrize(("task", "api", "at", "step"), STEPS)
async def test_each_step_runs_through_the_real_tool(live, task, api, at, step):
    built = tasks.build(task, HOST, api, today=date.today())
    sent_step = next(row for row in built["rows"] if row["api"] == api)["steps"][at]
    args = fill(sent_step["args"])
    assert sent_step["tool"] in {"query_api", "get_seo_data"}
    run = core.query_api if sent_step["tool"] == "query_api" else core.get_seo_data
    data = await run(**args)
    assert data["status"] == "ok", data["reason"]
    for name, result in data.get("sources", {}).items():
        assert result["status"] == "ok", (name, result["reason"])
    assert live


def test_each_step_of_get_seo_data_names_its_sources():
    for _, _, _, step in (param.values for param in STEPS):
        if step.tool == "get_seo_data":
            assert step.args["sources"], "A call with no sources runs every source."


def test_each_api_has_a_task():
    covered = {recipe.api for entry in tasks.TASKS.values() for recipe in entry.recipes}
    assert set(providers.APIS) <= covered
    assert covered <= set(providers.APIS)


def test_each_clarity_recipe_warns_about_the_quota():
    for entry in tasks.TASKS.values():
        for recipe in entry.recipes:
            if recipe.api == "clarity":
                assert tasks.CLARITY_QUOTA in recipe.limits


def test_tool_description_and_readme_list_each_task():
    readme = README.read_text()
    for name in tasks.TASKS:
        assert name in server.QUERY_API
        assert f"`{name}`" in readme


def test_the_server_instructions_point_to_tasks():
    assert "task" in server.server.instructions


def sampled_recipes():
    """Each recipe that asks the dashboard for a count, then spends a call on a sample."""
    for name, entry in tasks.TASKS.items():
        for recipe in entry.recipes:
            methods = [step.args.get("method") for step in recipe.steps]
            if "dashboard_query" in methods and "recordings" in methods:
                yield name, recipe


def test_the_click_tasks_keep_2_steps_with_the_dashboard_first():
    found = {name: recipe for name, recipe in sampled_recipes()}
    assert set(found) == {"dead_clicks", "rage_clicks", "quickback_clicks"}
    for recipe in found.values():
        methods = [step.args["method"] for step in recipe.steps]
        assert methods == ["dashboard_query", "recordings"]


async def test_a_count_of_0_stops_the_sample_step():
    for name, recipe in sampled_recipes():
        assert tasks.ZERO_COUNT_STOP in recipe.limits, name
        data = await core.query_api(api="clarity", task=name)
        first, second = data["rows"][0]["steps"]
        assert "skip_if" not in first
        assert second["skip_if"] == tasks.ZERO_COUNT_STOP
        assert "do not call recordings" in tasks.ZERO_COUNT_STOP


def test_the_readme_states_the_rule_of_a_count_of_0():
    assert "do not call `recordings`" in README.read_text()


# The column and the words of each click count, as a real dashboard answer names them.
COUNTS = {
    "dead_clicks": ("TotalDeadClicks", "dead click"),
    "rage_clicks": ("TotalRageClicks", "rage click"),
    "quickback_clicks": ("TotalQuickbackClicks", "quickback click"),
}


def rewritten(name, window, start=None, end=None):
    """The question as Clarity rewrites it, from a real answer."""
    first = (start or window["start"]).replace("-", "/")
    last = (end or window["end"]).replace("-", "/")
    words = COUNTS[name][1]
    return (
        f"Total {words} counts per visited page for non-bot users between "
        f"'{first} 00:00:00' and '{last} 23:59:59', showing top 10 pages by {words}s."
    )


def dashboard(fake_net, rows, question):
    def reply(request):
        if request.url.path.endswith("/mcp/dashboard/query"):
            return httpx2.Response(200, json={"data": rows, "query": question})
        if request.url.path.endswith("/mcp/recordings/sample"):
            return httpx2.Response(200, json=[])
        return answer(request)

    return fake_net(reply)


def clarity_paths(calls):
    return [call.url.path for call in calls if "clarity" in call.url.host]


async def run_recipe(name, fake_net, rows=None, question=None):
    """Run the 2 steps of the task as an agent that ignores skip_if."""
    data = await core.query_api(HOST, "clarity", "", None, name)
    column = COUNTS[name][0]
    window = data["rows"][0]["window"]
    rows = [{"VisitedUrl": f"https://{HOST}/a", column: 0}] if rows is None else rows
    calls = dashboard(fake_net, rows, question or rewritten(name, window))
    first, second = data["rows"][0]["steps"]
    count = await core.query_api(**first["args"])
    sample = await core.query_api(**second["args"])
    return count, sample, calls


@pytest.mark.parametrize("name", sorted(COUNTS))
async def test_a_0_count_makes_the_server_skip_the_sample(live, fake_net, name):
    count, sample, calls = await run_recipe(name, fake_net)
    assert count["status"] == "ok"
    assert count["calls_used_today"] == 1
    assert sample["status"] == "skipped", (
        f"{name}: the server sent the sample after a 0 count. In src/seo_mcp/tasks.py, build "
        "the dashboard question of the recipe from COUNTS, so that clarity._zero_count finds it."
    )
    assert "0" in sample["reason"]
    assert clarity_paths(calls) == ["/mcp/dashboard/query"]
    assert sample["calls_used_today"] == 1
    assert sample["rows"] == []


def test_each_click_task_has_a_count_that_the_server_reads():
    """A new click task must name its count, so that the server can enforce skip_if."""
    assert {name for name, _ in sampled_recipes()} == set(COUNTS), (
        "Each click task needs a row in tasks.COUNTS and in COUNTS of tests/test_tasks.py."
    )


async def test_a_count_above_0_sends_the_sample(live, fake_net):
    rows = [
        {"VisitedUrl": f"https://{HOST}/a", "TotalDeadClicks": 3},
        {"VisitedUrl": f"https://{HOST}/b", "TotalDeadClicks": 0},
    ]
    _, sample, calls = await run_recipe("dead_clicks", fake_net, rows)
    assert sample["status"] == "ok"
    assert clarity_paths(calls) == ["/mcp/dashboard/query", "/mcp/recordings/sample"]
    assert sample["calls_used_today"] == 2


async def test_no_saved_count_sends_the_sample(live, fake_net):
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    calls = dashboard(fake_net, [], "")
    sample = await core.query_api(**data["rows"][0]["steps"][1]["args"])
    assert sample["status"] == "ok"
    assert clarity_paths(calls) == ["/mcp/recordings/sample"]
    assert sample["calls_used_today"] == 1


ZERO = [{"VisitedUrl": f"https://{HOST}/a", "TotalDeadClicks": 0}]


@pytest.mark.parametrize(
    ("rows", "question"),
    [
        pytest.param([], None, id="no-rows"),
        pytest.param([{"VisitedUrl": "/a", "PageViews": 0}], None, id="no-count-column"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": None}], None, id="null-count"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": "1e-400"}], None, id="tiny-text"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": 0.0}], None, id="float"),
        pytest.param([{"VisitedUrl": "/a", "TotalDeadClicks": False}], None, id="bool"),
        pytest.param([{"TotalDeadClicks": 0, "DeadClickSessions": 0}], None, id="2-count-columns"),
        pytest.param(ZERO, "Total dead click counts per visited page", id="no-dates"),
        pytest.param(
            ZERO,
            "Total rage click counts between '{start} 00:00:00' and '{end} 23:59:59'",
            id="other-signal",
        ),
        pytest.param(
            ZERO,
            "Total dead click counts between '2026/01/01 00:00:00' and '{end} 23:59:59'",
            id="other-start",
        ),
        pytest.param(
            ZERO,
            "Total dead click counts between '{start} 00:00:00' and "
            "'{end} 23:59:59', showing top 10 pages by sessions.",
            id="top-by-other",
        ),
    ],
)
async def test_an_uncertain_0_still_sends_the_sample(live, fake_net, rows, question):
    """A wrong skip hides data, so each doubt sends the sample as before."""
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    start, end = (data["rows"][0]["window"][key].replace("-", "/") for key in ("start", "end"))
    asked = question.format(start=start, end=end) if question else None
    _, sample, calls = await run_recipe("dead_clicks", fake_net, rows, asked)
    assert sample["status"] == "ok"
    assert clarity_paths(calls)[-1] == "/mcp/recordings/sample"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"visitedUrls": [{"url": "/a", "operator": "contains"}]}, id="extra-filter"),
        pytest.param({"deadClickPresent": False}, id="flag-false"),
        pytest.param({"rageClickPresent": True}, id="other-flag"),
    ],
)
async def test_a_sample_of_another_question_still_goes_out(live, fake_net, change):
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    calls = dashboard(fake_net, ZERO, rewritten("dead_clicks", data["rows"][0]["window"]))
    first, second = data["rows"][0]["steps"]
    await core.query_api(**first["args"])
    args = json.loads(json.dumps(second["args"]))
    filters = args["request"]["filters"]
    filters.pop("deadClickPresent")
    filters |= change
    sample = await core.query_api(**args)
    assert sample["status"] == "ok"
    assert clarity_paths(calls)[-1] == "/mcp/recordings/sample"


async def test_a_partial_day_window_still_sends_the_sample(live, fake_net):
    data = await core.query_api(HOST, "clarity", "", None, "dead_clicks")
    calls = dashboard(fake_net, ZERO, rewritten("dead_clicks", data["rows"][0]["window"]))
    first, second = data["rows"][0]["steps"]
    await core.query_api(**first["args"])
    args = json.loads(json.dumps(second["args"]))
    span = args["request"]["filters"]["date"]
    span["end"] = span["end"].replace("23:59:59.999Z", "12:00:00.000Z")
    sample = await core.query_api(**args)
    assert sample["status"] == "ok"
    assert clarity_paths(calls)[-1] == "/mcp/recordings/sample"
