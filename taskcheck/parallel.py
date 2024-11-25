import subprocess
import re
from dataclasses import dataclass

from datetime import datetime, timedelta
from taskcheck.common import (
    AVOID_STATUS,
    console,
    get_calendars,
    get_long_range_time_map,
    get_tasks,
    hours_to_pdth,
    pdth_to_hours,
    hours_to_decimal,
)


@dataclass
class UrgencyCoefficients:
    estimated: dict
    inherit: bool
    active: float
    age_max: int
    urgency_due: int
    urgency_age: int


def get_urgency_coefficients():
    """
    Retrieves urgency coefficients from Taskwarrior configurations.
    Returns a dictionary mapping 'estimated.<value>.coefficient' to its float value and a
    boolean indicating if the urgency should be inherited by its dependants (`urgency.inherit`).
    """
    result = subprocess.run(["task", "_show"], capture_output=True, text=True)
    inherit_urgency = False
    active_task_coefficient = 0
    est_coeffs = {}
    urgency_age_max = 0
    urgency_age = 0
    urgency_due = 0
    pattern1 = re.compile(r"^urgency\.uda\.estimated\.(.+)\.coefficient=(.+)$")
    pattern2 = re.compile(r"^urgency\.inherit=(.+)$")
    pattern3 = re.compile(r"^urgency\.active\.coefficient=(.+)$")
    pattern4 = re.compile(r"^urgency\.age\.max=(.+)$")
    pattern5 = re.compile(r"^urgency\.due\.coefficient=(.+)$")
    pattern6 = re.compile(r"^urgency\.age\.coefficient=(.+)$")
    for line in result.stdout.splitlines():
        match = pattern1.match(line)
        if match:
            estimated_value = match.group(1)
            coefficient = float(match.group(2))
            est_coeffs[estimated_value] = coefficient

        match = pattern2.match(line)
        if match:
            inherit_urgency = match.group(1) == "1"

        match = pattern3.match(line)
        if match:
            active_coefficient = float(match.group(1))
            active_task_coefficient = active_coefficient

        match = pattern4.match(line)
        if match:
            urgency_age_max = int(match.group(1))

        match = pattern5.match(line)
        if match:
            urgency_due = int(match.group(1))

        match = pattern6.match(line)
        if match:
            urgency_age = int(match.group(1))

    return UrgencyCoefficients(
        est_coeffs,
        inherit_urgency,
        active_task_coefficient,
        urgency_age_max,
        urgency_due,
        urgency_age,
    )


def check_tasks_parallel(config, verbose=False):
    tasks = get_tasks()
    time_maps = config["time_maps"]
    days_ahead = config["scheduler"]["days_ahead"]
    calendars = get_calendars(config)
    urgency_coefficients = get_urgency_coefficients()

    task_info = initialize_task_info(
        tasks, time_maps, days_ahead, urgency_coefficients, calendars
    )

    for day_offset in range(days_ahead):
        allocate_time_for_day(task_info, day_offset, urgency_coefficients, verbose)

    update_tasks_with_scheduling_info(task_info, verbose)


def initialize_task_info(tasks, time_maps, days_ahead, urgency_coefficients, calendars):
    task_info = {}
    today = datetime.today().date()
    for task in tasks:
        if task.get("status") in AVOID_STATUS:
            continue
        if "estimated" not in task or "time_map" not in task:
            continue
        estimated_hours = pdth_to_hours(task["estimated"])
        time_map_names = task["time_map"].split(",")
        task_time_map, today_used_hours = get_long_range_time_map(
            time_maps, time_map_names, days_ahead, calendars
        )
        task_uuid = task["uuid"]
        initial_urgency = float(task.get("urgency", 0))
        estimated_urgency = urgency_estimated(
            {"remaining_hours": estimated_hours}, None, urgency_coefficients
        )
        due_urgency = urgency_due({"task": task}, today, urgency_coefficients)
        age_urgency = urgency_age({"task": task}, today, urgency_coefficients)
        task_info[task_uuid] = {
            "task": task,
            "remaining_hours": estimated_hours,
            "task_time_map": task_time_map,
            "today_used_hours": today_used_hours,
            "scheduling": {},
            "urgency": initial_urgency,
            "estimated_urgency": estimated_urgency,
            "due_urgency": due_urgency,
            "age_urgency": age_urgency,
            "started": False,
        }
    return task_info


def allocate_time_for_day(task_info, day_offset, urgency_coefficients, verbose):
    date = datetime.today().date() + timedelta(days=day_offset)
    total_available_hours = compute_total_available_hours(task_info, day_offset)
    # if verbose:
    #     print(f"Day {date}, total available hours: {total_available_hours:.2f}")
    if total_available_hours <= 0:
        return

    day_remaining_hours = total_available_hours
    tasks_remaining = prepare_tasks_remaining(task_info, day_offset)

    while day_remaining_hours > 0 and tasks_remaining:
        recompute_urgencies(tasks_remaining, urgency_coefficients, date)
        sorted_task_ids = sorted(
            tasks_remaining.keys(),
            key=lambda x: tasks_remaining[x]["urgency"],
            reverse=True,
        )

        allocated = False
        for uuid in sorted_task_ids:
            if uuid not in tasks_remaining:
                # already completed
                continue
            info = tasks_remaining[uuid]
            if any(d in tasks_remaining for d in info["task"].get("depends", [])):
                # cannot execute this task until all its dependencies are completed
                if verbose:
                    print(
                        "Skipping task",
                        info["task"]["id"],
                        "due to dependency on:",
                        [
                            tasks_remaining[_d]["task"]["id"]
                            for _d in info["task"].get("depends", [])
                            if _d in tasks_remaining
                        ],
                    )
                continue

            wait = info["task"].get("wait")
            if wait and date <= datetime.strptime(wait, "%Y%m%dT%H%M%SZ").date():
                if verbose:
                    print(f"Skipping task {info['task']['id']} due to wait date {wait}")
                continue
            allocation = allocate_time_to_task(info, day_offset, day_remaining_hours)
            if allocation > 0:
                day_remaining_hours -= allocation
                allocated = True
                date_str = date.isoformat()
                update_task_scheduling(info, allocation, date_str)
                if verbose:
                    print(
                        f"Allocated {allocation:.2f} hours to task {info['task']['id']} on {date} with urgency {info['urgency']:.2f} and estimated-related urgency {info['estimated_urgency']:.2f}"
                    )
                if (
                    info["remaining_hours"] <= 0
                    or info["task_time_map"][day_offset] <= 0
                ):
                    del tasks_remaining[uuid]
                # if day_remaining_hours <= 0:
                break
        if not allocated:
            break
    # if verbose and day_remaining_hours > 0:
    #     print(f"Unused time on {date}: {day_remaining_hours:.2f} hours")


def compute_total_available_hours(task_info, day_offset):
    if day_offset == 0:
        total_hours_list = [
            info["task_time_map"][day_offset] - info["today_used_hours"]
            for info in task_info.values()
        ]
    else:
        total_hours_list = [
            info["task_time_map"][day_offset] for info in task_info.values()
        ]
    total_available_hours = max(total_hours_list) if total_hours_list else 0
    return total_available_hours


def prepare_tasks_remaining(task_info, day_offset):
    return {
        info["task"]["uuid"]: info
        for info in task_info.values()
        if info["remaining_hours"] > 0 and info["task_time_map"][day_offset] > 0
    }


def urgency_due(info, date, urgency_coefficients):
    lfs = 0
    task = info["task"]
    if "due" in task:
        due = datetime.strptime(task["due"], "%Y%m%dT%H%M%SZ").date()
        # Map a range of 21 days to the value 0.2 - 1.0
        days_overdue = (date - due).days
        if days_overdue >= 7.0:
            lfs = 1.0  # < 1 wk ago
        elif days_overdue >= -14.0:
            lfs = ((days_overdue + 14.0) * 0.8 / 21.0) + 0.2
        else:
            lfs = 0.2  # 2 wks
    return lfs * urgency_coefficients.urgency_due


def urgency_age(info, date, urgency_coefficients):
    urgency_age_max = urgency_coefficients.age_max
    lfs = 1.0
    task = info["task"]
    if "entry" not in task:
        return 1.0
    entry = datetime.strptime(task["entry"], "%Y%m%dT%H%M%SZ").date()
    age = (date - entry).days  # in days
    if urgency_age_max == 0 or age >= urgency_age_max:
        lfs = 1.0
    return lfs * age / urgency_age_max * urgency_coefficients.urgency_age


def urgency_estimated(info, date, urgency_coefficients):
    """
    Computes the estimated urgency for the given remaining hours using the coefficients.
    """
    # Find the closest match (e.g., if '2h' is not available, use '1h' or '3h')
    closest_match = min(
        urgency_coefficients.estimated.keys(),
        key=lambda x: abs(pdth_to_hours(x) - info["remaining_hours"]),
    )
    coefficient = urgency_coefficients.estimated[closest_match]
    return coefficient


def update_urgency(info, urgency_key, urgency_compute_fn, urgency_coefficients, date):
    urgency_value = urgency_compute_fn(info, date, urgency_coefficients)
    old_urgency = info[urgency_key]
    # if old_urgency != urgency_value:
    #     print(
    #         f"new urgency value for task {info['task']['id']}: {urgency_key}: {urgency_value}"
    #     )
    info[urgency_key] = urgency_value
    info["urgency"] = info["urgency"] - old_urgency + urgency_value


def recompute_urgencies(tasks_remaining, urgency_coefficients, date):
    """Recompute urgency simulating that today is `date`"""
    # Recompute estimated urgencies as before
    for info in tasks_remaining.values():
        # Update estimated urgency
        update_urgency(
            info, "estimated_urgency", urgency_estimated, urgency_coefficients, date
        )
        # Update due partial urgency
        update_urgency(info, "due_urgency", urgency_due, urgency_coefficients, date)
        # Update age partial urgency
        update_urgency(info, "age_urgency", urgency_age, urgency_coefficients, date)

        started_by_user = info["task"].get("start", "") != ""
        started_by_scheduler = info["started"]
        if started_by_scheduler and not started_by_user:
            # If the task was started by the scheduler, apply the active task coefficient
            info["urgency"] += urgency_coefficients.active
            info["started"] = False

    if urgency_coefficients.inherit:
        # Build reverse dependencies mapping
        reverse_deps = {}  # Map from task_uuid to list of tasks that depend on it
        for task_uuid, info in tasks_remaining.items():
            for dep_uuid in info["task"].get("depends", []):
                if dep_uuid not in reverse_deps:
                    reverse_deps[dep_uuid] = []
                reverse_deps[dep_uuid].append(task_uuid)

        # Define a recursive function to compute the maximum urgency
        def get_max_urgency(info, visited):
            task_uuid = info["task"]["uuid"]
            if task_uuid in visited:
                return visited[task_uuid]  # Return cached value to avoid cycles
            # Start with the current task's urgency
            urgency = info["urgency"]
            visited[task_uuid] = urgency  # Mark as visited
            # Recursively compute urgencies of tasks that depend on this task
            for dep_uuid in reverse_deps.get(task_uuid, []):
                if dep_uuid in tasks_remaining:
                    dep_info = tasks_remaining[dep_uuid]
                    dep_urgency = get_max_urgency(dep_info, visited)
                    urgency = max(urgency, dep_urgency)
            visited[task_uuid] = urgency  # Update with the maximum urgency found
            return urgency

        # Update urgencies based on tasks that depend on them
        for info in tasks_remaining.values():
            visited = {}  # Reset visited dictionary for each task
            max_urgency = get_max_urgency(info, visited)
            info["urgency"] = max_urgency  # Update the task's urgency


def allocate_time_to_task(info, day_offset, day_remaining_hours):
    task_daily_available = info["task_time_map"][day_offset]
    if task_daily_available <= 0:
        return 0

    allocation = min(
        info["remaining_hours"],
        task_daily_available,
        day_remaining_hours,
        hours_to_decimal(info["task"].get("min_block", 2)),
    )

    if allocation <= 0.05:
        return 0

    if info["remaining_hours"] == info["task"]["estimated"]:
        info["started"] = True
    info["remaining_hours"] -= allocation
    info["task_time_map"][day_offset] -= allocation

    return allocation


def update_task_scheduling(info, allocation, date_str):
    if date_str not in info["scheduling"]:
        info["scheduling"][date_str] = 0
    info["scheduling"][date_str] += allocation


def update_tasks_with_scheduling_info(task_info, verbose):
    for info in task_info.values():
        task = info["task"]
        scheduling_note = ""
        scheduled_dates = sorted(info["scheduling"].keys())
        if not scheduled_dates:
            continue
        start_date = scheduled_dates[0]
        end_date = scheduled_dates[-1]
        for date_str in scheduled_dates:
            hours = info["scheduling"][date_str]
            scheduling_note += f"{date_str} - {hours_to_pdth(hours)}\n"

        subprocess.run(
            [
                "task",
                str(task["id"]),
                "modify",
                f"scheduled:{start_date}",
                f"completion_date:{end_date}",
                f'scheduling:"{scheduling_note.strip()}"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        due = task.get("due")
        end_date = datetime.strptime(end_date, "%Y-%m-%d")
        if due is not None and end_date > datetime.strptime(due, "%Y%m%dT%H%M%SZ"):
            console.print(
                f"[red]Warning: Task {task['id']} ('{task['description']}') is not going to be completed on time.[/red]"
            )

        if verbose:
            print(
                f"Updated task {task['id']} with scheduled dates {start_date} to {end_date}"
            )
