# VIP S3 bucket auto-provisioning (onboarding)

## Purpose

When a client completes onboarding via `POST /onboarding`, the onboarding Lambda creates that client’s VIP destination bucket in S3 (if possible) using the **same naming convention** as the data ingestion pipeline and Terraform:

`mmm-{environment}-data-{client_id}`

Example: `mmm-qa-data-acmecorp`.

Data ingestion already resolves the destination with `get_vip_bucket_name(client_id)` in the data-transfer Lambda; **no change** is required there once the bucket exists and IAM allows writes.

Buckets created this way are **not** managed by Terraform. Existing Terraform `for_each` VIP buckets can remain for historical clients; new clients rely on onboarding provisioning (or manual / Terraform if you add `client_id` and apply).

## Flow

1. Request is validated and written to DynamoDB (`client_metadata`, `pipeline_infos`) as before.
2. `client_id` is derived with `normalize_client_id(request.account.name)`.
3. `ensure_vip_bucket()` runs (see `lambda/vip_bucket_provisioner.py`):
   - Creates the bucket (idempotent if you already own it).
   - Applies public access block, versioning, encryption (SSE-S3 or SSE-KMS), ownership `BucketOwnerEnforced`, lifecycle rule (abort incomplete multipart uploads after 7 days), tags, and bucket policy (TLS-only deny + allow `PutObject` / `PutObjectAcl` for the data ingestion Lambda role).
   - Optionally enables access logging to `mmm-{environment}-data-logs` **only if** that bucket already exists (typically created by data ingestion Terraform).

## API response fields

On HTTP 200 after a successful DynamoDB write:

| Field | Meaning |
| --- | --- |
| `vip_bucket_created` | `true` if provisioning finished without error; `false` if skipped or failed. |
| `vip_bucket_error` | Present when `vip_bucket_created` is `false`; human-readable reason. |

Provisioning failures **do not** change the HTTP status from 200 when DynamoDB succeeded, so onboarding data is not rolled back. Operators should fix S3/IAM and re-run or create the bucket manually.

## Terraform (onboarding stack)

- **`iam.tf`**: Inline policy `lambda_s3_vip_provision` grants the onboarding Lambda S3 control-plane actions on `arn:aws:s3:::mmm-{env_prefix}-data-*` and `/*`.
- **`variables.tf`**: `data_ingestion_lambda_role_name` (default `{env_prefix}-data-ingestion-pipeline-lambda-role`), `vip_encryption_type`, `vip_kms_key_arn`, `vip_enable_versioning`, `vip_enable_logging`.
- **`lambda.tf`**: Passes `DATA_TRANSFER_ROLE_ARN` (from `data.aws_iam_role.data_ingestion_lambda`) and VIP settings into the Lambda environment.

**Prerequisite:** The data ingestion Lambda IAM role must exist in the account so Terraform can resolve `data.aws_iam_role.data_ingestion_lambda`. If the role name differs, set `data_ingestion_lambda_role_name`.

## Environment variables (Lambda)

| Variable | Description |
| --- | --- |
| `ENVIRONMENT` | Environment segment in the bucket name (e.g. `qa`). |
| `AWS_REGION` | Region for bucket creation (set by Lambda runtime). |
| `DATA_TRANSFER_ROLE_ARN` | Principal allowed `PutObject` / `PutObjectAcl` in the bucket policy. |
| `VIP_ENCRYPTION_TYPE` | `SSE-S3` (default) or `SSE-KMS`. |
| `VIP_KMS_KEY_ARN` | Required when `VIP_ENCRYPTION_TYPE=SSE-KMS`. |
| `VIP_ENABLE_VERSIONING` | `true` / `false`. |
| `VIP_ENABLE_LOGGING` | `true` / `false` (logging only applied if `mmm-{env}-data-logs` exists). |

## Local tests

```text
pip install -r onboarding_pipeline/tests/requirements-dev.txt
pytest onboarding_pipeline/tests/test_vip_bucket_provisioner.py -v
```

## Operational runbook (manual fallback)

If auto-provision fails:

1. Create `mmm-{env}-data-{client_id}` with the same settings as `data_ingestion_pipeline/terraform/vip_buckets.tf`, **or** add `client_id` to `var.client_ids` and Terraform apply.
2. Ensure the data-transfer Lambda role can write to the bucket (wildcard IAM or bucket policy consistent with Terraform).
