# Onboarding Lambda — IAM role and policies (copy-paste for tickets)

Use this document in a Jira/Linear ticket. Replace placeholders before apply:

| Placeholder | Example | Meaning |
|-------------|---------|---------|
| `<AWS_ACCOUNT_ID>` | `123456789012` | Target AWS account |
| `<REGION>` | `eu-west-1` | Region where Lambda runs |
| `<ENV_PREFIX>` | `dev`, `qa`, `prod` | Environment segment (must match `ENVIRONMENT` on the Lambda) |

**Suggested IAM role name (matches Terraform):** `mmm-<ENV_PREFIX>-onboarding-handler-role`  
**Suggested Lambda function name:** `mmm-<ENV_PREFIX>-onboarding-handler`

**Attach to the role:** AWS managed policy `arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole`

---

## 1) Trust policy (who can assume this role)

Only AWS Lambda may assume this role.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

---

## 2) Inline policy — CloudWatch Logs (onboarding Lambda)

**Policy name (suggested):** `mmm-<ENV_PREFIX>-onboarding-handler-logging`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:<REGION>:<AWS_ACCOUNT_ID>:log-group:/aws/lambda/mmm-<ENV_PREFIX>-onboarding-handler:*"
      ]
    }
  ]
}
```

---

## 3) Inline policy — DynamoDB (client metadata + pipeline infos)

**Policy name (suggested):** `mmm-<ENV_PREFIX>-onboarding-handler-dynamodb`

Default table names used by Terraform when variables are empty:

- Client metadata: `<ENV_PREFIX>-mmm-client-metadata`
- Pipeline infos: `mmm-<ENV_PREFIX>-pipeline-infos`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:BatchWriteItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:<REGION>:<AWS_ACCOUNT_ID>:table/<ENV_PREFIX>-mmm-client-metadata",
        "arn:aws:dynamodb:<REGION>:<AWS_ACCOUNT_ID>:table/mmm-<ENV_PREFIX>-pipeline-infos",
        "arn:aws:dynamodb:<REGION>:<AWS_ACCOUNT_ID>:table/<ENV_PREFIX>-mmm-client-metadata/index/*",
        "arn:aws:dynamodb:<REGION>:<AWS_ACCOUNT_ID>:table/mmm-<ENV_PREFIX>-pipeline-infos/index/*"
      ]
    }
  ]
}
```

If your tables use different names, replace the four ARNs accordingly (keep `/index/*` for any GSI queries the Lambda uses).

---

## 4) Inline policy — S3 VIP bucket auto-creation (`mmm-<ENV>-data-<client_id>`)

**Policy name (suggested):** `mmm-<ENV_PREFIX>-onboarding-handler-s3-vip-provision`

This is what allows the onboarding Lambda to **create and configure** per-client VIP buckets.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "VipBucketProvision",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:HeadBucket",
        "s3:GetBucketLocation",
        "s3:PutBucketVersioning",
        "s3:PutEncryptionConfiguration",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketOwnershipControls",
        "s3:PutLifecycleConfiguration",
        "s3:PutBucketTagging",
        "s3:PutBucketPolicy",
        "s3:PutBucketLogging"
      ],
      "Resource": [
        "arn:aws:s3:::mmm-<ENV_PREFIX>-data-*",
        "arn:aws:s3:::mmm-<ENV_PREFIX>-data-*/*"
      ]
    }
  ]
}
```

---

## 5) AWS managed policy (attach to the same role)

**Policy ARN:** `arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole`

Grants `logs:CreateLogStream` and `logs:PutLogEvents` on generic log groups; the inline policy in section 2 scopes to the function log group. Attaching both matches the Terraform setup (`lambda_basic_execution` attachment + explicit logging policy).

---

## 6) Lambda environment variable the platform team must set (bucket policy)

The Lambda reads **`DATA_TRANSFER_ROLE_ARN`**: ARN of the **data ingestion** Lambda execution role. That ARN is embedded in each new VIP bucket policy so ingestion can `PutObject` / `PutObjectAcl`.

**Example role name pattern:** `mmm-<ENV_PREFIX>-data-ingestion-pipeline-lambda-role`  
**Example ARN:** `arn:aws:iam::<AWS_ACCOUNT_ID>:role/mmm-<ENV_PREFIX>-data-ingestion-pipeline-lambda-role`

The onboarding Lambda role does **not** need IAM permission to modify that role; only the **correct ARN string** must be in Lambda env config (Terraform: `DATA_TRANSFER_ROLE_ARN` in `lambda.tf`).

---

## 7) Optional — KMS (only if VIP buckets use SSE-KMS)

If `VIP_ENCRYPTION_TYPE=SSE-KMS` and `VIP_KMS_KEY_ARN` is set, the onboarding role also needs KMS permissions on that key (exact actions depend on your key policy; commonly `kms:Decrypt`, `kms:GenerateDataKey*`, `kms:DescribeKey`). Add a separate inline policy or extend key policy — not included in the default Terraform `iam.tf`.

---

## 8) Summary checklist for the assignee

1. Create role `mmm-<ENV>-onboarding-handler-role` with trust policy (section 1).
2. Attach `AWSLambdaBasicExecutionRole` (section 5).
3. Add inline policies: sections 2, 3, 4 (adjust table ARNs if needed).
4. Ensure Lambda env includes `DATA_TRANSFER_ROLE_ARN` for the data ingestion role (section 6).
5. If SSE-KMS: add KMS permissions (section 7).

Source in repo: `onboarding_pipeline/terraform/iam.tf`, `lambda.tf`, `variables.tf`.
