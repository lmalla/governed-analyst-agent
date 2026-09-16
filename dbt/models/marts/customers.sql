-- Row access policies re-applied on every `dbt run` via post-hook, since
-- CREATE OR REPLACE (this model's materialization) drops them otherwise
-- (see BUILD_SPEC.md §5). Scoped to `customers` only for this spike —
-- orders/support_tickets get the same pattern in a later plan.
{{ config(
    post_hook=[
        "CREATE OR REPLACE ROW ACCESS POLICY all_rows ON {{ this }} GRANT TO (\"serviceAccount:persona-analyst@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"serviceAccount:persona-governance@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"user:{{ env_var('USER_EMAIL') }}\") FILTER USING (TRUE)",
        "CREATE OR REPLACE ROW ACCESS POLICY east_only ON {{ this }} GRANT TO (\"serviceAccount:persona-support-east@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\") FILTER USING (region = 'East')"
    ]
) }}
select * from {{ ref('stg_customers') }}
