"""The job registry, its listing endpoint, and on-demand rescans."""

import pytest

from tasks import TaskRegistry


@pytest.fixture
def registry():
    return TaskRegistry(ttl=900, max_tasks=200)


# --- the registry ---------------------------------------------------------------


def test_a_new_task_is_active_with_no_error(registry):
    task = registry.get(registry.create("download"))

    assert task["kind"] == "download"
    assert task["active"] is True
    assert task["progress"] == 0
    assert task["error"] is None


def test_listing_returns_every_task_newest_first(registry):
    first = registry.create("scan")
    second = registry.create("download")

    listed = registry.list_all()

    assert [task["id"] for task in listed] == [second, first]


def test_a_completed_task_is_no_longer_active(registry):
    task_id = registry.create("download")
    registry.update(task_id, status="completed", progress=100)

    task = registry.get(task_id)

    assert task["active"] is False
    assert task["finished_age"] is not None


def test_a_failed_task_carries_its_error(registry):
    task_id = registry.create("download")
    registry.update(task_id, status="failed", error="boom")

    assert registry.get(task_id)["error"] == "boom"


def test_a_detail_summary_survives_to_the_client(registry):
    """A batch import that added some and skipped others needs to say so."""
    task_id = registry.create("import")
    registry.update(task_id, status="completed", detail="Imported 3 of 5")

    assert registry.get(task_id)["detail"] == "Imported 3 of 5"


def test_an_active_task_has_no_finished_age(registry):
    assert registry.get(registry.create("scan"))["finished_age"] is None


def test_internal_timestamps_are_not_exposed(registry):
    """Monotonic clock values are meaningless to a client."""
    task = registry.get(registry.create("scan"))

    assert "created_at" not in task
    assert "finished_at" not in task


def test_updating_an_unknown_task_is_a_no_op(registry):
    registry.update("nope", status="completed")

    assert registry.get("nope") is None


def test_finished_tasks_expire(registry):
    short = TaskRegistry(ttl=0, max_tasks=200)
    task_id = short.create("download")
    short.update(task_id, status="completed")

    assert short.get(task_id) is None


def test_active_tasks_are_never_expired(registry):
    """A long transcode must not vanish from the tray while it is still running."""
    short = TaskRegistry(ttl=0, max_tasks=200)
    task_id = short.create("download")

    assert short.get(task_id) is not None


def test_the_task_count_is_capped(registry):
    small = TaskRegistry(ttl=900, max_tasks=3)
    ids = [small.create("download") for _ in range(5)]

    listed = small.list_all()

    assert len(listed) == 3
    # The oldest are the ones dropped.
    assert {task["id"] for task in listed} == set(ids[2:])


# --- the endpoints ----------------------------------------------------------------


def test_the_listing_endpoint_reports_running_jobs(client, app_env):
    test_client, main = client
    task_id = main.registry.create("download")
    main.registry.update(task_id, status="downloading", progress=40)

    response = test_client.get("/api/tasks")

    assert response.status_code == 200
    listed = {task["id"]: task for task in response.json()["tasks"]}
    assert listed[task_id]["progress"] == 40
    assert listed[task_id]["kind"] == "download"


def test_a_job_started_elsewhere_is_visible(client, app_env):
    """The whole point: the id used to live only in the starting tab's script."""
    test_client, main = client
    task_id = main.registry.create("import")

    ids = [task["id"] for task in test_client.get("/api/tasks").json()["tasks"]]

    assert task_id in ids


def test_the_listing_is_empty_when_nothing_has_run(client, app_env):
    test_client, main = client
    main.registry._tasks.clear()

    assert test_client.get("/api/tasks").json()["tasks"] == []


def test_a_single_task_status_is_still_available(client, app_env):
    test_client, main = client
    task_id = main.registry.create("scan")

    response = test_client.get(f"/api/task-status/{task_id}")

    assert response.status_code == 200
    assert response.json()["id"] == task_id


def test_an_unknown_task_status_is_404(client, app_env):
    test_client, _ = client

    assert test_client.get("/api/task-status/nope").status_code == 404


def test_a_rescan_can_be_started_on_demand(client, app_env):
    """Scanning previously happened only at startup and on a folder switch."""
    test_client, main = client

    response = test_client.post("/api/scan")

    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert main.registry.get(task_id) is not None


def test_the_rescan_task_appears_in_the_listing(client, app_env):
    test_client, _ = client
    task_id = test_client.post("/api/scan").json()["task_id"]

    ids = [task["id"] for task in test_client.get("/api/tasks").json()["tasks"]]

    assert task_id in ids
