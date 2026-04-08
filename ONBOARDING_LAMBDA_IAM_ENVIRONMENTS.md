# Onboarding Lambda: environments, Spacelift, and IAM for VIP S3 buckets

This document explains **who deploys what**, which **IAM permissions** the onboarding Lambda needs for **automatic VIP bucket creation**, and what to give the **platform / data-ingestion** team so QA and production (PRD) behave like dev.

For bucket behaviour and API fields, see [VIP_BUCKET_AUTO_PROVISION.md](./VIP_BUCKET_AUTO_PROVISION.md).

---

## 1. Scenario: dev vs QA / PRD

| Environment | Typical ownership | How infra is applied |
|-------------|-------------------|----------------------|
| **Dev** | Application / platform team | **Terraform** locally or CI (`terraform plan/apply` with `environments/dev.tfvars`) |
| **QA** | Platform / central infra | **Spacelift** (or equivalent) runs the **same** `onboarding_pipeline/terraform` stack against the QA account |
| **PRD (production)** | Platform / central infra | **Spacelift** runs the same stack with production variables and approvals |

The **code and IAM shape are the same** in all environments: only **AWS account**, **`env_prefix`**, **table names**, and **role names** change. Spacelift must apply the onboarding Terraform module (or equivalent HCL) so the Lambda role, policies, and environment variables match what is described below.

---

## 2. What the onboarding Lambda does to S3

On `POST /onboarding`, after DynamoDB succeeds, the handler calls `ensure_vip_bucket()` in [`lambda/vip_bucket_provisioner.py`](../lambda/vip_bucket_provisioner.py). It:

1. Creates `mmm-{environment}-data-{client_id}` (idempotent if the account already owns the bucket).
2. Sets public access block, versioning, default encryption (SSE-S3 or SSE-KMS), ownership `BucketOwnerEnforced`, lifecycle (abort incomplete multipart after 7 days), tags, and a **bucket policy** (TLS-only + optional `PutObject` / `PutObjectAcl` for the **data ingestion Lambda role**).
3. Optionally enables **access logging** to `mmm-{environment}-data-logs` **only if** that log bucket already exists (usually created by the data ingestion Terraform stack).

Buckets created this way are **not** tracked in Terraform state; they are tagged `ManagedBy=OnboardingLambda`.

---

## 3. IAM: onboarding Lambda execution role

The onboarding function runs as **`aws_iam_role.lambda_execution`** (see [`terraform/iam.tf`](../terraform/iam.tf)). For VIP provisioning, the important inline policy is **`lambda_s3_vip_provision`**.

### 3.1 S3 permissions (required for auto bucket creation)

Grant these actions on **both** the bucket ARN and `/*` object ARN pattern:

**Resource pattern (as in Terraform):**

- `arn:aws:s3:::mmm-{env_prefix}-data-*`
- `arn:aws:s3:::mmm-{env_prefix}-data-*/*`

**Actions:**

| Action | Why |
|--------|-----|
| `s3:CreateBucket` | Create `mmm-{env}-data-{client_id}` |
| `s3:HeadBucket` | Check existence (e.g. log target bucket) |
| `s3:GetBucketLocation` | Region-aware creation / checks |
| `s3:PutBucketVersioning` | Enable versioning |
| `s3:PutEncryptionConfiguration` | Default encryption (AES256 or KMS) |
| `s3:PutBucketPublicAccessBlock` | Block public access |
| `s3:PutBucketOwnershipControls` | `BucketOwnerEnforced` |
| `s3:PutLifecycleConfiguration` | Multipart cleanup rule |
| `s3:PutBucketTagging` | Standard VIP tags |
| `s3:PutBucketPolicy` | TLS deny + allow data-transfer role writes |
| `s3:PutBucketLogging` | Optional logging to `mmm-{env}-data-logs` |

**Spacelift / platform team:** reproduce this statement in the onboarding Lambda role in QA and PRD (same pattern, correct `env_prefix` for that account).

### 3.2 Other permissions already on the same role (onboarding stack)

The same execution role also needs (already defined in `iam.tf`):

- **CloudWatch Logs:** `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` on the function log group.
- **DynamoDB:** `PutItem`, `GetItem`, `UpdateItem`, `Query`, `BatchWriteItem` on `client_metadata` and `pipeline_infos` tables (and their GSIs).
- **Attached policy:** `AWSLambdaBasicExecutionRole`.

### 3.3 KMS (only if `VIP_ENCRYPTION_TYPE=SSE-KMS`)

Bucket default encryption with a **customer-managed CMK** requires the onboarding role to be allowed to use that key. The repo’s Terraform does **not** add separate `kms:*` statements; you must either:

- Use **SSE-S3** (`AES256`) in lower environments, or  
- Add IAM (and key policy) allowing the onboarding Lambda role at least **`kms:Decrypt`**, **`kms:GenerateDataKey`**, **`kms:DescribeKey`** (exact set per your org’s KMS policy) on `var.vip_kms_key_arn`.

---

## 4. IAM / trust: data ingestion Lambda (not the onboarding role)

The **bucket policy** written by onboarding allows **`DATA_TRANSFER_ROLE_ARN`** to `s3:PutObject` and `s3:PutObjectAcl` on `arn:aws:s3:::mmm-{env}-data-{client}/*`.

- Terraform sets `DATA_TRANSFER_ROLE_ARN` from **`data.aws_iam_role.data_ingestion_lambda`**, resolved by role name `data_ingestion_lambda_role_name` (default: `{env_prefix}-data-ingestion-pipeline-lambda-role` — see [`terraform/variables.tf`](../terraform/variables.tf)).
- **Platform team:** in QA/PRD, ensure that role **exists before** onboarding applies, or override `data_ingestion_lambda_role_name` to match your Spacelift-managed data-ingestion stack.

The **data-transfer Lambda role** still needs its own **IAM** (or resource policy) to write to `mmm-{env}-data-*` buckets; the new bucket’s policy grants access to the role ARN you pass in — **no change** to ingestion code if the role name/ARN is correct.

---

## 5. Environment variables (Spacelift / Lambda configuration)

Set these on the onboarding Lambda (see [`terraform/lambda.tf`](../terraform/lambda.tf)):

| Variable | Purpose |
|----------|---------|
| `ENVIRONMENT` | Same as `env_prefix` (segment in bucket name: `dev`, `qa`, `prod`, etc.) |
| `DATA_TRANSFER_ROLE_ARN` | ARN embedded in bucket policy for writes |
| `VIP_ENCRYPTION_TYPE` | `SSE-S3` or `SSE-KMS` |
| `VIP_KMS_KEY_ARN` | Required if SSE-KMS |
| `VIP_ENABLE_VERSIONING` | `true` / `false` |
| `VIP_ENABLE_LOGGING` | `true` / `false` (logging skipped if log bucket missing) |

Plus existing onboarding variables (`CLIENT_METADATA_TABLE`, `PIPELINE_INFOS_TABLE`, `AWS_REGION_NAME`, `LOG_LEVEL`, etc.).

---

## 6. Checklist for the team (QA / PRD on Spacelift)

1. **Run the onboarding Terraform stack** (same module as dev) with the correct `env_prefix` and AWS provider for QA/PRD.
2. **Onboarding Lambda role** includes the **S3 VIP provision** statement (section 3.1) on `mmm-{env}-data-*` and `/*`.
3. **Data ingestion Lambda IAM role** exists; Terraform `data "aws_iam_role" "data_ingestion_lambda"` succeeds, or `data_ingestion_lambda_role_name` is set correctly.
4. **Log bucket** `mmm-{env}-data-logs` exists if you want access logging (optional).
5. **KMS**: if using SSE-KMS, key policy + IAM for onboarding role on that key.
6. **DynamoDB** tables for that environment exist and match variable-derived names.
7. After deploy, smoke-test `POST /onboarding` and confirm `vip_bucket_created` and bucket in S3.

---

## 7. Reference files in this repo

| Topic | File |
|-------|------|
| IAM (including S3 VIP policy) | [`terraform/iam.tf`](../terraform/iam.tf) |
| Lambda env vars | [`terraform/lambda.tf`](../terraform/lambda.tf) |
| Variables (`env_prefix`, VIP, data ingestion role name) | [`terraform/variables.tf`](../terraform/variables.tf) |
| Bucket create/configure logic | [`lambda/vip_bucket_provisioner.py`](../lambda/vip_bucket_provisioner.py) |

---

## 8. Copy-paste IAM for tickets

Full trust policy + all inline JSON policies (placeholders only) for platform tickets: **[TICKET_IAM_ONBOARDING_LAMBDA_COPYPASTE.md](./TICKET_IAM_ONBOARDING_LAMBDA_COPYPASTE.md)**.

---

## 9. Terraform `env_prefix` vs naming “PRD”

`variables.tf` validates `env_prefix` as one of `dev`, `qa`, `staging`, `prod`. If your organisation uses the label **PRD** only in Spacelift but the bucket prefix must be **`mmm-prod-data-...`**, use **`env_prefix = "prod"`** in tfvars while keeping your Spacelift stack name as PRD.
