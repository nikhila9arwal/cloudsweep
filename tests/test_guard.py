"""Offline tests of the budget guard and cancellation handler, retargeted to Batch.
In-memory clients stand in for AWS; no SDK client is ever constructed."""

import copy
import json
import sys
import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "cloudsweep"))
import guard  # noqa: E402

PROJECT, REGION, ACCOUNT = "demo", "us-east-1", "123456789012"
ARN = "arn:aws:%s:" + REGION + ":" + ACCOUNT + ":%s"
CONFIG = {"PROJECT": PROJECT, "REGION": REGION, "ACCOUNT": ACCOUNT, "BUDGET_NAME": PROJECT + "-monthly",
          "CUTOFF_AMOUNT": "50", "STATE_PARAMETER": "/demo/cloud/cutoff",
          "DENY_POLICY_ARN": "arn:aws:iam::%s:policy/demo-submit-deny" % ACCOUNT,
          "AGENT_ROLE": "demo-agent", "TOPIC_ARN": ARN % ("sns", "demo-budget-cutoff"),
          "RULE_ARN": ARN % ("events", "rule/demo-guard-reconcile"),
          "QUEUE_ARN": ARN % ("batch", "job-queue/demo-cpu"),
          "CE_ARN": ARN % ("batch", "compute-environment/demo-cpu"), "BUCKET": "bucket",
          "HEALTH_TOPIC_ARN": ARN % ("sns", "demo-health")}
TICK = {"source": "aws.events", "detail-type": "Scheduled Event", "account": ACCOUNT, "region": REGION,
        "resources": [CONFIG["RULE_ARN"]], "detail": {}}
SNS = {"Records": [{"EventSource": "aws:sns", "Sns": {"Type": "Notification", "TopicArn": CONFIG["TOPIC_ARN"],
                                                      "Message": "AWS budget notification"}}]}
START = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
SUB = "20261029T000000Z-abcdef"


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class NoSuchKey(Exception):
    pass


class Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return iter(self.pages(**kwargs))


def job(identifier, status, name=SUB + "-run", size=None, parent=None, index=None):
    summary = {"jobId": identifier, "jobName": name, "status": status}
    if size:
        summary["arrayProperties"] = {"size": size}
    if parent:
        summary["arrayProperties"] = {"index": index}
        summary["parent"] = parent
    return summary


class World:
    """Fake clients exposing only the API methods the guard and handler use."""

    def __init__(self, amount="10"):
        self.state = guard.initial_state(PROJECT, REGION)
        self.attached = []
        self.jobs = []
        self.queue_state = self.ce_state = "ENABLED"
        self.desired = 0
        self.instances = 0
        self.objects = {}
        self.events = []
        self.failures = {}
        self.budget = {"BudgetName": CONFIG["BUDGET_NAME"], "BudgetType": "COST", "TimeUnit": "MONTHLY",
                       "CostFilters": {"TagKeyValue": ["user:Project$demo"]},
                       "CostTypes": {"IncludeCredit": False, "IncludeRefund": False, "UseBlended": False},
                       "BudgetLimit": {"Amount": "50.0", "Unit": "USD"},
                       "CalculatedSpend": {"ActualSpend": {"Amount": amount, "Unit": "USD"},
                                           "ForecastedSpend": {"Amount": "80", "Unit": "USD"}}}

    @property
    def clients(self):
        return {name: self for name in ("ssm", "iam", "budgets", "batch", "ecs", "s3", "sns")}

    def record(self, name, target=None):
        self.events.append((name, target))
        if self.failures.get((name, target)):
            self.failures[(name, target)] -= 1
            raise AwsError("ServiceUnavailable")

    # SSM
    def get_parameter(self, Name):
        assert Name == CONFIG["STATE_PARAMETER"]
        self.record("get_state")
        if self.state is None:
            raise AwsError("ParameterNotFound")
        return {"Parameter": {"Value": json.dumps(self.state)}}

    def put_parameter(self, Name, Type, Value, Overwrite):
        assert (Name, Type, Overwrite) == (CONFIG["STATE_PARAMETER"], "String", True)
        self.record("write_state")
        self.state = json.loads(Value)

    # IAM
    def attach_role_policy(self, RoleName, PolicyArn):
        assert (RoleName, PolicyArn) == (CONFIG["AGENT_ROLE"], CONFIG["DENY_POLICY_ARN"])
        self.record("attach")
        if PolicyArn not in self.attached:
            self.attached.append(PolicyArn)

    # Budgets
    def describe_budget(self, AccountId, BudgetName):
        assert (AccountId, BudgetName) == (ACCOUNT, CONFIG["BUDGET_NAME"])
        self.record("describe_budget")
        return {"Budget": self.budget}

    # Batch
    def get_paginator(self, name):
        if name == "list_attached_role_policies":
            def policies(RoleName):
                self.record("list_policies")
                return [{"AttachedPolicies": []}, {"AttachedPolicies": [{"PolicyArn": p} for p in self.attached]}]
            return Paginator(policies)
        if name == "list_container_instances":
            return Paginator(lambda cluster: [{"containerInstanceArns": ["i"] * self.instances}])
        assert name == "list_jobs"

        def jobs(jobStatus, jobQueue=None, arrayJobId=None):
            self.record("list_jobs")
            if jobQueue is not None:
                assert jobQueue == CONFIG["QUEUE_ARN"], "listed another queue"
                found = [j for j in self.jobs if "parent" not in j and j["status"] == jobStatus]
            else:
                found = [j for j in self.jobs if j.get("parent") == arrayJobId and j["status"] == jobStatus]
            return [{"jobSummaryList": [{k: v for k, v in j.items() if k != "parent"} for j in found[::2]]},
                    {"jobSummaryList": [{k: v for k, v in j.items() if k != "parent"} for j in found[1::2]]}]
        return Paginator(jobs)

    def stop(self, kind, jobId, reason):
        self.record(kind, jobId)
        for item in self.jobs:
            if item["jobId"] == jobId or item.get("parent") == jobId:
                if item["status"] not in guard.WAITING + guard.ACTIVE:
                    continue
                if kind == "cancel" and item["status"] not in guard.WAITING:
                    continue  # AWS returns success without cancelling a job that has started.
                item["status"] = "FAILED"

    def cancel_job(self, jobId, reason):
        self.stop("cancel", jobId, reason)

    def terminate_job(self, jobId, reason):
        self.stop("terminate", jobId, reason)

    def update_job_queue(self, jobQueue, state):
        assert jobQueue == CONFIG["QUEUE_ARN"]
        self.record("disable_queue")
        self.queue_state = state

    def update_compute_environment(self, computeEnvironment, state):
        assert computeEnvironment == CONFIG["CE_ARN"]
        self.record("disable_ce")
        self.ce_state = state

    def describe_compute_environments(self, computeEnvironments):
        assert computeEnvironments == [CONFIG["CE_ARN"]]
        return {"computeEnvironments": [{"state": self.ce_state, "status": "VALID", "ecsClusterArn": "cluster",
                                         "computeResources": {"desiredvCpus": self.desired, "maxvCpus": 128}}]}

    # S3 and SNS
    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)

        class Body:
            def __init__(self, data):
                self.data = data

            def read(self):
                return self.data
        return {"Body": Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.record("put_object", Key)
        self.objects[Key] = Body

    def publish(self, TopicArn, Subject, Message):
        assert TopicArn == CONFIG["HEALTH_TOPIC_ARN"]
        self.record("notify", Message)

    def run(self, event=TICK, now=START):
        return guard.run(copy.deepcopy(event), CONFIG, self.clients, now)

    def stops(self):
        return {(name, target) for name, target in self.events if name in ("cancel", "terminate")}


def sweep(world):
    world.jobs = [job("waiting", "RUNNABLE"), job("starting", "STARTING"), job("running", "RUNNING"),
                  job("array", "PENDING", size=3),
                  job("array:0", "RUNNING", parent="array", index=0),
                  job("array:1", "RUNNABLE", parent="array", index=1),
                  job("array:2", "SUCCEEDED", parent="array", index=2),
                  job("done", "SUCCEEDED")]


class GuardTests(unittest.TestCase):
    def test_below_cutoff_changes_nothing_and_publishes_a_snapshot(self):
        world = World("49.99")
        sweep(world)
        self.assertFalse(world.run(SNS)["cutoff_active"])
        self.assertFalse(world.state["cutoff_active"])
        self.assertEqual(world.stops(), set())
        self.assertEqual((world.queue_state, world.ce_state, world.attached), ("ENABLED", "ENABLED", []))
        snapshot = json.loads(world.objects["guard/spend.json"])
        self.assertEqual((snapshot["actual_usd"], snapshot["cutoff_usd"], snapshot["cutoff_active"]),
                         ("49.99", "50.0", False))
        self.assertIn("delayed", snapshot["basis"])

    def test_cutoff_latches_denies_disables_and_stops_every_job(self):
        world = World("50")
        sweep(world)
        result = world.run()
        self.assertTrue(world.state["cutoff_active"])
        self.assertEqual(world.state["reason"], "actual-budget-cutoff")
        self.assertEqual(world.attached, [CONFIG["DENY_POLICY_ARN"]])
        self.assertEqual((world.queue_state, world.ce_state), ("DISABLED", "DISABLED"))
        self.assertEqual(world.stops(), {("terminate", "waiting"), ("terminate", "starting"),
                                         ("terminate", "running"), ("terminate", "array")})
        # A stopped array parent stops its children; they are not listed one by one.
        self.assertEqual(sorted(result["stopped"]["terminated"]), ["running", "starting"])
        self.assertIn("array", result["stopped"]["cancelled"])
        self.assertEqual(next(j for j in world.jobs if j["jobId"] == "array:2")["status"], "SUCCEEDED")
        notices = [message for name, message in world.events if name == "notify"]
        self.assertEqual(len(notices), 1)
        self.assertIn("cutoff latched (actual-budget-cutoff)", notices[0])
        world.run()  # a later tick reconciles without a second notice
        self.assertEqual(sum(name == "notify" for name, _ in world.events), 1)

    def test_exact_sources_only_with_no_api_calls_for_others(self):
        variants = [None, {}, {"source": "aws.events"}, {"Records": []}, {"submission": SUB}]
        for field, value in (("account", "999999999999"), ("region", "us-west-2"), ("detail", {"force": 1}),
                             ("resources", [CONFIG["RULE_ARN"] + "-x"])):
            variants.append(dict(TICK, **{field: value}))
        other = copy.deepcopy(SNS)
        other["Records"][0]["Sns"]["TopicArn"] += "-alerts"
        variants.append(other)
        for event in variants:
            world = World("100")
            with self.subTest(event=event):
                self.assertIn("ignored", world.run(event))
                self.assertEqual(world.events, [])

    def test_budget_scope_must_match_and_bad_data_cannot_trigger(self):
        for field, value in (("BudgetName", "other"), ("TimeUnit", "DAILY"),
                             ("CostFilters", {"TagKeyValue": ["user:Project$other"]}),
                             ("BudgetLimit", {"Amount": "55", "Unit": "USD"})):
            world = World("100")
            world.budget[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                world.run()
            self.assertFalse(world.state["cutoff_active"])

    def test_missing_forecast_is_unknown_and_cannot_block_actual_cutoff(self):
        for forecast in (None, {"Amount": "NaN", "Unit": "USD"}, {"Amount": "2", "Unit": "EUR"}):
            world = World("50")
            del world.budget["CalculatedSpend"]["ForecastedSpend"]
            if forecast is not None:
                world.budget["CalculatedSpend"]["ForecastedSpend"] = forecast
            with self.subTest(forecast=forecast):
                self.assertTrue(world.run()["cutoff_active"])
                self.assertNotIn("forecast_usd", json.loads(world.objects["guard/spend.json"]))

    def test_cost_basis_drift_is_rejected(self):
        for field in ("IncludeCredit", "IncludeRefund", "UseBlended", "UseAmortized"):
            world = World("0")
            world.budget["CostTypes"][field] = True
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "excluding credits"):
                world.run()

    def test_missing_actual_spend_is_not_invented_as_zero(self):
        world = World()
        del world.budget["CalculatedSpend"]
        with self.assertRaises(KeyError):
            world.run()
        self.assertNotIn("guard/spend.json", world.objects)
        # A previous cutoff is still enforced if billing is temporarily unavailable.
        world.state["cutoff_active"] = True
        world.jobs = [job("running", "RUNNING")]
        with self.assertRaises(RuntimeError):
            world.run()
        self.assertIn(("terminate", "running"), world.stops())

    def test_deny_and_disable_are_independent(self):
        world = World("60")
        sweep(world)
        world.failures[("attach", None)] = 1
        with self.assertRaises(RuntimeError):
            world.run()
        self.assertEqual((world.queue_state, world.ce_state), ("DISABLED", "DISABLED"))
        self.assertIn(("terminate", "running"), world.stops())
        world = World("60")
        world.failures[("disable_queue", None)] = 1
        with self.assertRaises(RuntimeError):
            world.run()
        self.assertEqual(world.attached, [CONFIG["DENY_POLICY_ARN"]])
        self.assertEqual(world.ce_state, "DISABLED")
        world.run()  # the next tick repairs the failed step
        self.assertEqual(world.queue_state, "DISABLED")

    def test_snapshot_failure_does_not_suppress_spend_snapshot_or_cutoff_notice(self):
        world = World("50")
        world.jobs = [job("running", "RUNNING")]
        world.failures[("put_object", "guard/watch.json")] = 1
        with self.assertRaisesRegex(RuntimeError, "watch snapshot"):
            world.run()
        self.assertTrue(world.state["cutoff_active"])
        self.assertEqual(world.stops(), {("terminate", "running")})
        self.assertTrue(json.loads(world.objects["guard/spend.json"])["cutoff_active"])
        self.assertTrue(any(name == "notify" for name, _ in world.events))

    def test_failed_child_listing_after_a_failed_parent_stop_still_notifies(self):
        world = World("50")
        sweep(world)
        world.failures[("terminate", "array")] = 1
        original = world.get_paginator

        def paginator(name):
            found = original(name)
            if name != "list_jobs":
                return found

            def pages(**kwargs):
                if kwargs.get("arrayJobId"):
                    raise AwsError("ThrottlingException")
                return found.pages(**kwargs)
            return Paginator(pages)
        world.get_paginator = paginator
        with self.assertRaises(RuntimeError):
            world.run()
        self.assertTrue(world.state["cutoff_active"])
        self.assertEqual(sum(name == "notify" for name, _ in world.events), 1)
        self.assertIn("guard/spend.json", world.objects)

    def test_duplicates_and_month_rollover_never_reset_the_latch(self):
        world = World("50")
        world.run(SNS)
        saved = copy.deepcopy(world.state)
        world.budget["CalculatedSpend"]["ActualSpend"]["Amount"] = "0"
        world.failures[("describe_budget", None)] = 10
        with self.assertRaises(RuntimeError):  # the budget is unreadable; the latch still holds
            world.run(SNS)
        world.failures.clear()
        world.run()
        world.queue_state = "ENABLED"  # someone re-enables the queue by hand
        world.run()
        self.assertEqual(world.state, saved)
        self.assertEqual(world.queue_state, "DISABLED")

    def test_failed_state_write_recovers_from_the_attached_deny(self):
        world = World("50")
        world.failures[("write_state", None)] = 1
        with self.assertRaises(RuntimeError):
            world.run()
        self.assertFalse(world.state["cutoff_active"])
        world.budget["CalculatedSpend"]["ActualSpend"]["Amount"] = "0"
        world.run()
        self.assertEqual((world.state["cutoff_active"], world.state["reason"]), (True, "recover-attached-deny"))

    def test_cutoff_deadline_keeps_latch_and_next_tick_finishes_remaining_stops(self):
        world = World("50")
        world.jobs = [job("a", "RUNNING"), job("b", "RUNNING")]
        remaining = iter((30000, 10000, 9000))
        with self.assertRaisesRegex(RuntimeError, "reconciliation incomplete"):
            guard.run(TICK, CONFIG, world.clients, START, remaining_ms=lambda: next(remaining))
        self.assertTrue(world.state["cutoff_active"])
        self.assertEqual(world.stops(), {("terminate", "a")})
        world.run()
        self.assertEqual(world.stops(), {("terminate", "a"), ("terminate", "b")})

    def test_corrupt_or_mismatched_state_latches_and_missing_state_retries(self):
        for change in ({"project": "other"}, {"schema_version": 2}, {"cutoff_active": "false"}):
            world = World("0")
            world.state.update(change)
            with self.subTest(change=change):
                self.assertTrue(world.run()["cutoff_active"])
                self.assertTrue(guard.valid_state(world.state, PROJECT, REGION))
        world = World("0")
        world.state = None
        with self.assertRaises(AwsError):
            world.run()
        self.assertEqual(world.events, [("get_state", None)])

    def test_capacity_must_return_to_zero_within_the_grace(self):
        world = World("50")
        world.desired, world.instances = 2, 1
        self.assertTrue(world.run(now=START)["cutoff_active"])
        with self.assertRaises(RuntimeError) as raised:
            world.run(now=START + timedelta(minutes=31))
        self.assertIn("capacity has not returned to zero", str(raised.exception))
        world.desired = world.instances = 0
        world.run(now=START + timedelta(minutes=36))
        idle = World("0")
        idle.desired = 2  # no jobs at all, capacity held: an alert, never a silent cost
        idle.run(now=START)
        with self.assertRaises(RuntimeError):
            idle.run(now=START + timedelta(minutes=40))

    def test_incomplete_compute_response_cannot_look_drained(self):
        world = World()
        valid = {"ecsClusterArn": "cluster", "computeResources": {"desiredvCpus": 0}}
        variants = [{"computeResources": {"desiredvCpus": 0}}, {"ecsClusterArn": "cluster"}]
        variants += [dict(valid, computeResources={"desiredvCpus": value})
                     for value in (-1, "0", None, False, 0.0)]
        for ce in variants:
            world.describe_compute_environments = lambda computeEnvironments: {"computeEnvironments": [ce]}
            with self.subTest(ce=ce), self.assertRaisesRegex(ValueError, "cannot confirm drain"):
                guard.capacity(world, world, CONFIG["CE_ARN"])

    def test_watchdog_stops_stuck_starting_but_only_alerts_for_waiting_jobs(self):
        world = World("0")
        world.jobs = [job("stuck", "STARTING"), job("waits", "RUNNABLE")]
        world.run(now=START)
        world.run(now=START + timedelta(minutes=20))
        self.assertEqual(world.stops(), set())
        world.run(now=START + timedelta(minutes=31))
        self.assertEqual(world.stops(), {("terminate", "stuck")})
        self.assertTrue(any(name == "notify" for name, _ in world.events))
        # The waiting job has seen no progress since the STARTING job stopped counting.
        world.run(now=START + timedelta(minutes=31 + 181))
        self.assertEqual(world.stops(), {("terminate", "stuck")})
        self.assertEqual(world.jobs[1]["status"], "RUNNABLE")
        notices = [message for name, message in world.events if name == "notify"]
        self.assertEqual(len(notices), 2)
        self.assertIn("jobs remain queued", notices[-1])

    def test_waiting_queue_alerts_repeat_only_after_another_inactivity_interval(self):
        world = World("0")
        world.jobs = [job(status.lower(), status) for status in guard.WAITING]
        for minutes in (0, 180):
            self.assertNotIn("waiting_alert", world.run(now=START + timedelta(minutes=minutes)))
        self.assertEqual(world.run(now=START + timedelta(minutes=181))["waiting_alert"], 3)
        for minutes in (186, 361):
            self.assertNotIn("waiting_alert", world.run(now=START + timedelta(minutes=minutes)))
        self.assertEqual(world.run(now=START + timedelta(minutes=362))["waiting_alert"], 3)
        self.assertEqual(sum(name == "notify" for name, _ in world.events), 2)
        self.assertEqual(world.stops(), set())
        self.assertEqual([item["status"] for item in world.jobs], list(guard.WAITING))
        # The alert-only policy does not weaken the cost cutoff.
        world.budget["CalculatedSpend"]["ActualSpend"]["Amount"] = "50"
        world.run(now=START + timedelta(minutes=363))
        self.assertEqual(world.stops(), {("terminate", status.lower()) for status in guard.WAITING})

    def test_healthy_active_work_resets_the_waiting_alert_timer(self):
        for status in guard.ACTIVE:
            world = World("0")
            world.jobs = [job("waits", "RUNNABLE")]
            world.run(now=START)
            world.jobs.append(job("progress", status))
            world.run(now=START + timedelta(minutes=175))
            world.jobs.pop()
            world.run(now=START + timedelta(minutes=200))
            self.assertFalse(any(name == "notify" for name, _ in world.events))
            world.run(now=START + timedelta(minutes=356))
            self.assertEqual(sum(name == "notify" for name, _ in world.events), 1)
            self.assertEqual(world.stops(), set())

    def test_a_busy_queue_is_not_a_stalled_queue(self):
        world = World("0")
        world.jobs = [job("running", "RUNNING"), job("waits", "RUNNABLE")]
        for minutes in (0, 100, 200, 400):
            world.run(now=START + timedelta(minutes=minutes))
        self.assertEqual(world.stops(), set())


class CancelHandlerTests(unittest.TestCase):
    def test_only_the_named_submission_in_the_project_queue(self):
        world = World()
        sweep(world)
        world.jobs.append(job("other", "RUNNING", name="20261029T000000Z-ffffff-run"))
        result = guard.cancel_run({"submission": SUB}, CONFIG, world.clients)
        self.assertNotIn(("terminate", "other"), world.stops())
        self.assertIn(("terminate", "array"), world.stops())
        self.assertIn("array", result["stopped"]["cancelled"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["capacity"]["desired_vcpus"], 0)
        everything = guard.cancel_run({"all": True}, CONFIG, world.clients)
        self.assertEqual(everything["stopped"]["terminated"], ["other"])

    def test_malformed_requests_touch_nothing(self):
        for event in (None, {}, {"all": "yes"}, {"submission": SUB, "all": True}, {"submission": "x"},
                      {"submission": "../../" + "a" * 17}, {"jobs": ["j"]}, {"status": False},
                      {"status": True, "all": True}):
            world = World()
            sweep(world)
            with self.subTest(event=event):
                self.assertIn("error", guard.cancel_run(event, CONFIG, world.clients))
                self.assertEqual(world.events, [])

    def test_status_only_reads_capacity(self):
        world = World()
        sweep(world)
        world.desired, world.instances = 0, 1
        result = guard.cancel_run({"status": True}, CONFIG, world.clients)
        self.assertEqual(result["capacity"]["instances"], 1)
        self.assertEqual(result["stopped"], {"cancelled": [], "terminated": []})
        self.assertEqual(world.events, [])  # no job enumeration or mutation

    def test_waiting_job_that_starts_during_cancellation_is_stopped(self):
        world = World()
        world.jobs = [job("racing", "RUNNABLE")]
        stale = guard.queue_jobs(world, CONFIG["QUEUE_ARN"])
        world.jobs[0]["status"] = "RUNNING"
        done, errors = guard.stop_jobs(world, stale, "cancel test")
        self.assertEqual(errors, [])
        self.assertEqual(world.jobs[0]["status"], "FAILED")
        self.assertEqual(done["cancelled"], ["racing"])  # classified by observed status
        self.assertEqual(world.stops(), {("terminate", "racing")})

    def test_failed_array_parent_does_not_suppress_child_stop(self):
        world = World()
        world.jobs = [job("array", "PENDING", size=2),
                      job("array:0", "RUNNING", parent="array", index=0),
                      job("array:1", "RUNNABLE", parent="array", index=1)]
        world.failures[("terminate", "array")] = 1
        result = guard.cancel_run({"all": True}, CONFIG, world.clients)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(world.stops(), {("terminate", "array"), ("terminate", "array:0"),
                                         ("terminate", "array:1")})
        self.assertEqual(result["stopped"], {"cancelled": ["array:1"], "terminated": ["array:0"]})

    def test_near_deadline_reports_unattempted_jobs_and_partial_results(self):
        world = World()
        world.jobs = [job("a", "RUNNING"), job("b", "RUNNING")]
        remaining = iter((30000, 10000, 9000))
        result = guard.cancel_run({"all": True}, CONFIG, world.clients,
                                  remaining_ms=lambda: next(remaining))
        self.assertEqual(world.stops(), {("terminate", "a")})
        self.assertEqual(result["stopped"]["terminated"], ["a"])
        self.assertTrue(any("1 jobs not processed" in error for error in result["errors"]))
        self.assertIsNone(result["capacity"])


class RegistrationHandlerTests(unittest.TestCase):
    def setUp(self):
        self.digest = "sha256:" + "a" * 64
        self.config = {"RUN_TEMPLATE_ARN": "arn:template:2", "REPOSITORY_URI": "registry/project"}
        self.template = {
            "jobDefinitionName": "project-run", "jobDefinitionArn": "arn:template:2", "revision": 2,
            "status": "ACTIVE", "tags": {"Project": "project"}, "containerOrchestrationType": "ECS",
            "type": "container", "parameters": {"submission": "unset"},
            "retryStrategy": {"attempts": 2}, "timeout": {"attemptDurationSeconds": 86400},
            "platformCapabilities": ["EC2"], "containerProperties": {
                "image": "registry/project:unpublished", "user": "1000", "privileged": False,
                "jobRoleArn": "project-job", "executionRoleArn": "project-execution",
                "environment": [{"name": "IMAGE_DIGEST", "value": "unpublished"},
                                {"name": "CLOUDSWEEP_STORE", "value": "s3://project"}]}}
        self.batch = Mock()
        self.batch.describe_job_definitions.return_value = {"jobDefinitions": [self.template]}
        self.batch.register_job_definition.return_value = {"jobDefinitionArn": "arn:new:3"}
        self.batch.get_paginator.return_value = Paginator(lambda **kwargs: [{"jobDefinitions": []}])

    def test_only_digest_changes_the_exact_stack_template_and_stale_revision_is_not_reused(self):
        original = copy.deepcopy(self.template)
        stale = dict(self.template, revision=1, containerProperties={"image": "registry/project@" + self.digest})
        self.batch.get_paginator.return_value = Paginator(lambda **kwargs: [{"jobDefinitions": [stale]}])
        result = guard.register_run({"image_digest": self.digest}, self.config, self.batch)
        self.assertEqual(result, {"jobDefinitionArn": "arn:new:3"})
        self.batch.describe_job_definitions.assert_called_once_with(jobDefinitions=["arn:template:2"])
        registered = self.batch.register_job_definition.call_args.kwargs
        self.assertEqual(set(registered) - set(self.template), set())
        for key in ("tags", "status", "revision", "jobDefinitionArn", "containerOrchestrationType"):
            self.assertNotIn(key, registered)
        for key in ("retryStrategy", "timeout", "platformCapabilities"):
            self.assertEqual(registered[key], original[key])
        expected = original["containerProperties"]
        expected["image"] = "registry/project@" + self.digest
        expected["environment"][0]["value"] = self.digest
        self.assertEqual(registered["containerProperties"], expected)
        self.assertEqual(self.template["containerProperties"]["image"], "registry/project:unpublished")
        self.assertEqual(self.template["containerProperties"]["environment"][0]["value"], "unpublished")

    def test_later_matching_revision_is_reused_across_pages(self):
        prior = dict(self.template, revision=3, jobDefinitionArn="arn:existing:3",
                     containerProperties={"image": "registry/project@" + self.digest})
        self.batch.get_paginator.return_value = Paginator(
            lambda **kwargs: [{"jobDefinitions": [self.template]}, {"jobDefinitions": [prior]}])
        self.assertEqual(guard.register_run({"image_digest": self.digest}, self.config, self.batch),
                         {"jobDefinitionArn": "arn:existing:3"})
        self.batch.register_job_definition.assert_not_called()

    def test_extra_fields_and_invalid_digests_cannot_register(self):
        forbidden = {"image": "elsewhere/image", "containerProperties": {"privileged": True},
                     "jobRoleArn": "other-role", "volumes": [{"host": {"sourcePath": "/"}}]}
        events = [{"image_digest": self.digest, key: value} for key, value in forbidden.items()]
        events += [{"image_digest": value} for value in (None, "x", "sha256:" + "g" * 64)]
        for event in events:
            with self.subTest(event=event):
                self.assertIn("error", guard.register_run(event, self.config, self.batch))
        self.assertEqual(self.batch.mock_calls, [])


if __name__ == "__main__":
    unittest.main()


class CostTagTests(unittest.TestCase):
    class Ce:
        def __init__(self, status=None, errors=None):
            self.status, self.errors, self.updates = status, errors or [], []

        def list_cost_allocation_tags(self, **kwargs):
            if self.status is None:
                return {"CostAllocationTags": []}
            return {"CostAllocationTags": [{"TagKey": "Project", "Type": "UserDefined", "Status": self.status}]}

        def update_cost_allocation_tags_status(self, **kwargs):
            self.updates.append(kwargs)
            return {"Errors": self.errors}

    def test_unseen_tag_is_reported_and_not_activated(self):
        ce = self.Ce(None)
        state, watch = guard.cost_tag(ce, {}, START)
        self.assertIn("not yet seen", state)
        self.assertEqual(ce.updates, [])
        self.assertIn("not yet seen", watch["tag_state"])

    def test_inactive_tag_is_activated_and_answers_are_kept_for_a_day(self):
        ce = self.Ce("Inactive")
        state, watch = guard.cost_tag(ce, {}, START)
        self.assertEqual(state, "activation requested")
        self.assertEqual(ce.updates[0]["CostAllocationTagsStatus"], [{"TagKey": "Project", "Status": "Active"}])
        ce.status = "Active"
        self.assertEqual(guard.cost_tag(ce, watch, START + timedelta(hours=1))[0], "activation requested")
        self.assertEqual(guard.cost_tag(ce, watch, START + timedelta(hours=25))[0], "active")

    def test_member_accounts_are_told_who_activates_the_tag(self):
        class Linked:
            def list_cost_allocation_tags(self, **kwargs):
                raise RuntimeError("AccessDeniedException: Linked account doesn't have access to cost allocation tags.")
        state, watch = guard.cost_tag(Linked(), {}, START)
        self.assertIn("management account", state)
        self.assertEqual(watch["tag_state"], state)

    def test_tag_failures_never_fail_the_guard(self):
        world = World()

        class Broken:
            def list_cost_allocation_tags(self, **kwargs):
                raise RuntimeError("no access")
        clients = dict(world.clients, ce=Broken())
        result = guard.run(TICK, CONFIG, clients, now=START)
        self.assertFalse(result["cutoff_active"])
        spend = json.loads(world.objects["guard/spend.json"])
        self.assertTrue(spend["cost_tag"].startswith("unknown"))
