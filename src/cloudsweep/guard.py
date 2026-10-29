"""Budget guard and control handler (Lambda). AWS invokes the handlers; tests supply
in-memory clients.

No billing alert is a strict spend cap. AWS billing data arrive hours after usage, so the
cutoff stops spending after the reported amount crosses the threshold; final charges can
exceed it. The scheduled invocation checks the delayed billing data, reconciles a latched
cutoff, publishes a spend snapshot for agents, watches for stuck jobs and checks that
capacity returns to zero.
"""

import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

WAITING = ("SUBMITTED", "PENDING", "RUNNABLE")
ACTIVE = ("STARTING", "RUNNING")


def initial_state(project, region):
    return {"schema_version": 1, "cutoff_active": False, "project": project, "region": region}


def valid_state(state, project, region):
    return (isinstance(state, dict) and type(state.get("schema_version")) is int
            and state.get("schema_version") == 1
            and type(state.get("cutoff_active")) is bool
            and state.get("project") == project and state.get("region") == region)


def accepted_event(event, config):
    """Permissions authenticate the caller; also reject other sources in code.

    Budget SNS message bodies are prose, not a stable API. Only the dedicated cutoff topic
    is accepted, and DescribeBudget determines actual spend.
    """
    if not isinstance(event, dict):
        return False
    if event.get("source") == "aws.events":
        return (event.get("detail-type") == "Scheduled Event"
                and event.get("account") == config["ACCOUNT"]
                and event.get("region") == config["REGION"]
                and event.get("resources") == [config["RULE_ARN"]]
                and event.get("detail") == {})
    records = event.get("Records")
    return (isinstance(records, list) and bool(records)
            and all(isinstance(record, dict)
                    and record.get("EventSource") == "aws:sns"
                    and isinstance(record.get("Sns"), dict)
                    and record["Sns"].get("Type") == "Notification"
                    and record["Sns"].get("TopicArn") == config["TOPIC_ARN"]
                    for record in records))


def failure(error):
    return "%s: %s" % (type(error).__name__, error)


def attempt(errors, name, action, *args, **kwargs):
    """Record a failed operation while allowing independent safety actions to continue."""
    try:
        return action(*args, **kwargs)
    except Exception as error:
        errors.append("%s: %s" % (name, failure(error)))


def has_deny(iam, role, policy_arn):
    """An attached deny also records a latch if an SSM write failed earlier."""
    return any(policy["PolicyArn"] == policy_arn
               for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role)
               for policy in page.get("AttachedPolicies", []))


def validate_budget(budget, config):
    """Validate the shared guard/reset budget contract without making an AWS call."""
    expected = {"TagKeyValue": ["user:Project$" + config["PROJECT"]]}
    if (budget.get("BudgetName") != config["BUDGET_NAME"] or budget.get("BudgetType") != "COST"
            or budget.get("TimeUnit") != "MONTHLY" or budget.get("CostFilters") != expected
            or budget.get("BudgetLimit", {}).get("Unit") != "USD"):
        raise ValueError("budget identity, scope or currency changed; administrator must inspect")
    cost_types = budget.get("CostTypes", {})
    if any(cost_types.get(name) is not False for name in ("IncludeCredit", "IncludeRefund")) or any(
            cost_types.get(name, False) is not False for name in ("UseBlended", "UseAmortized")):
        raise ValueError("budget no longer measures unblended cost excluding credits and refunds")
    spend = budget["CalculatedSpend"]
    if spend["ActualSpend"]["Unit"] != "USD":
        raise ValueError("actual spend is not in USD")
    try:
        amount = Decimal(spend["ActualSpend"]["Amount"])
        limit = Decimal(budget["BudgetLimit"]["Amount"])
    except InvalidOperation as error:
        raise ValueError("budget contains an invalid amount") from error
    if not amount.is_finite() or limit != Decimal(config["CUTOFF_AMOUNT"]):
        raise ValueError("budget amount or cutoff configuration changed")
    result = {"actual_usd": str(amount), "cutoff_usd": str(limit),
              "budget_start": str(budget.get("TimePeriod", {}).get("Start", ""))}
    # ForecastedSpend is optional (especially for a new budget). It is display-only:
    # missing or invalid forecasts must neither look like zero nor prevent a cutoff.
    forecast = spend.get("ForecastedSpend", {})
    try:
        forecast_amount = Decimal(forecast["Amount"])
        if forecast.get("Unit") == "USD" and forecast_amount.is_finite():
            result["forecast_usd"] = str(forecast_amount)
    except (KeyError, TypeError, InvalidOperation):
        pass
    return result


def queue_jobs(batch, queue, children=True):
    """Every nonterminal job in the project queue (deduplicated), with array children unless
    children is False."""
    found = {}
    pages = batch.get_paginator("list_jobs")
    for status in WAITING + ACTIVE:
        for page in pages.paginate(jobQueue=queue, jobStatus=status):
            for job in page.get("jobSummaryList", []):
                found.setdefault(job["jobId"], dict(job, parent=None))
    if children:
        for child in children_of(batch, [job for job in found.values() if "size" in job.get("arrayProperties", {})]):
            found.setdefault(child["jobId"], child)
    return list(found.values())


def children_of(batch, parents):
    found = []
    pages = batch.get_paginator("list_jobs")
    for parent in parents:
        for status in WAITING + ACTIVE:
            for page in pages.paginate(arrayJobId=parent["jobId"], jobStatus=status):
                found += [dict(child, parent=parent["jobId"], jobName=parent["jobName"])
                          for child in page.get("jobSummaryList", [])]
    return found


def stop_all(batch, top, reason, remaining_ms=None):
    """Stop top-level jobs first: a stopped array parent stops its children, so children are
    listed only for parents whose stop failed. A queue of large arrays therefore cannot use
    up the deadline in enumeration before any stop request is sent."""
    done, errors = stop_jobs(batch, top, reason, remaining_ms)
    stopped = set(done["cancelled"] + done["terminated"])
    failed = [job for job in top if "size" in job.get("arrayProperties", {}) and job["jobId"] not in stopped
              and job["status"] in WAITING + ACTIVE]
    if failed and not any("deadline" in error for error in errors):
        try:
            children = children_of(batch, failed)
        except Exception as error:  # recorded; the next invocation retries
            return done, errors + ["list children: " + failure(error)]
        more, more_errors = stop_jobs(batch, children, reason, remaining_ms)
        for bucket in done:
            done[bucket] += more[bucket]
        errors += more_errors
    return done, errors


def leaves(jobs):
    return [job for job in jobs if "size" not in job.get("arrayProperties", {})]


def stop_jobs(batch, jobs, reason, remaining_ms=None):
    """Request stops; result buckets describe the observed, possibly stale job statuses.

    TerminateJob also cancels waiting jobs. CancelJob can silently do nothing if a job
    starts between ListJobs and the stop request. A successful parent request covers
    all array children; failed parents still allow individual child requests.
    """
    done, errors = {"cancelled": [], "terminated": []}, []
    covered = set()
    for index, job in enumerate(jobs):
        if job["status"] not in WAITING + ACTIVE:
            continue
        try:
            if job.get("parent") not in covered:
                if remaining_ms is not None and remaining_ms() < 20000:
                    errors.append("stop incomplete: %d jobs not processed before Lambda deadline; retry"
                                  % (len(jobs) - index))
                    break
                batch.terminate_job(jobId=job["jobId"], reason=reason)
                if "size" in job.get("arrayProperties", {}):
                    covered.add(job["jobId"])
            done["cancelled" if job["status"] in WAITING else "terminated"].append(job["jobId"])
        except Exception as error:
            errors.append("stop %s: %s" % (job["jobId"], failure(error)))
    return done, errors


def capacity(batch, ecs, environment):
    """Requested vCPUs and ECS registrations; neither inventories unregistered EC2 instances."""
    described = batch.describe_compute_environments(computeEnvironments=[environment])["computeEnvironments"]
    if len(described) != 1:
        raise ValueError("compute environment not found")
    ce = described[0]
    cluster = ce.get("ecsClusterArn")
    desired = ce.get("computeResources", {}).get("desiredvCpus")
    if not isinstance(cluster, str) or not cluster or type(desired) is not int or desired < 0:
        raise ValueError("compute environment has no valid desired capacity or ECS cluster; cannot confirm drain")
    instances = 0
    for page in ecs.get_paginator("list_container_instances").paginate(cluster=cluster):
        instances += len(page.get("containerInstanceArns", []))
    return {"desired_vcpus": desired,
            "max_vcpus": ce.get("computeResources", {}).get("maxvCpus", 0),
            "instances": instances, "state": ce.get("state"), "status": ce.get("status")}


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(s3, bucket, key):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception as error:
        if type(error).__name__ == "NoSuchKey" or getattr(error, "response", {}).get(
                "Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def write_json(s3, bucket, key, value):
    s3.put_object(Bucket=bucket, Key=key, Body=(json.dumps(value, sort_keys=True, indent=2) + "\n").encode(),
                  ContentType="application/json")


def watchdog(jobs, watch, now):
    """Find prolonged STARTING jobs to stop and waiting jobs needing an alert only.

    A non-stuck STARTING or RUNNING job resets the waiting timer at every observation.
    STARTING duration begins at first observation, not AWS's actual state transition.
    After an inactivity alert, wait another interval before sending a reminder.
    """
    starting_limit = timedelta(minutes=30)
    stall_limit = timedelta(minutes=180)
    seen = watch.get("starting", {})
    starting = {job["jobId"]: seen.get(job["jobId"], stamp(now))
                for job in leaves(jobs) if job["status"] == "STARTING"}
    stuck = [job for job in leaves(jobs) if job["status"] == "STARTING"
             and now - parse_time(starting[job["jobId"]]) > starting_limit]
    waiting = [job for job in leaves(jobs) if job["status"] in WAITING]
    progressing = any(job["status"] in ACTIVE for job in leaves(jobs) if job not in stuck)
    progress_at = stamp(now) if progressing or not waiting else watch.get("progress_at") or stamp(now)
    stalled = waiting if now - parse_time(progress_at) > stall_limit else []
    if stalled:
        progress_at = stamp(now)
    return stuck, stalled, dict(watch, starting=starting, progress_at=progress_at)


def check_capacity(found, busy, watch, now):
    """An error when capacity stays above zero with no work (or under a cutoff) past the grace."""
    if busy or (found["desired_vcpus"] == 0 and found["instances"] == 0):
        return None, dict(watch, capacity_since=None)
    since = watch.get("capacity_since") or stamp(now)
    watch = dict(watch, capacity_since=since)
    if now - parse_time(since) > timedelta(minutes=30):
        return ("capacity has not returned to zero since %s: %d desired vCPUs, %d instances; "
                "administrator must inspect the compute environment" % (
                    since, found["desired_vcpus"], found["instances"])), watch
    return None, watch


def cost_tag(ce, watch, now):
    """The Project cost-allocation tag's state, activating it once billing has seen it.

    AWS Budgets counts only active cost-allocation tags: until the tag is active the budget,
    and so the cutoff, sees no project spend. Checked at most once a day (Cost Explorer calls
    can be billed); the last answer is kept in the watch state between checks.
    """
    checked = watch.get("tag_checked_at")
    if checked and now - parse_time(checked) < timedelta(hours=24) and watch.get("tag_state"):
        return watch["tag_state"], watch
    try:
        found = ce.list_cost_allocation_tags(TagKeys=["Project"], Type="UserDefined").get("CostAllocationTags", [])
        if any(tag.get("TagKey") == "Project" and tag.get("Status") == "Active" for tag in found):
            state = "active"
        elif not found:
            state = "not yet seen by billing (AWS lists a new tag up to 24 hours after first use)"
        else:
            result = ce.update_cost_allocation_tags_status(
                CostAllocationTagsStatus=[{"TagKey": "Project", "Status": "Active"}])
            state = ("activation failed: %s" % json.dumps(result["Errors"], default=str) if result.get("Errors")
                     else "activation requested")
    except Exception as error:
        if "Linked account" in str(error):
            state = ("managed by the organization's management account, which must activate the Project tag "
                     "(this member account cannot check it)")
        else:
            state = "unknown (%s)" % failure(error)
    return state, dict(watch, tag_checked_at=stamp(now), tag_state=state)


def run(event, config, clients, now=None, remaining_ms=None):
    if not accepted_event(event, config):
        return {"ignored": "unexpected event source"}
    now = now or datetime.now(timezone.utc)
    project, region = config["PROJECT"], config["REGION"]
    ssm, iam, budgets, batch, ecs, s3, sns = (clients[name] for name in (
        "ssm", "iam", "budgets", "batch", "ecs", "s3", "sns"))
    # API errors (including missing state during initial provisioning) raise for
    # asynchronous retry and the next scheduled invocation. Workflow reads fail
    # closed too. Corrupt existing state is repaired to latched.
    raw_state = ssm.get_parameter(Name=config["STATE_PARAMETER"])["Parameter"]["Value"]
    try:
        state = json.loads(raw_state)
    except (TypeError, ValueError):
        state = None
    valid = valid_state(state, project, region)
    active = not valid or state["cutoff_active"]
    reason = "missing-or-invalid-state" if not valid else "previous-cutoff"
    if not active and has_deny(iam, config["AGENT_ROLE"], config["DENY_POLICY_ARN"]):
        active, reason = True, "recover-attached-deny"
    errors, notices, result = [], [], {}
    try:
        budget = budgets.describe_budget(AccountId=config["ACCOUNT"], BudgetName=config["BUDGET_NAME"])["Budget"]
        spend = validate_budget(budget, config)
    except Exception as error:
        if not active:
            raise  # without billing data the cutoff cannot be decided; retry
        spend = None
        errors.append("budget: " + failure(error))
    if not active and Decimal(spend["actual_usd"]) >= Decimal(config["CUTOFF_AMOUNT"]):
        active, reason = True, "actual-budget-cutoff"

    if active and (not valid or not state["cutoff_active"]):
        state = initial_state(project, region)
        state.update(cutoff_active=True, reason=reason, triggered_at=now.isoformat())
        notices.append("cutoff latched (%s): submissions denied, queue and compute environment disabled, "
                       "project jobs stopped; reset is an administrator operation" % reason)
        # The attached deny recovers a failed state write, even across a month boundary.
        attempt(errors, "state write", ssm.put_parameter, Name=config["STATE_PARAMETER"], Type="String",
                Value=json.dumps(state, separators=(",", ":")), Overwrite=True)
    if active:
        attempt(errors, "deny", iam.attach_role_policy, RoleName=config["AGENT_ROLE"],
                PolicyArn=config["DENY_POLICY_ARN"])
        attempt(errors, "disable queue", batch.update_job_queue, jobQueue=config["QUEUE_ARN"], state="DISABLED")
        attempt(errors, "disable compute environment", batch.update_compute_environment,
                computeEnvironment=config["CE_ARN"], state="DISABLED")

    watch = attempt(errors, "watch state", read_json, s3, config["BUCKET"], "guard/watch.json") or {}
    jobs = attempt(errors, "list jobs", queue_jobs, batch, config["QUEUE_ARN"], not active)
    if jobs is not None and active:
        result["stopped"], stop_errors = stop_all(batch, jobs, "project budget cutoff", remaining_ms)
        errors += stop_errors
    elif jobs is not None:
        stuck, stalled, watch = watchdog(jobs, watch, now)
        if stuck:
            done, stop_errors = stop_jobs(batch, stuck, "guard watchdog: STARTING longer than 30 minutes", remaining_ms)
            errors += stop_errors
            result["watchdog"] = {"terminated": done["terminated"]}
            notices.append("watchdog stopped jobs: " + json.dumps(result["watchdog"], sort_keys=True))
        if stalled:
            result["waiting_alert"] = len(stalled)
            notices.append("%d waiting jobs have no active progress observed for 180 minutes; "
                           "jobs remain queued; inspect capacity and placement" % len(stalled))
    # Preserve partial stop results in the Lambda log before optional reads/writes.
    # An in-flight SDK call can still exhaust the timeout; this is a best-effort
    # hand-back margin, and the durable latch is reconciled on the next invocation.
    if remaining_ms is not None and remaining_ms() < 20000:
        errors.append("reconciliation incomplete before Lambda deadline; retry")
        result.update(cutoff_active=active, errors=errors)
        print(json.dumps(result, sort_keys=True, default=str))
        raise RuntimeError("; ".join(errors))
    try:
        found = capacity(batch, ecs, config["CE_ARN"])
        result["capacity"] = found
        busy = (not active) and bool(jobs is None or leaves(jobs))
        problem, watch = check_capacity(found, busy, watch, now)
        if problem:
            errors.append(problem)
    except Exception as error:
        errors.append("capacity: " + failure(error))
    tag = "unchecked"
    if clients.get("ce") is not None:  # informational: never blocks the guard
        tag, watch = cost_tag(clients["ce"], watch, now)
    attempt(errors, "watch snapshot", write_json, s3, config["BUCKET"], "guard/watch.json", watch)
    attempt(errors, "spend snapshot", write_json, s3, config["BUCKET"], "guard/spend.json", dict(
        spend or {}, schema=1, read_at=stamp(now), cutoff_active=active, budget=config["BUDGET_NAME"],
        basis="AWS Budgets actual spend of costs tagged Project=%s; gross of credits and refunds; "
              "untagged charges excluded; delayed by hours" % project,
        capacity=result.get("capacity"), cost_tag=tag))
    if notices:
        attempt(errors, "notify", sns.publish, TopicArn=config["HEALTH_TOPIC_ARN"],
                Subject=("%s guard" % project)[:99], Message="\n".join(notices))
    result["cutoff_active"] = active
    print(json.dumps(result, sort_keys=True, default=str))
    if errors:
        raise RuntimeError("; ".join(errors))  # the guard-errors alarm emails the administrator
    return result


def cancel_run(event, config, clients, remaining_ms=None):
    """Agent-invoked: stop project jobs or read capacity without modifying jobs.

    Batch authorizes CancelJob/TerminateJob per job ARN and cannot tell creation-time tags
    from later retagging, so agents get this handler instead of those actions: it only sees
    jobs it lists from the project queue.
    """
    if not isinstance(event, dict) or len(event) != 1:
        return {"error": 'expected {"submission": ID}, {"all": true} or {"status": true}'}
    mode, value = next(iter(event.items()))
    if mode == "submission":
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}", value):
            return {"error": "invalid submission id"}
    elif mode not in ("all", "status") or value is not True:
        return {"error": 'expected {"all": true} or {"status": true}'}
    batch, ecs = clients["batch"], clients["ecs"]
    jobs = [] if mode == "status" else [job for job in queue_jobs(batch, config["QUEUE_ARN"], children=False)
                                       if mode == "all" or job["jobName"] == value + "-run"]
    done, errors = stop_all(batch, jobs, "cancelled by cloudsweep", remaining_ms)
    try:
        if remaining_ms is not None and remaining_ms() < 20000:
            raise RuntimeError("capacity unread before Lambda deadline; poll status")
        found = capacity(batch, ecs, config["CE_ARN"])
    except Exception as error:
        found, errors = None, errors + ["capacity: " + failure(error)]
    result = {"stopped": done, "errors": errors, "capacity": found}
    if "status" not in event:
        print(json.dumps(result, sort_keys=True, default=str))
    return result


def register_run(event, config, batch):
    """The caller chooses a project image digest; all container privileges come from the stack."""
    if (set(event) != {"image_digest"} or not isinstance(event["image_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", event["image_digest"])):
        return {"error": "expected only a sha256 image_digest"}
    template, = batch.describe_job_definitions(jobDefinitions=[config["RUN_TEMPLATE_ARN"]])["jobDefinitions"]
    image = config["REPOSITORY_URI"] + "@" + event["image_digest"]
    for page in batch.get_paginator("describe_job_definitions").paginate(
            jobDefinitionName=template["jobDefinitionName"], status="ACTIVE"):
        for prior in page["jobDefinitions"]:
            if (prior["revision"] > template["revision"]
                    and prior["containerProperties"]["image"] == image):
                return {"jobDefinitionArn": prior["jobDefinitionArn"]}
    definition = copy.deepcopy({key: template[key] for key in (
        "jobDefinitionName", "type", "parameters", "retryStrategy", "timeout", "containerProperties",
        "platformCapabilities", "propagateTags") if key in template})
    container = definition["containerProperties"]
    container["image"] = image
    for item in container["environment"]:
        if item["name"] == "IMAGE_DIGEST":
            item["value"] = event["image_digest"]
    return batch.register_job_definition(**definition)


def clients_for(names, region):
    # boto3 is provided by the Lambda runtime; importing here keeps offline tests
    # independent of an SDK installation and prevents accidental account calls.
    import boto3
    from botocore.config import Config
    sdk = Config(retries={"mode": "standard", "total_max_attempts": 5}, connect_timeout=3, read_timeout=10)
    return {name: boto3.client(name, region_name=("us-east-1" if name in ("budgets", "ce") else region), config=sdk)
            for name in names}


def handler(event, context):
    config = os.environ
    if not accepted_event(event, config):
        return {"ignored": "unexpected event source"}
    return run(event, config, clients_for(("ssm", "iam", "budgets", "batch", "ecs", "s3", "sns", "ce"),
                                          config["REGION"]), remaining_ms=context.get_remaining_time_in_millis)


def control_handler(event, context):
    config = os.environ
    if isinstance(event, dict) and "image_digest" in event:
        return register_run(event, config, clients_for(("batch",), config["REGION"])["batch"])
    return cancel_run(event, config, clients_for(("batch", "ecs"), config["REGION"]),
                      remaining_ms=context.get_remaining_time_in_millis)
