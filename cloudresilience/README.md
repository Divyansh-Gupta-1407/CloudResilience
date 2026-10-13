# CloudResilience: the AWS layer for ResilienceAI

CloudResilience extends **ResilienceAI**, the chaos-engineering platform built by the Bosch hackathon team
(see the [root README](../README.md)), with an AWS observability and incident-reporting layer:

- Every 3-second telemetry window of a chaos experiment is published to **Amazon CloudWatch** as custom
  metrics.
- **CloudWatch alarms** watch those metrics and notify an **SNS** topic.
- Every squad run produces an incident report. It is stored, encrypted and versioned, in **Amazon S3**.
- The **EC2 inventory** and instance metrics appear in the control center.
- All of these AWS resources are defined in one **CloudFormation** template, including a least-privilege
  **IAM** policy.

Fault injection is unchanged. It still runs only in the local `kind` sandbox, under the original safety
controller. Nothing in this package injects faults into cloud resources.

## Attribution

| Part | Author |
|---|---|
| `chaos/`, `frontend/` (except the Cloud page), `dashboard/`, `patches/`, the patched Online Boutique, the LangGraph agents, the safety controller, telemetry, severity ranking, fixes and rollback | Original hackathon team (see `git log upstream/main`) |
| `cloudresilience/` (this folder), `frontend/src/pages/Cloud.tsx`, the *Upload to S3* button on Findings, the Cloud items in the sidebar, the `ROLLBACK_HOOKS` / `/cloud` mount at the end of `chaos/api.py` | The AWS extension on branch `feature/aws-cloudresilience` |

The original repository has no license file. Before you publish this fork or reuse the code elsewhere,
agree on terms with the original contributors.

## Architecture

```
 local (laptop)                                                    AWS account
 ┌──────────────────────────────────────────────┐
 │ kind sandbox: Online Boutique (11 services)  │
 │      ▲ faults (safety controller, unchanged) │
 │      │                                       │
 │ ResilienceAI backend (FastAPI, chaos/)       │   PutMetricData     ┌───────────────────────────┐
 │   telemetry windows ─────────────────────────┼───────────────────▶ │ CloudWatch metrics        │
 │   CloudResilience (cloudresilience/)         │                     │ CloudResilience/Boutique  │
 │     TelemetryPublisher (background thread)   │                     │   ├─ dashboard            │
 │     /cloud routes ◀── React "Cloud" page     │   GetMetricData     │   └─ 3 alarms ───┐        │
 │       ├─ inventory, metrics, alarms ◀────────┼──────────────────── │                  ▼        │
 │       └─ reports ────────────────────────────┼── PutObject ──────▶ │ S3 report bucket   SNS ──▶ email / SMS / Lambda
 │   rollback hook ─▶ auto report + notify ─────┼── Publish ────────▶ │ (SSE, versioned,   topic  │
 └──────────────────────────────────────────────┘                     │  TLS-only, private)       │
                                                                      └───────────────────────────┘
```

| Module | What it does | AWS APIs |
|---|---|---|
| `aws/cloudwatch_monitor.py` | Converts telemetry windows to metrics (error rate, p50/p95, per-journey error rates, faults held, stalled). Publishes them in batches from a background thread. Reads EC2 and chaos metrics back. Lists alarm states | `PutMetricData`, `GetMetricData`, `DescribeAlarms` |
| `aws/s3_report_store.py` | Uploads `report.json` and `report.md` per run to `reports/YYYY/MM/DD/<squad_id>/`. Lists reports and creates presigned download links. Rejects keys outside `reports/` | `PutObject`, `GetObject`, `ListObjectsV2` |
| `aws/sns_notifier.py` | Publishes the incident summary with filterable message attributes (`worst_severity`, `resilience_score`, `environment`) | `Publish` |
| `aws/ec2_inventory.py` | Lists instances (paginated, with an optional tag filter) and counts them by state, type and AZ | `DescribeInstances` |
| `reporting.py` | Builds the report from a squad run. It uses the same score formula as `/findings` | none |
| `service.py`, `api/cloud_routes.py` | The `/cloud` API. The rollback hook can upload a report automatically | none |
| `infra/cloudformation.yaml` | Bucket, bucket policy, SNS topic, alarms, CloudWatch dashboard, IAM managed policy | none |
| `policies/cloudresilience-backend-policy.json` | The same IAM policy for manual setup (a test keeps it identical to the template) | none |

## Deploy the AWS resources

You need the AWS CLI, signed in to an account where you can create IAM policies.

```powershell
aws cloudformation deploy `
  --stack-name cloudresilience-sandbox `
  --template-file cloudresilience/infra/cloudformation.yaml `
  --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides Environment=sandbox NotificationEmail=you@example.com

aws cloudformation describe-stacks --stack-name cloudresilience-sandbox --query "Stacks[0].Outputs"
```

Next:

1. Confirm the subscription email that SNS sends you.
2. Attach the `BackendPolicyArn` output to the identity the backend runs as. That can be your SSO
   permission set, an IAM user for local use, or a task or instance role.

Cost: the metrics are 9 custom metrics with one dimension set, plus 3 alarms, 1 dashboard and S3/SNS
usage at report scale. This costs a few US dollars a month, mostly for the custom metrics. Stop the
publisher when you are not experimenting.

## Run the backend with AWS enabled

```powershell
$env:AWS_PROFILE        = "your-profile"           # or any other boto3 credential source
$env:CR_AWS_REGION      = "eu-central-1"
$env:CR_REPORT_BUCKET   = "<ReportBucketName output>"
$env:CR_SNS_TOPIC_ARN   = "<IncidentTopicArn output>"
$env:CR_PUBLISH_METRICS = "1"     # stream telemetry to CloudWatch from startup
$env:CR_AUTO_REPORT     = "1"     # upload a report and notify after every squad rollback
.\start-dashboard.ps1
```

| Variable | Default | Meaning |
|---|---|---|
| `CR_AWS_REGION` | `AWS_REGION` | Region for all clients |
| `CR_REPORT_BUCKET` | unset (reports off) | Bucket for incident reports |
| `CR_REPORT_PREFIX` | `reports/` | Key prefix (the IAM policy is scoped to `reports/*`) |
| `CR_SNS_TOPIC_ARN` | unset (notifications off) | Topic for incident notifications |
| `CR_METRIC_NAMESPACE` | `CloudResilience/Boutique` | Must match the template parameter and the policy condition |
| `CR_ENVIRONMENT` | `sandbox` | `Environment` metric dimension |
| `CR_PUBLISH_METRICS` | off | Start the CloudWatch publisher with the backend |
| `CR_AUTO_REPORT` | off | Report and notify automatically after every squad rollback |
| `CR_EC2_TAG_FILTER` | unset | `Key=Value` filter for the inventory |

No credentials are read or stored by this code. boto3's default chain is used, so the code runs
unchanged on a laptop with SSO and on ECS or EKS with a task role.

Every AWS feature is optional. If AWS is not configured, the original platform works as before. The
Cloud page then shows what is missing, and an AWS failure never affects an experiment, the
telemetry or a rollback.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/cloud/status` | Identity (`sts:GetCallerIdentity`), config, publisher state, recent uploads |
| GET | `/cloud/inventory` | EC2 instances and counts |
| GET | `/cloud/ec2/{instance_id}/metrics?minutes=60` | CPU, network, status checks |
| GET | `/cloud/telemetry?minutes=30` | Chaos telemetry read back from CloudWatch |
| GET | `/cloud/alarms` | Alarm states |
| POST | `/cloud/telemetry/publish/start` and `/stop` | Control the publisher |
| POST | `/cloud/reports` `{"squad_id": null, "notify": true}` | Upload the latest (or a given) run's report and notify |
| GET | `/cloud/reports`, `/cloud/reports/url?key=...` | List reports, presigned download link |

There is also a command line for each module:
`python -m cloudresilience.aws.ec2_inventory`,
`python -m cloudresilience.aws.cloudwatch_monitor ec2|telemetry|alarms`,
`python -m cloudresilience.aws.s3_report_store list|upload|url`.

## Try it without an AWS account

boto3 honours `AWS_ENDPOINT_URL`, so the whole layer runs against a local [moto](https://github.com/getmoto/moto)
server:

```powershell
python -m moto.server -p 5055          # in its own window
$env:AWS_ENDPOINT_URL = "http://127.0.0.1:5055"; $env:AWS_ACCESS_KEY_ID = "testing"; $env:AWS_SECRET_ACCESS_KEY = "testing"
$env:CR_AWS_REGION = "us-east-1"; $env:CR_REPORT_BUCKET = "cr-demo-reports"
python -c "import boto3; boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='cr-demo-reports')"
python -m uvicorn chaos.api:app --port 8000
```

## Tests

```powershell
pip install -r cloudresilience/requirements.txt
python -m pytest cloudresilience/tests -q
```

There are 35 tests. They run against moto, with fake credentials forced, so they cannot reach a real
account. They cover:

- the metric conversion and batching;
- publishing and reading the metrics back through `GetMetricData`;
- publisher failure handling;
- EC2 inventory and tag filters;
- S3 upload, listing, presigned links, versioning and key-escape rejection;
- SNS delivery, checked through an SQS subscriber, including the message attributes;
- the `/cloud` API end to end and the automatic report on rollback;
- an IAM policy check: no wildcard actions, writes scoped, and the JSON policy identical to the
  template;
- a `cfn-lint` check of the template.

The report fixture is a real analysed run from the project's database.

Not yet verified against a real AWS account: `cloudformation deploy` itself, and the IAM policy under
real evaluation. Run both once before relying on them.

## Roadmap

1. **Run the target on EKS.** Deploy Online Boutique to a small EKS cluster. Allow it as a second
   sandbox context in `safety.yaml`, scoped to a dedicated namespace.
2. **Run the backend in AWS.** Use ECS Fargate or the same EKS cluster, with a task role that has the
   policy above. Store the SQLite state in RDS or DynamoDB.
3. **Cloud-level faults with AWS FIS.** Use AWS Fault Injection Service templates (for example, stop
   instances by tag or throttle an AZ), with a CloudWatch alarm as the FIS stop condition. They would
   pass through the same allowlist and validation as today's faults.
